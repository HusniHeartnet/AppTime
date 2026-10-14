"""macOS parts of AppTime: which apps are on screen, idle time, the
dashboard window, and opening at login.

Same functions as platform_win.py. Uses only information macOS gives any
app without asking for permissions: the list of on-screen windows with
their owner app and position (not their titles or contents), and the time
since the last keyboard or mouse input.
"""

import os
import plistlib
import subprocess

import Quartz
from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSRunningApplication, NSWorkspace
from Foundation import NSUserDefaults

from geometry import visible_owners

APP_NAME = "AppTime"
DATA_ROOT = os.path.expanduser("~/Library/Application Support")
STARTUP_LABEL = "Open at Login"
WINDOW_ICON = None  # the app bundle's own icon is used

# macOS's own tools and interface aren't "apps you spend time on", so they
# aren't counted at all (not as app time, not as screen time, never shared).
# Keys are lowercase bundle identifiers.
IGNORED = {
    "",
    "com.husniheartnet.apptime",            # AppTime's own dashboard
    # macOS tools
    "com.apple.finder",                     # Finder and the desktop
    "com.apple.activitymonitor",            # Activity Monitor
    "com.apple.systempreferences",          # System Settings
    "com.apple.systemsettings",
    # macOS's own interface
    "com.apple.dock",                       # Dock, Launchpad, Mission Control
    "com.apple.controlcenter",              # Control Center, menu bar items
    "com.apple.notificationcenterui",       # notifications, widgets
    "com.apple.spotlight",                  # Spotlight search
    "com.apple.systemuiserver",
    "com.apple.windowmanager",              # Stage Manager
    "com.apple.screencaptureui",            # screenshot toolbar
    "com.apple.screensaver.engine",         # screen saver
    "com.apple.loginwindow",                # lock and login screens
    "com.apple.securityagent",              # password prompts
    "com.apple.coreservices.uiagent",       # "are you sure you want to open" dialogs
    "com.apple.universalaccessauthwarn",
}

# Processes of this running AppTime (the tracker and its dashboard window).
OWN_PIDS = {os.getpid()}

_names = {}  # app key -> name shown in the dashboard


def idle_seconds():
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType
    )


def screen_locked():
    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(session.get("CGSSessionScreenIsLocked", 0))


def display_rects():
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    rects = []
    for display in (ids or [])[:count]:
        b = Quartz.CGDisplayBounds(display)
        rects.append((b.origin.x, b.origin.y, b.origin.x + b.size.width, b.origin.y + b.size.height))
    return rects


def app_key(app):
    """(key, path, name) for an NSRunningApplication."""
    name = str(app.localizedName() or "")
    bundle_id = app.bundleIdentifier()
    key = str(bundle_id).lower() if bundle_id else (name.lower() + ".app" if name else "")
    url = app.bundleURL() or app.executableURL()
    path = str(url.path()) if url else None
    return key, path, name


def visible_apps():
    """{app key: app path} for every app with a window you can actually see,
    plus the focused app. Empty while the screen is locked."""
    if screen_locked():
        return {}
    found = {}

    def add(app):
        if app is None or app.processIdentifier() in OWN_PIDS:
            return
        key, path, name = app_key(app)
        if key:
            found[key] = path
            _names[key] = name or key

    add(NSWorkspace.sharedWorkspace().frontmostApplication())

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    windows = []  # front to back
    for w in info:
        if w.get(Quartz.kCGWindowLayer, 0) != 0:  # menu bar, Dock, overlays
            continue
        if w.get(Quartz.kCGWindowAlpha, 1) <= 0:
            continue
        b = w.get(Quartz.kCGWindowBounds) or {}
        x, y, width, height = b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0)
        if width <= 0 or height <= 0:
            continue
        windows.append((w.get(Quartz.kCGWindowOwnerPID), (x, y, x + width, y + height)))

    for pid in visible_owners(display_rects(), windows):
        if pid and pid not in OWN_PIDS:
            add(NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))
    return found


def app_name(key, path):
    return _names.get(key) or key


def memory_mb():
    """Memory of this process, as Activity Monitor roughly shows it."""
    try:
        rss_kb = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], timeout=5))
        return rss_kb / 1024
    except Exception:
        return 0.0


def dark_mode():
    return NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle") == "Dark"


# ---------------------------------------------------------------- dashboard window

def prepare_window():
    """Called in the dashboard window's process before the window opens.

    The app runs as a menu bar app (no Dock icon). While the dashboard window
    is open, show it in the Dock and app switcher like a normal app."""
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)


# ---------------------------------------------------------------- open at login

AGENT_LABEL = "com.husniheartnet.apptime"
AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")


def startup_enabled():
    return os.path.exists(AGENT_PATH)


def set_startup(enabled, command):
    if enabled:
        os.makedirs(os.path.dirname(AGENT_PATH), exist_ok=True)
        with open(AGENT_PATH, "wb") as f:
            plistlib.dump({
                "Label": AGENT_LABEL,
                "ProgramArguments": list(command),
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }, f)
    elif os.path.exists(AGENT_PATH):
        os.remove(AGENT_PATH)


def record_installed_version(version, exe):
    pass  # macOS reads the version from the app bundle itself
