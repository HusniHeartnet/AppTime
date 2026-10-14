"""AppTime - screen time tracker for Windows.

Records how long each app is in the foreground while you're at the laptop,
and serves a local dashboard (day / week / month / year / lifetime).

Run:  pythonw apptime.py      (tray icon, no console)
      python apptime.py       (same, with a console for logs)
"""

import ctypes
import secrets
import ssl
import urllib.error
import urllib.request
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_NAME = "AppTime"
APP_VERSION = "1.2"

# Community sharing (see server/README.md). Sharing is only offered when the
# URL is set AND server_cert.pem (the server's own certificate) is present;
# AppTime trusts only that certificate when talking to the server.
COMMUNITY_URL = "https://139.190.100.30:8443"
PRIVACY_URL = "https://github.com/HusniHeartnet/AppTime/blob/main/PRIVACY.md"
COMMUNITY_LINK = ""  # optional: where results are posted, e.g. a Discord invite
UPLOAD_CHECK_SECONDS = 15 * 60
PORT = 47820
URL = f"http://127.0.0.1:{PORT}/"
POLL_SECONDS = 1.0
FLUSH_SECONDS = 30
MAX_GAP_SECONDS = 5  # longer gaps between polls mean sleep/hibernate: not counted
DEFAULT_IDLE_MINUTES = 5

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
DB_PATH = os.path.join(DATA_DIR, "apptime.db")
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
SERVER_CERT_PATH = os.path.join(RESOURCE_DIR, "server_cert.pem")

# Foreground "apps" that aren't really you using an app.
IGNORED_EXES = {"lockapp.exe", "logonui.exe", "screenclippinghost.exe", ""}


# ---------------------------------------------------------------- Win32 bits

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetTickCount.restype = wt.DWORD
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]


def idle_seconds():
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def window_exe_path(hwnd):
    pid = window_pid(hwnd)
    path = process_path(pid) if pid else None
    # Store apps (Settings, Calculator, ...) live inside ApplicationFrameHost;
    # the real app is a child window owned by another process.
    if path and os.path.basename(path).lower() == "applicationframehost.exe":
        found = []

        def cb(child, _):
            child_pid = window_pid(child)
            if child_pid and child_pid != pid:
                found.append(child_pid)
                return False
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        if found:
            path = process_path(found[0]) or path
    return path


def foreground_exe_path():
    hwnd = user32.GetForegroundWindow()
    return window_exe_path(hwnd) if hwnd else None


# A window counts as "on screen" when at least this much of it is uncovered.
MIN_VISIBLE_FRACTION = 0.15
MIN_VISIBLE_PIXELS = 200 * 200

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

dwmapi = ctypes.WinDLL("dwmapi")
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM]

try:  # real pixel coordinates on scaled (125%, 150%...) displays
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass


def monitor_rects():
    rects = []

    def cb(_mon, _hdc, rect, _):
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return rects


def window_rect(hwnd):
    r = wt.RECT()
    # The frame bounds exclude Windows 10/11's invisible resize borders.
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def intersect(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def subtract(rects, cut):
    """Remove `cut` from a list of rectangles, splitting them as needed."""
    out = []
    for r in rects:
        i = intersect(r, cut)
        if area(i) == 0:
            out.append(r)
            continue
        left, top, right, bottom = r
        if i[1] > top:
            out.append((left, top, right, i[1]))
        if i[3] < bottom:
            out.append((left, i[3], right, bottom))
        if i[0] > left:
            out.append((left, i[1], i[0], i[3]))
        if i[2] < right:
            out.append((i[2], i[1], right, i[3]))
    return out


def candidate_windows():
    """Top-level app windows that could be on screen, front to back."""
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
            return True
        cloaked = wt.DWORD()
        # Cloaked = hidden by Windows: other virtual desktops, suspended Store apps.
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in SHELL_CLASSES:
            return True
        rect = window_rect(hwnd)
        if area(rect) > 0:
            found.append((hwnd, rect))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def visible_exe_paths():
    """Paths of every app with a window you can actually see, plus the focused app."""
    paths = set()
    fg = user32.GetForegroundWindow()
    fg_path = window_exe_path(fg) if fg else None
    if fg_path and os.path.basename(fg_path).lower() in IGNORED_EXES:
        return set()  # lock screen / sign-in screen
    if fg_path:
        paths.add(fg_path)
    uncovered = monitor_rects()
    for hwnd, rect in candidate_windows():
        if not uncovered:
            break
        shown = sum(area(intersect(r, rect)) for r in uncovered)
        if shown >= MIN_VISIBLE_PIXELS and shown >= MIN_VISIBLE_FRACTION * area(rect):
            path = window_exe_path(hwnd)
            if path:
                paths.add(path)
        uncovered = subtract(uncovered, rect)
    return paths


def friendly_name(path):
    """The 'File description' from the exe's version info, e.g. 'Visual Studio Code'."""
    fallback = os.path.splitext(os.path.basename(path))[0]
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return fallback
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return fallback
        ptr, length = ctypes.c_void_p(), wt.UINT()
        langs = [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]
        if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)) and length.value >= 4:
            pairs = ctypes.cast(ptr, ctypes.POINTER(wt.WORD))
            langs.insert(0, (pairs[0], pairs[1]))
        for lang, codepage in langs:
            key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            if version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length)) and length.value > 1:
                text = ctypes.wstring_at(ptr, length.value - 1).strip()
                if text:
                    return text
    except Exception:
        pass
    return fallback


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
            exe TEXT NOT NULL,          -- lowercase file name, e.g. code.exe
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
            if not self.paused and gap <= MAX_GAP_SECONDS and idle_seconds() < self.idle_minutes * 60:
                try:
                    paths = visible_exe_paths()
                except Exception:
                    paths = set()
                for path in paths:
                    exe = os.path.basename(path).lower()
                    if exe in IGNORED_EXES:
                        continue
                    exes.add(exe)
                    if exe not in self.known_apps:
                        conn.execute(
                            "INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?, ?, ?)",
                            (exe, friendly_name(path), path),
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
                return self.send(200, {"ok": True})
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


# ---------------------------------------------------------------- startup / tray

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def starts_with_windows():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_start_with_windows(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


def tray_image():
    from PIL import Image, ImageDraw
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
        webbrowser.open(URL)

    def toggle_pause(icon, item):
        tracker.paused = not tracker.paused

    def toggle_startup(icon, item):
        set_start_with_windows(not starts_with_windows())

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard, default=True),
        pystray.MenuItem("Pause tracking", toggle_pause, checked=lambda item: tracker.paused),
        pystray.MenuItem("Start with Windows", toggle_startup, checked=lambda item: starts_with_windows()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    pystray.Icon(APP_NAME, tray_image(), APP_NAME, menu).run()


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), None)
    except OSError:
        # Already running: just show the dashboard.
        webbrowser.open(URL)
        return

    tracker = Tracker()
    server.RequestHandlerClass = make_handler(tracker)
    threading.Thread(target=tracker.run, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if sharing_available():
        threading.Thread(target=uploader, args=(tracker.stop_event,), daemon=True).start()
        conn = connect()
        if not get_share(conn)["decided"]:
            webbrowser.open(URL)  # first run: show the sharing question
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

    tracker.stop_event.set()
    tracker.flush()
    server.shutdown()


if __name__ == "__main__":
    main()
