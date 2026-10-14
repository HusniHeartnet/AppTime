"""AppTime - screen time tracker for Windows and macOS.

Records how long each app is on screen while you're at the computer, and
serves a local dashboard (day / week / month / year / lifetime).

Run:  pythonw apptime.py      (Windows: tray icon, no console)
      python3 apptime.py      (macOS, or with a console for logs)
      python apptime.py --selftest   (check tracking works, then exit)

The operating-system parts live in platform_win.py and platform_mac.py.
"""

import secrets
import ssl
import urllib.error
import urllib.request
import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import updater

if sys.platform == "darwin":
    import platform_mac as osx
else:
    import platform_win as osx

APP_NAME = "AppTime"
APP_VERSION = "1.7.0"

# Community sharing (see server/README.md). Sharing is only offered when the
# URL is set AND server_cert.pem (the server's own certificate) is present;
# AppTime trusts only that certificate when talking to the server.
COMMUNITY_URL = "https://139.190.100.30:8443"
PRIVACY_URL = "https://github.com/HusniHeartnet/AppTime/blob/main/PRIVACY.md"
COMMUNITY_LINK = ""  # optional: where results are posted, e.g. a Discord invite
UPLOAD_CHECK_SECONDS = 15 * 60
PORT = int(os.environ.get("APPTIME_PORT", "47820"))
URL = f"http://127.0.0.1:{PORT}/"
POLL_SECONDS = 1.0
FLUSH_SECONDS = 30
MAX_GAP_SECONDS = 5  # longer gaps between polls mean sleep/hibernate: not counted
DEFAULT_IDLE_MINUTES = 5

DATA_DIR = os.path.join(osx.DATA_ROOT, APP_NAME)
DB_PATH = os.path.join(DATA_DIR, "apptime.db")
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
SERVER_CERT_PATH = os.path.join(RESOURCE_DIR, "server_cert.pem")

# ---------------------------------------------------------------- own resource usage

class SelfUsage:
    """CPU and memory used by this background process (shown as AppTime in Task Manager / Activity Monitor)."""

    def __init__(self):
        self.last = (time.monotonic(), time.process_time())
        self.cpu_percent = 0.0

    def sample(self):
        now, cpu = time.monotonic(), time.process_time()
        wall = now - self.last[0]
        if wall >= 2:  # average over at least a couple of seconds
            self.cpu_percent = max(0.0, (cpu - self.last[1]) / wall / (os.cpu_count() or 1) * 100)
            self.last = (now, cpu)
        return {"cpu_percent": round(self.cpu_percent, 2), "memory_mb": round(osx.memory_mb(), 1)}


self_usage = SelfUsage()


# ---------------------------------------------------------------- storage

def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    had_usage = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'usage'").fetchone()
    had_screen = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'screen'").fetchone()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage (
            day TEXT NOT NULL,          -- YYYY-MM-DD, local time
            hour INTEGER NOT NULL,      -- 0-23
            exe TEXT NOT NULL,          -- app key: code.exe (Windows), com.microsoft.vscode (macOS)
            seconds REAL NOT NULL,
            PRIMARY KEY (day, hour, exe)
        );
        -- Real clock time with at least one app on screen. Several apps can be
        -- on screen at once, so app times can add up to more than this.
        CREATE TABLE IF NOT EXISTS screen (
            day TEXT NOT NULL,
            hour INTEGER NOT NULL,
            seconds REAL NOT NULL,
            PRIMARY KEY (day, hour)
        );
        CREATE TABLE IF NOT EXISTS apps (
            exe TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    if had_usage and not had_screen:
        # Older versions counted only the focused app, so app time was screen time.
        conn.execute("INSERT INTO screen SELECT day, hour, SUM(seconds) FROM usage GROUP BY day, hour")
        conn.commit()
    return conn


def get_setting(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()


# ---------------------------------------------------------------- backup

BACKUP_EXTENSION = ".apptime"


def export_backup(path):
    """Save a copy of all tracked time to `path` (an SQLite file)."""
    src = connect()
    dest = sqlite3.connect(path)
    try:
        src.backup(dest)
        # The community-sharing ID and its secret stay on this computer only:
        # anyone holding them could delete this person's shared data.
        dest.execute("DELETE FROM settings WHERE key = 'share'")
        dest.commit()
        days = dest.execute("SELECT COUNT(DISTINCT day) FROM screen").fetchone()[0]
    finally:
        dest.close()
        src.close()
    return {"path": path, "days": days}


def import_backup(path):
    """Merge a backup into this computer's data.

    For each app and hour, the larger of the two values is kept, so importing
    the same backup twice never double-counts."""
    try:
        src = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError:
        raise ValueError("That file isn't an AppTime backup.")
    try:
        if not {"usage", "apps"} <= tables:
            raise ValueError("That file isn't an AppTime backup.")
        usage = src.execute("SELECT day, hour, exe, seconds FROM usage").fetchall()
        if "screen" in tables:
            screen = src.execute("SELECT day, hour, seconds FROM screen").fetchall()
        else:  # backups from before screen time was stored separately
            screen = src.execute("SELECT day, hour, SUM(seconds) FROM usage GROUP BY day, hour").fetchall()
        apps = src.execute("SELECT exe, name, path FROM apps").fetchall()
    finally:
        src.close()
    usage = [u for u in usage if u[2] not in osx.IGNORED]
    conn = connect()
    try:
        conn.executemany(
            """INSERT INTO usage (day, hour, exe, seconds) VALUES (?, ?, ?, ?)
               ON CONFLICT (day, hour, exe) DO UPDATE SET seconds = MAX(seconds, excluded.seconds)""", usage)
        conn.executemany(
            """INSERT INTO screen (day, hour, seconds) VALUES (?, ?, ?)
               ON CONFLICT (day, hour) DO UPDATE SET seconds = MAX(seconds, excluded.seconds)""", screen)
        conn.executemany("INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?, ?, ?)", apps)
        conn.commit()
    finally:
        conn.close()
    return {"days": len({row[0] for row in screen})}


# ---------------------------------------------------------------- tracker

class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}  # (day, hour, exe) -> seconds not yet written; exe None = screen time
        self.known_apps = set()
        self.paused = False
        self.current = set()  # exes being counted right now
        self.stop_event = threading.Event()
        conn = connect()
        self.idle_minutes = get_setting(conn, "idle_minutes", DEFAULT_IDLE_MINUTES)
        self.known_apps = {r[0] for r in conn.execute("SELECT exe FROM apps")}
        ignored = sorted(osx.IGNORED - {""})
        conn.execute(f"DELETE FROM usage WHERE exe IN ({','.join('?' * len(ignored))})", ignored)
        conn.commit()
        conn.close()

    def run(self):
        conn = connect()
        last = time.monotonic()
        last_flush = last
        while not self.stop_event.wait(POLL_SECONDS):
            now = time.monotonic()
            gap = now - last
            last = now
            exes = set()
            if not self.paused and gap <= MAX_GAP_SECONDS and osx.idle_seconds() < self.idle_minutes * 60:
                try:
                    apps = osx.visible_apps()
                except Exception:
                    apps = {}
                for exe, path in apps.items():
                    if exe in osx.IGNORED:
                        continue
                    exes.add(exe)
                    if exe not in self.known_apps:
                        conn.execute(
                            "INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?, ?, ?)",
                            (exe, osx.app_name(exe, path), path),
                        )
                        conn.commit()
                        self.known_apps.add(exe)
            self.current = exes
            if exes:
                stamp = dt.datetime.now()
                day = stamp.strftime("%Y-%m-%d")
                with self.lock:
                    for exe in exes | {None}:
                        key = (day, stamp.hour, exe)
                        self.pending[key] = self.pending.get(key, 0.0) + gap
            if now - last_flush >= FLUSH_SECONDS:
                self.flush(conn)
                last_flush = now
        self.flush(conn)
        conn.close()

    def flush(self, conn=None):
        with self.lock:
            items, self.pending = self.pending, {}
        if not items:
            return
        own = conn is None
        conn = conn or connect()
        conn.executemany(
            """INSERT INTO usage (day, hour, exe, seconds) VALUES (?, ?, ?, ?)
               ON CONFLICT (day, hour, exe) DO UPDATE SET seconds = seconds + excluded.seconds""",
            [(d, h, e, s) for (d, h, e), s in items.items() if e is not None],
        )
        conn.executemany(
            """INSERT INTO screen (day, hour, seconds) VALUES (?, ?, ?)
               ON CONFLICT (day, hour) DO UPDATE SET seconds = seconds + excluded.seconds""",
            [(d, h, s) for (d, h, e), s in items.items() if e is None],
        )
        conn.commit()
        if own:
            conn.close()


# ---------------------------------------------------------------- stats

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def month_start(d):
    return d.replace(day=1)


def add_months(d, n):
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def period(rng, anchor, first_day):
    """Return (start, end_inclusive, label, prev_anchor, next_anchor)."""
    if rng == "day":
        return anchor, anchor, anchor.strftime("%A, %d %B %Y"), anchor - dt.timedelta(days=1), anchor + dt.timedelta(days=1)
    if rng == "week":
        start = anchor - dt.timedelta(days=anchor.weekday())
        end = start + dt.timedelta(days=6)
        label = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
        return start, end, label, start - dt.timedelta(days=7), start + dt.timedelta(days=7)
    if rng == "month":
        start = month_start(anchor)
        end = add_months(start, 1) - dt.timedelta(days=1)
        return start, end, start.strftime("%B %Y"), add_months(start, -1), add_months(start, 1)
    if rng == "year":
        start = dt.date(anchor.year, 1, 1)
        return start, dt.date(anchor.year, 12, 31), str(anchor.year), dt.date(anchor.year - 1, 1, 1), dt.date(anchor.year + 1, 1, 1)
    # lifetime
    return first_day, dt.date.today(), "Lifetime", None, None


def buckets_for(rng, start, end):
    """List of (key, label, sql_expr_value) describing the chart's x axis."""
    if rng == "day":
        return "hour", [(h, f"{h:02d}") for h in range(24)]
    if rng in ("week", "month"):
        out, d = [], start
        while d <= end:
            label = WEEKDAYS[d.weekday()] if rng == "week" else str(d.day)
            out.append((d.isoformat(), label))
            d += dt.timedelta(days=1)
        return "day", out
    if rng == "year":
        return "month", [(f"{start.year}-{m:02d}", MONTHS[m - 1]) for m in range(1, 13)]
    # lifetime: months if the history is short, years otherwise
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if months <= 36:
        out, d = [], month_start(start)
        while d <= end:
            out.append((d.strftime("%Y-%m"), f"{MONTHS[d.month - 1]} {d.strftime('%y')}"))
            d = add_months(d, 1)
        return "month", out
    return "year", [(str(y), str(y)) for y in range(start.year, end.year + 1)]


def total_between(conn, start, end, exe=None):
    """Screen time, or one app's time when `exe` is given."""
    args = [start.isoformat(), end.isoformat()]
    if exe:
        return conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) FROM usage WHERE day BETWEEN ? AND ? AND exe = ?", args + [exe]
        ).fetchone()[0]
    return conn.execute("SELECT COALESCE(SUM(seconds), 0) FROM screen WHERE day BETWEEN ? AND ?", args).fetchone()[0]


def stats(conn, rng, anchor, exe=None):
    today = dt.date.today()
    row = conn.execute("SELECT MIN(day) FROM screen").fetchone()
    first_day = dt.date.fromisoformat(row[0]) if row and row[0] else today
    start, end, label, prev_anchor, next_anchor = period(rng, anchor, first_day)
    # Bar heights and totals: one app's time when filtered, otherwise screen time.
    where = "day BETWEEN ? AND ?"
    args = [start.isoformat(), end.isoformat()]
    if exe:
        where += " AND exe = ?"
        args.append(exe)
    height_table = "usage" if exe else "screen"

    unit, bucket_list = buckets_for(rng, start, end)
    expr = {"hour": "hour", "day": "day", "month": "substr(day, 1, 7)", "year": "substr(day, 1, 4)"}[unit]
    heights = dict(conn.execute(f"SELECT {expr}, SUM(seconds) FROM {height_table} WHERE {where} GROUP BY 1", args))
    per_app = {}
    for key, app_exe, secs in conn.execute(f"SELECT {expr}, exe, SUM(seconds) FROM usage WHERE {where} GROUP BY 1, 2", args):
        per_app.setdefault(key, {})[app_exe] = secs
    buckets = [
        {"key": k, "label": lab, "seconds": heights.get(k, 0), "apps": per_app.get(k, {})}
        for k, lab in bucket_list
    ]

    # Each app keeps one color everywhere: the 7 most-used apps of all time get a
    # color slot (in that order), everything else is "Other".
    colors = {
        e: i
        for i, (e,) in enumerate(conn.execute("SELECT exe FROM usage GROUP BY exe ORDER BY SUM(seconds) DESC LIMIT 7"))
    }

    # The app list always covers every app, so you can switch between them.
    apps = [
        {"exe": e, "name": n or e, "seconds": s}
        for e, n, s in conn.execute(
            """SELECT u.exe, a.name, SUM(u.seconds) FROM usage u LEFT JOIN apps a ON a.exe = u.exe
               WHERE u.day BETWEEN ? AND ? GROUP BY u.exe ORDER BY 3 DESC""",
            args[:2],
        )
    ]
    screen_total = total_between(conn, start, end)
    total = sum(b["seconds"] for b in buckets)
    days_tracked = conn.execute(f"SELECT COUNT(DISTINCT day) FROM {height_table} WHERE {where}", args).fetchone()[0]

    previous_total = None
    has_prev = False
    if rng in ("day", "week", "month", "year"):
        p_start, p_end, *_ = period(rng, prev_anchor, first_day)
        has_prev = p_end >= first_day
        if has_prev:
            previous_total = total_between(conn, p_start, p_end, exe)

    busiest = max(buckets, key=lambda b: b["seconds"]) if total else None
    app_name = None
    if exe:
        r = conn.execute("SELECT name FROM apps WHERE exe = ?", (exe,)).fetchone()
        app_name = r[0] if r else exe

    return {
        "range": rng,
        "anchor": anchor.isoformat(),
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev": prev_anchor.isoformat() if has_prev else None,
        "next": next_anchor.isoformat() if next_anchor and next_anchor <= today else None,
        "today": today.isoformat(),
        "first_day": first_day.isoformat(),
        "unit": unit,
        "total": total,
        "screen_total": screen_total,
        "previous_total": previous_total,
        "days_tracked": days_tracked,
        "busiest": busiest,
        "buckets": buckets,
        "apps": apps,
        "colors": colors,
        "app": exe,
        "app_name": app_name,
    }


# ---------------------------------------------------------------- community sharing

SHARE_DEFAULTS = {
    "decided": False,       # has the person answered the consent question?
    "enabled": False,
    "since": None,          # first day that may be shared (the day they agreed)
    "install_id": None,
    "token": None,
    "hidden": [],           # exes never shared
    "last_uploaded": None,  # last day sent successfully
    "last_status": None,
}


def get_share(conn):
    return {**SHARE_DEFAULTS, **get_setting(conn, "share", {})}


def set_share(conn, share):
    set_setting(conn, "share", share)


def share_days(conn, share, include_today=False):
    """The day records that would be sent, oldest first (at most 60)."""
    if not share["since"]:
        return []
    today = dt.date.today().isoformat()
    day_before_since = (dt.date.fromisoformat(share["since"]) - dt.timedelta(days=1)).isoformat()
    after = max(share["last_uploaded"] or "", day_before_since)
    op = "<=" if include_today else "<"
    days = [
        r[0]
        for r in conn.execute(
            f"SELECT day FROM screen WHERE day > ? AND day {op} ? GROUP BY day ORDER BY day LIMIT 60",
            (after, today),
        )
    ]
    hidden = set(share["hidden"])
    out = []
    for day in days:
        screen = conn.execute("SELECT SUM(seconds) FROM screen WHERE day = ?", (day,)).fetchone()[0] or 0
        apps = [
            {"exe": e, "name": n or e, "seconds": int(sec)}
            for e, n, sec in conn.execute(
                """SELECT u.exe, a.name, SUM(u.seconds) FROM usage u LEFT JOIN apps a ON a.exe = u.exe
                   WHERE u.day = ? GROUP BY u.exe ORDER BY 3 DESC""",
                (day,),
            )
            if e not in hidden and sec >= 1
        ]
        out.append({"day": day, "screen": int(screen), "apps": apps})
    return out


def sharing_available():
    return bool(COMMUNITY_URL) and os.path.exists(SERVER_CERT_PATH)


def server_ssl_context():
    ctx = ssl.create_default_context(cafile=SERVER_CERT_PATH)  # trust our server only
    return ctx


def post_json(path, payload):
    req = urllib.request.Request(
        COMMUNITY_URL.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20, context=server_ssl_context()) as res:
        return json.loads(res.read() or b"{}")


share_lock = threading.Lock()


def upload_pending():
    """Send finished days that haven't been shared yet. Safe to call any time."""
    if not sharing_available():
        return
    with share_lock:
        conn = connect()
        try:
            share = get_share(conn)
            if not share["enabled"]:
                return
            days = share_days(conn, share)
            if not days:
                return
            try:
                post_json("/api/upload", {
                    "install_id": share["install_id"], "token": share["token"],
                    "app_version": APP_VERSION, "days": days,
                })
                share["last_uploaded"] = days[-1]["day"]
                share["last_status"] = f"Sent {len(days)} day(s) on {dt.datetime.now():%d %b %Y %H:%M}"
            except (urllib.error.URLError, OSError, ValueError):
                share["last_status"] = f"Couldn't reach the server ({dt.datetime.now():%d %b %H:%M}), will retry"
            set_share(conn, share)
        finally:
            conn.close()


def uploader(stop_event):
    stop_event.wait(60)  # let startup settle
    while not stop_event.is_set():
        upload_pending()
        stop_event.wait(UPLOAD_CHECK_SECONDS)


def share_state(conn):
    share = get_share(conn)
    preview = share_days(conn, share, include_today=True) if share["enabled"] else []
    if not preview:
        # Nothing waiting: show what today would look like.
        sample = {**share, "since": dt.date.today().isoformat(), "last_uploaded": None}
        preview = share_days(conn, sample, include_today=True)[-1:]
    apps = [
        {"exe": e, "name": n or e}
        for e, n in conn.execute(
            "SELECT u.exe, a.name FROM usage u LEFT JOIN apps a ON a.exe = u.exe GROUP BY u.exe ORDER BY SUM(u.seconds) DESC"
        )
    ]
    return {
        "available": sharing_available(),
        "decided": share["decided"],
        "enabled": share["enabled"],
        "has_id": bool(share["install_id"]),
        "since": share["since"],
        "hidden": share["hidden"],
        "last_uploaded": share["last_uploaded"],
        "last_status": share["last_status"],
        "community_url": COMMUNITY_LINK or None,
        "privacy_url": PRIVACY_URL,
        "preview": preview,
        "today": dt.date.today().isoformat(),
        "apps": apps,
    }


def update_share(conn, body):
    with share_lock:
        share = get_share(conn)
        if "enabled" in body:
            enabled = bool(body["enabled"])
            share["decided"] = True
            if enabled and not share["enabled"]:
                share["since"] = dt.date.today().isoformat()
                share["last_uploaded"] = None
                if not share["install_id"]:
                    share["install_id"] = secrets.token_hex(16)
                    share["token"] = secrets.token_hex(32)
            share["enabled"] = enabled
        if "hidden" in body:
            share["hidden"] = sorted({str(e).lower() for e in body["hidden"]})
        set_share(conn, share)


def delete_shared(conn):
    """Ask the server to delete everything sent from this install, then start over with a new ID."""
    with share_lock:
        share = get_share(conn)
        if share["install_id"]:
            post_json("/api/delete", {"install_id": share["install_id"], "token": share["token"]})
        share.update(enabled=False, decided=True, since=None, install_id=None, token=None, last_uploaded=None,
                     last_status=f"Shared data deleted on {dt.datetime.now():%d %b %Y %H:%M}")
        set_share(conn, share)


# ---------------------------------------------------------------- web server

def make_handler(tracker):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                with open(os.path.join(RESOURCE_DIR, "dashboard.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if url.path in ("/assets/logo.png", "/favicon.ico"):
                name = "logo.png" if url.path.endswith(".png") else "AppTime.ico"
                ctype = "image/png" if name.endswith(".png") else "image/x-icon"
                with open(os.path.join(RESOURCE_DIR, "assets", name), "rb") as f:
                    return self.send(200, f.read(), ctype)
            if url.path == "/api/update":
                return self.send(200, app_updater.state())
            if url.path == "/api/window-ping":
                # The dashboard window asks whether it should come to the front.
                return self.send(200, {"focus": window.take_focus_request()})
            if url.path == "/api/stats":
                tracker.flush()
                rng = q.get("range", "day")
                if rng not in ("day", "week", "month", "year", "all"):
                    return self.send(400, {"error": "bad range"})
                try:
                    anchor = dt.date.fromisoformat(q.get("date", dt.date.today().isoformat()))
                except ValueError:
                    return self.send(400, {"error": "bad date"})
                conn = connect()
                try:
                    data = stats(conn, rng, anchor, q.get("app") or None)
                finally:
                    conn.close()
                data["paused"] = tracker.paused
                data["idle_minutes"] = tracker.idle_minutes
                data["usage"] = self_usage.sample()
                data["startup"] = osx.startup_enabled()
                data["startup_label"] = osx.STARTUP_LABEL
                return self.send(200, data)
            if url.path == "/api/share":
                conn = connect()
                try:
                    return self.send(200, share_state(conn))
                finally:
                    conn.close()
            self.send(404, {"error": "not found"})

        def do_POST(self):
            # Only accept requests from the dashboard itself.
            if self.headers.get("Origin") not in (None, URL.rstrip("/")):
                return self.send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/update":
                if body.get("action") == "check":
                    app_updater.check_quietly()
                elif body.get("action") == "install":
                    if app_updater.can_install():
                        app_updater.start_install()
                    else:
                        webbrowser.open(app_updater.notes_url)  # e.g. running from source: download by hand
                        return self.send(200, {**app_updater.state(), "manual": True})
                return self.send(200, app_updater.state())
            if url.path == "/api/quit":
                threading.Timer(0.3, quit_now).start()  # answer first, then quit
                return self.send(200, {"ok": True})
            if url.path == "/api/open-window":
                window.open()
                return self.send(200, {"ok": True})
            if url.path == "/api/settings":
                if "idle_minutes" in body:
                    minutes = int(body["idle_minutes"])
                    if not 1 <= minutes <= 60:
                        return self.send(400, {"error": "idle_minutes must be 1-60"})
                    tracker.idle_minutes = minutes
                    conn = connect()
                    set_setting(conn, "idle_minutes", minutes)
                    conn.close()
                if "paused" in body:
                    tracker.paused = bool(body["paused"])
                if "startup" in body:
                    set_startup(bool(body["startup"]))
                return self.send(200, {"ok": True})
            if url.path in ("/api/export", "/api/import"):
                path = str(body.get("path") or "")
                if not path:
                    return self.send(400, {"error": "No file chosen."})
                try:
                    if url.path == "/api/export":
                        tracker.flush()
                        return self.send(200, export_backup(path))
                    return self.send(200, import_backup(path))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                except (OSError, sqlite3.Error) as e:
                    return self.send(500, {"error": f"Couldn't use that file ({e})."})
            if url.path == "/api/share" and sharing_available():
                conn = connect()
                try:
                    update_share(conn, body)
                finally:
                    conn.close()
                if body.get("enabled"):
                    threading.Thread(target=upload_pending, daemon=True).start()
                return self.send(200, {"ok": True})
            if url.path == "/api/share/delete" and sharing_available():
                conn = connect()
                try:
                    delete_shared(conn)
                except (urllib.error.URLError, OSError, ValueError):
                    return self.send(502, {"error": "Couldn't reach the server. Nothing was changed, try again later."})
                finally:
                    conn.close()
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})

    return Handler


# ---------------------------------------------------------------- dashboard window
#
# The dashboard opens in its own window, run as a separate short-lived process.
# Closing the window ends that process, so the window's memory is given back
# and only the small background tracker keeps running.

def own_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")  # Windows: no console window
    return [pythonw if os.path.exists(pythonw) else exe, os.path.abspath(__file__), *args]


class DashboardWindow:
    def __init__(self):
        self.proc = None
        self.focus_requested = False

    def open(self):
        if self.proc and self.proc.poll() is None:
            self.focus_requested = True  # already open: bring it to the front
            return
        try:
            self.proc = subprocess.Popen(own_command("--window"))
            osx.OWN_PIDS.add(self.proc.pid)
        except OSError:
            webbrowser.open(URL)

    def take_focus_request(self):
        requested, self.focus_requested = self.focus_requested, False
        return requested

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


window = DashboardWindow()


def run_window():
    """Entry point of the `--window` process: shows the dashboard until closed."""
    try:
        import webview
    except ImportError:
        webbrowser.open(URL)
        return
    osx.prepare_window()

    def call(path, payload):
        req = urllib.request.Request(URL.rstrip("/") + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}") or {"error": "Something went wrong."}
        except OSError:
            return {"error": "AppTime isn't running."}

    dialogs = getattr(webview, "FileDialog", None)
    save_dialog = dialogs.SAVE if dialogs else webview.SAVE_DIALOG
    open_dialog = dialogs.OPEN if dialogs else webview.OPEN_DIALOG

    class Api:
        """Called from the dashboard page (window.pywebview.api)."""

        def export_data(self):
            name = f"AppTime backup {dt.date.today().isoformat()}{BACKUP_EXTENSION}"
            chosen = win.create_file_dialog(save_dialog, save_filename=name,
                                            file_types=(f"AppTime backup (*{BACKUP_EXTENSION})",))
            if not chosen:
                return {"cancelled": True}
            path = chosen if isinstance(chosen, str) else chosen[0]
            if not path.lower().endswith(BACKUP_EXTENSION):
                path += BACKUP_EXTENSION
            return call("/api/export", {"path": path})

        def import_data(self):
            chosen = win.create_file_dialog(open_dialog, file_types=(
                f"AppTime backup (*{BACKUP_EXTENSION};*.db)", "All files (*.*)"))
            if not chosen:
                return {"cancelled": True}
            return call("/api/import", {"path": chosen if isinstance(chosen, str) else chosen[0]})

    win = webview.create_window(
        APP_NAME, URL, width=1120, height=860, min_size=(400, 500), js_api=Api(),
        background_color="#111110" if osx.dark_mode() else "#f4f4f2",
    )

    closed = threading.Event()
    win.events.closed += closed.set

    def watch_focus():
        # Lets the tray's "Open dashboard" bring this window forward.
        while not closed.wait(1):
            try:
                with urllib.request.urlopen(URL + "api/window-ping", timeout=3) as res:
                    if json.loads(res.read()).get("focus"):
                        win.restore()
                        win.show()
                        win.on_top = True
                        win.on_top = False
            except Exception:
                if not main_is_running():
                    win.destroy()  # AppTime was quit: close the window too
                    return

    try:
        icon = os.path.join(RESOURCE_DIR, "assets", osx.WINDOW_ICON) if osx.WINDOW_ICON else None
        webview.start(watch_focus, private_mode=True, icon=icon if icon and os.path.exists(icon) else None)
    except Exception:
        webbrowser.open(URL)  # e.g. Windows without the WebView2 runtime
    # Window closed: end this process right away so all its memory is freed,
    # even if a helper thread is still finishing.
    os._exit(0)


def main_is_running():
    try:
        urllib.request.urlopen(URL + "api/window-ping", timeout=3).close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- startup / tray

# Things an update needs to shut down: the tracker and the tray icon.
running = {}


def save_and_stop():
    """Close the dashboard and save the latest tracked time."""
    tracker = running.get("tracker")
    window.close()
    if tracker:
        tracker.stop_event.set()
        tracker.flush()


def quit_now():
    """End AppTime cleanly (used by the installer and uninstaller)."""
    save_and_stop()
    try:
        running["tray"].stop()
    except Exception:
        pass
    os._exit(0)


def restart_into(command):
    """Start the freshly installed version and end this one."""
    save_and_stop()
    # A fully independent new copy: without this, PyInstaller would make it
    # reuse this copy's unpacked files, which are deleted when this copy exits.
    subprocess.Popen(command, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    quit_now()


app_updater = updater.Updater(APP_VERSION, f"{APP_NAME}/{APP_VERSION}", restart_into)


def set_startup(enabled):
    """The person switched starting at sign-in on or off."""
    osx.set_startup(enabled, own_command())
    conn = connect()
    set_setting(conn, "startup", bool(enabled))
    conn.close()


def ensure_startup():
    """Installed app: start automatically at sign-in unless the person switched
    that off. Runs at every start, so the startup entry is put back if it went
    missing, and follows the app if its file was moved."""
    if not getattr(sys, "frozen", False):
        return  # running from source code: leave it to the person
    conn = connect()
    wanted = get_setting(conn, "startup", True)
    conn.close()
    try:
        if wanted:
            osx.set_startup(True, own_command())
        elif osx.startup_enabled():
            osx.set_startup(False, own_command())
    except OSError:
        pass


def tray_image():
    from PIL import Image, ImageDraw
    path = os.path.join(RESOURCE_DIR, "assets", "tray.png")
    if os.path.exists(path):
        return Image.open(path)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(42, 120, 214, 255))
    d.line((32, 32, 32, 14), fill="white", width=6)
    d.line((32, 32, 46, 40), fill="white", width=6)
    d.ellipse((27, 27, 37, 37), fill="white")
    return img


def run_tray(tracker, server):
    import pystray

    def open_dashboard(icon=None, item=None):
        window.open()

    def toggle_pause(icon, item):
        tracker.paused = not tracker.paused

    def toggle_startup(icon, item):
        set_startup(not osx.startup_enabled())

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard, default=True),
        pystray.MenuItem("Pause tracking", toggle_pause, checked=lambda item: tracker.paused),
        pystray.MenuItem(osx.STARTUP_LABEL, toggle_startup, checked=lambda item: osx.startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(APP_NAME, tray_image(), APP_NAME, menu)
    running["tray"] = icon
    if sys.platform == "win32":
        allow_windows_shutdown(icon, tracker)
    icon.run()


def allow_windows_shutdown(icon, tracker):
    """Let Windows shut down or restart, saving the latest time first.

    Before shutting down, Windows asks every window whether it may. pystray's
    tray window answers 0 ("no") to every message it doesn't handle itself, so
    without this AppTime would block shutdown and get force-closed."""
    WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016

    def may_end(wparam, lparam):
        tracker.flush()
        return 1  # yes, go ahead

    def ending(wparam, lparam):
        if wparam:  # the session really is ending
            tracker.stop_event.set()
            tracker.flush()
            window.close()
        return 0

    icon._message_handlers[WM_QUERYENDSESSION] = may_end
    icon._message_handlers[WM_ENDSESSION] = ending


def selftest():
    """Check that tracking works on this computer, print what it sees, and exit.
    Used by the release build on GitHub's Windows and Mac machines."""
    print(f"{APP_NAME} {APP_VERSION} on {sys.platform}")
    print("idle seconds:", round(osx.idle_seconds(), 1))
    apps = osx.visible_apps()
    print("apps on screen:", {k: osx.app_name(k, p) for k, p in apps.items()})
    print("memory MB:", round(osx.memory_mb(), 1), "| dark mode:", osx.dark_mode())
    print("starts at login:", osx.startup_enabled(), "| sharing available:", sharing_available())
    for name in ("dashboard.html", "assets/logo.png", "assets/tray.png"):
        assert os.path.exists(os.path.join(RESOURCE_DIR, name)), f"missing {name}"
    try:
        app_updater.check()
        print("latest release:", app_updater.latest, "| update file found:", bool(app_updater.asset))
    except Exception as e:
        print("update check failed (network?):", e)
    import pystray  # noqa: F401  (the tray library loads)
    import webview  # noqa: F401  (the window library loads)
    print("selftest OK")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--window" in sys.argv:
        return run_window()
    after_update = "--after-update" in sys.argv
    server = None
    for _ in range(60 if after_update else 1):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", PORT), None)
            break
        except OSError:
            if after_update:
                time.sleep(0.5)  # the previous version is still closing
    if server is None:
        # Already running: ask that copy to show its dashboard.
        try:
            req = urllib.request.Request(URL + "api/open-window", data=b"{}", method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            webbrowser.open(URL)
        return

    tracker = Tracker()
    running["tracker"] = tracker
    if getattr(sys, "frozen", False):
        osx.record_installed_version(APP_VERSION, sys.executable)
    ensure_startup()
    threading.Thread(target=app_updater.loop, args=(tracker.stop_event,), daemon=True).start()
    threading.Timer(15, updater.remove_old_copy).start()  # previous version, if we were just updated
    if after_update:
        window.open()
    server.RequestHandlerClass = make_handler(tracker)
    threading.Thread(target=tracker.run, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if sharing_available():
        threading.Thread(target=uploader, args=(tracker.stop_event,), daemon=True).start()
        conn = connect()
        if not get_share(conn)["decided"]:
            window.open()  # first run: show the sharing question
        conn.close()

    if "--no-tray" in sys.argv:
        print(f"{APP_NAME} tracking. Dashboard: {URL}  (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        try:
            run_tray(tracker, server)
        except ImportError:
            print("pystray/Pillow not installed: running without a tray icon. Ctrl+C to quit.")
            print(f"Dashboard: {URL}")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

    window.close()
    tracker.stop_event.set()
    tracker.flush()
    server.shutdown()


if __name__ == "__main__":
    main()
