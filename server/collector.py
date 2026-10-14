"""AppTime community collector - runs on your VPS.

Two parts:
- Public (HTTPS, port 8443): receives anonymous daily app totals from AppTime
  users who opted in, and deletes them on request. Nothing can be read back.
- Private report (127.0.0.1:8081, only reachable through an SSH tunnel): the
  community stats page you screenshot for Discord and social media.

Run:  python3 collector.py
Needs Python 3.9+ (no extra packages) and cert.pem/key.pem (see server/README.md).
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("APPTIME_HOST", "0.0.0.0")
PORT = int(os.environ.get("APPTIME_PORT", "8443"))
REPORT_PORT = int(os.environ.get("APPTIME_REPORT_PORT", "8081"))
DB_PATH = os.environ.get("APPTIME_DB", os.path.join(HERE, "community.db"))
CERT_PATH = os.environ.get("APPTIME_CERT", os.path.join(HERE, "cert.pem"))
KEY_PATH = os.environ.get("APPTIME_KEY", os.path.join(HERE, "key.pem"))

# An app only shows on the community page once this many people use it,
# so one person's unusual app can't point back to them.
MIN_USERS_PER_APP = int(os.environ.get("APPTIME_MIN_USERS", "3"))
# Data older than this is deleted automatically (PRIVACY.md promises 2 years).
RETENTION_DAYS = int(os.environ.get("APPTIME_RETENTION_DAYS", "730"))
MAX_BODY = 256 * 1024
MAX_DAYS_PER_UPLOAD = 60
MAX_APPS_PER_DAY = 300
UPLOADS_PER_IP_PER_HOUR = 30
HIVEMIND_TOP_N = 5
HIVEMIND_MAX_USERS = 1500  # pairwise comparison cost grows with users squared

ID_RE = re.compile(r"^[0-9a-f]{32}$")
# App keys: "chrome.exe" (Windows), "com.google.chrome" (macOS bundle id), or
# "some app.app" (macOS app without a bundle id).
EXE_RE = re.compile(r"^(?:[a-z0-9 ._+()&'-]{1,80}\.(?:exe|app)|[a-z0-9-]+(?:\.[a-z0-9-]+){1,8})$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Windows' own tools and interface aren't counted (AppTime 1.3+ doesn't send
# them; this also drops them from older versions' uploads).
SYSTEM_EXES = {
    "apptime.exe", "explorer.exe", "taskmgr.exe", "systemsettings.exe", "resmon.exe", "perfmon.exe",
    "mmc.exe", "control.exe", "openwith.exe", "pickerhost.exe", "consent.exe", "rundll32.exe",
    "dllhost.exe", "searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "shellhost.exe", "textinputhost.exe", "screenclippinghost.exe",
    "applicationframehost.exe", "lockapp.exe", "logonui.exe",
    # macOS
    "com.husniheartnet.apptime", "com.apple.finder", "com.apple.activitymonitor", "com.apple.systempreferences",
    "com.apple.systemsettings", "com.apple.dock", "com.apple.controlcenter", "com.apple.notificationcenterui",
    "com.apple.spotlight", "com.apple.systemuiserver", "com.apple.windowmanager", "com.apple.screencaptureui",
    "com.apple.screensaver.engine", "com.apple.loginwindow", "com.apple.securityagent",
    "com.apple.coreservices.uiagent", "com.apple.universalaccessauthwarn",
}

db_lock = threading.Lock()
rate = {}  # ip -> [timestamps]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS installs (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            app_version TEXT
        );
        CREATE TABLE IF NOT EXISTS days (
            install TEXT NOT NULL,
            day TEXT NOT NULL,
            screen INTEGER NOT NULL,   -- seconds
            PRIMARY KEY (install, day)
        );
        CREATE TABLE IF NOT EXISTS app_days (
            install TEXT NOT NULL,
            day TEXT NOT NULL,
            exe TEXT NOT NULL,
            name TEXT NOT NULL,
            seconds INTEGER NOT NULL,
            PRIMARY KEY (install, day, exe)
        );
        CREATE INDEX IF NOT EXISTS app_days_day ON app_days (day);
        CREATE INDEX IF NOT EXISTS days_day ON days (day);
        """
    )
    return conn


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_install(conn, install_id, token, create):
    """True if the token matches (or a new install was registered)."""
    row = conn.execute("SELECT token_hash FROM installs WHERE id = ?", (install_id,)).fetchone()
    if row:
        return hmac.compare_digest(row[0], token_hash(token))
    if not create:
        return False
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO installs (id, token_hash, first_seen, last_seen) VALUES (?, ?, ?, ?)",
        (install_id, token_hash(token), now, now),
    )
    return True


def clean_name(name):
    name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()
    return name[:80]


def validate_days(days):
    """Return cleaned day records, or raise ValueError."""
    if not isinstance(days, list) or not 1 <= len(days) <= MAX_DAYS_PER_UPLOAD:
        raise ValueError("days must be a list of 1-60 entries")
    today = dt.date.today()
    out = []
    for d in days:
        day = str(d.get("day", ""))
        if not DAY_RE.match(day):
            raise ValueError("bad day")
        date = dt.date.fromisoformat(day)
        if date > today + dt.timedelta(days=1) or date < dt.date(2024, 1, 1):
            raise ValueError("day out of range")
        screen = int(d.get("screen", 0))
        if not 0 <= screen <= 86400:
            raise ValueError("screen out of range")
        apps = d.get("apps", [])
        if not isinstance(apps, list) or len(apps) > MAX_APPS_PER_DAY:
            raise ValueError("too many apps")
        clean_apps = {}
        for a in apps:
            exe = str(a.get("exe", "")).lower()
            secs = int(a.get("seconds", 0))
            if not EXE_RE.match(exe) or exe in SYSTEM_EXES or not 0 < secs <= 86400:
                continue
            clean_apps[exe] = (clean_name(a.get("name")) or exe[:-4], min(secs, screen))
        out.append((day, screen, clean_apps))
    return out


# ---------------------------------------------------------------- community stats

def community_stats(conn, rng):
    today = dt.date.today()
    if rng == "week":
        start = today - dt.timedelta(days=7)
    elif rng == "month":
        start = today - dt.timedelta(days=30)
    else:
        start = dt.date(2000, 1, 1)
    where, args = "day >= ? AND day < ?", [start.isoformat(), today.isoformat()]

    people, person_days, avg_screen = conn.execute(
        f"SELECT COUNT(DISTINCT install), COUNT(*), AVG(screen) FROM days WHERE {where}", args
    ).fetchone()
    people = people or 0

    # Apps are grouped by name, so e.g. Google Chrome on Windows (chrome.exe)
    # and on Mac (com.google.chrome) count as one app.
    rows = conn.execute(
        f"""SELECT MIN(exe), COUNT(DISTINCT install) AS users, SUM(seconds) AS secs,
                   (SELECT name FROM app_days n WHERE lower(n.name) = lower(a.name)
                    GROUP BY name ORDER BY COUNT(*) DESC LIMIT 1)
            FROM app_days a WHERE {where}
            GROUP BY lower(name) HAVING users >= ?""",
        args + [MIN_USERS_PER_APP],
    ).fetchall()
    apps = [
        {
            "exe": exe,
            "name": name or exe,
            "users": users,
            "share": users / people if people else 0,
            "hours": secs / 3600,
            "hours_per_user_day": 0.0,
        }
        for exe, users, secs, name in rows
    ]
    # Average time per person per day they used it.
    used_days = dict(
        conn.execute(f"SELECT lower(name), COUNT(*) FROM app_days WHERE {where} GROUP BY lower(name)", args).fetchall()
    )
    for a in apps:
        a["hours_per_user_day"] = a["hours"] / used_days.get(a["name"].lower(), 1)

    most_people = sorted(apps, key=lambda a: (-a["users"], -a["hours"]))[:15]
    most_hours = sorted(apps, key=lambda a: -a["hours"])[:15]

    # Hivemind score: how much everyone's top apps overlap (average Jaccard
    # similarity of each pair of people's top-5 apps, as a 0-100 score).
    tops = {}
    for install, exe, secs in conn.execute(
        f"SELECT install, lower(name), SUM(seconds) FROM app_days WHERE {where} GROUP BY install, lower(name)", args
    ):
        tops.setdefault(install, []).append((secs, exe))
    sets = [frozenset(e for _, e in sorted(v, reverse=True)[:HIVEMIND_TOP_N]) for v in tops.values()]
    sets = sets[:HIVEMIND_MAX_USERS]
    hivemind = None
    if len(sets) >= 2:
        total = count = 0
        for a, b in combinations(sets, 2):
            total += len(a & b) / len(a | b)
            count += 1
        hivemind = round(100 * total / count)

    return {
        "range": rng,
        "from": start.isoformat() if rng != "all" else None,
        "to": (today - dt.timedelta(days=1)).isoformat(),
        "people": people,
        "person_days": person_days or 0,
        "avg_screen_hours": (avg_screen or 0) / 3600,
        "hivemind": hivemind,
        "min_users_per_app": MIN_USERS_PER_APP,
        "most_people": most_people,
        "most_hours": most_hours,
    }


_cache = {}


def cached_stats(rng):
    hit = _cache.get(rng)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    conn = connect()
    try:
        data = community_stats(conn, rng)
    finally:
        conn.close()
    _cache[rng] = (time.time(), data)
    return data


# ---------------------------------------------------------------- HTTP

rate_lock = threading.Lock()


def rate_limited(ip):
    """IPs are only kept in memory, for at most an hour, to slow down abuse."""
    now = time.time()
    with rate_lock:
        if len(rate) > 10000:
            for key in [k for k, v in rate.items() if now - v[-1] >= 3600]:
                del rate[key]
        hits = [t for t in rate.get(ip, []) if now - t < 3600]
        hits.append(now)
        rate[ip] = hits
        return len(hits) > UPLOADS_PER_IP_PER_HOUR


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "AppTimeCollector"
    timeout = 30

    def log_message(self, fmt, *args):
        pass  # no IP logging

    def client_ip(self):
        return self.client_address[0]

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)



class ReportHandler(BaseHandler):
    """Private stats page. Listens on 127.0.0.1 only."""

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(HERE, "community.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/community":
            rng = parse_qs(url.query).get("range", ["month"])[0]
            if rng not in ("week", "month", "all"):
                return self.send(400, {"error": "bad range"})
            _cache.clear()  # the owner wants fresh numbers
            return self.send(200, cached_stats(rng))
        self.send(404, {"error": "not found"})


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # failed TLS handshakes from scanners etc. aren't worth a traceback


class PublicHandler(BaseHandler):
    """Upload and delete only. Nothing stored can be read through it."""

    def setup(self):
        super().setup()
        self.connection.do_handshake()  # TLS, in this request's own thread

    def do_GET(self):
        self.send(200, b"AppTime collector is running.\n", "text/plain; charset=utf-8")

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        url = urlparse(self.path)
        if rate_limited(self.client_ip()):
            return self.send(429, {"error": "slow down"})
        try:
            body = self.read_json()
            install_id = str(body.get("install_id", ""))
            token = str(body.get("token", ""))
            if not ID_RE.match(install_id) or len(token) < 32:
                raise ValueError("bad id")
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"error": "bad request"})

        if url.path == "/api/upload":
            try:
                days = validate_days(body.get("days"))
            except (ValueError, TypeError, AttributeError) as e:
                return self.send(400, {"error": str(e)})
            with db_lock:
                conn = connect()
                try:
                    if not check_install(conn, install_id, token, create=True):
                        return self.send(403, {"error": "forbidden"})
                    for day, screen, apps in days:
                        # Re-sending a day replaces it.
                        conn.execute("DELETE FROM app_days WHERE install = ? AND day = ?", (install_id, day))
                        conn.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)", (install_id, day, screen))
                        conn.executemany(
                            "INSERT INTO app_days VALUES (?, ?, ?, ?, ?)",
                            [(install_id, day, exe, n, s) for exe, (n, s) in apps.items()],
                        )
                    conn.execute(
                        "UPDATE installs SET last_seen = ?, app_version = ? WHERE id = ?",
                        (dt.datetime.utcnow().isoformat(timespec="seconds"), str(body.get("app_version", ""))[:20], install_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return self.send(200, {"ok": True, "days": len(days)})

        if url.path == "/api/delete":
            with db_lock:
                conn = connect()
                try:
                    if not check_install(conn, install_id, token, create=False):
                        # Nothing stored under this id (or wrong token): nothing to delete.
                        return self.send(200, {"ok": True, "deleted": False})
                    for table, col in (("app_days", "install"), ("days", "install"), ("installs", "id")):
                        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (install_id,))
                    conn.commit()
                finally:
                    conn.close()
            _cache.clear()
            return self.send(200, {"ok": True, "deleted": True})

        self.send(404, {"error": "not found"})


def purge_old_data():
    """Once a day, delete days older than the retention period, and installs with nothing left."""
    while True:
        cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).isoformat()
        with db_lock:
            conn = connect()
            try:
                conn.execute("DELETE FROM app_days WHERE day < ?", (cutoff,))
                conn.execute("DELETE FROM days WHERE day < ?", (cutoff,))
                conn.execute("DELETE FROM installs WHERE id NOT IN (SELECT DISTINCT install FROM days)"
                             " AND last_seen < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()
        time.sleep(24 * 3600)


def main():
    conn = connect()
    conn.execute(f"DELETE FROM app_days WHERE exe IN ({','.join('?' * len(SYSTEM_EXES))})", sorted(SYSTEM_EXES))
    conn.commit()
    conn.close()
    threading.Thread(target=purge_old_data, daemon=True).start()

    report = ThreadingHTTPServer(("127.0.0.1", REPORT_PORT), ReportHandler)
    threading.Thread(target=report.serve_forever, daemon=True).start()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    public = QuietServer((HOST, PORT), PublicHandler)
    public.socket = ctx.wrap_socket(public.socket, server_side=True, do_handshake_on_connect=False)

    print(f"AppTime collector: uploads on https://{HOST}:{PORT}, report on http://127.0.0.1:{REPORT_PORT}  (db: {DB_PATH})")
    public.serve_forever()


if __name__ == "__main__":
    main()
