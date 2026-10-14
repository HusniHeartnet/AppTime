"""Windows parts of AppTime: which apps are on screen, idle time, the
dashboard window's web view, and starting with Windows.

platform_mac.py has the same functions for macOS.
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess

from geometry import area, visible_owners

APP_NAME = "AppTime"
DATA_ROOT = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
STARTUP_LABEL = "Start with Windows"
WINDOW_ICON = "AppTime.ico"  # classic-bitmap .ico: .NET garbles PNG-compressed icon images

# Foreground "apps" that aren't really you using an app.
# The lock and sign-in screens: nothing counts while they're up.
LOCK_EXES = {"lockapp.exe", "logonui.exe"}

# Windows' own tools and interface aren't "apps you spend time on", so they
# aren't counted at all (not as app time, not as screen time, never shared).
IGNORED = LOCK_EXES | {
    "",
    "apptime.exe",                   # AppTime's own dashboard
    # Windows tools
    "explorer.exe",                  # File Explorer and the desktop
    "taskmgr.exe",                   # Task Manager
    "systemsettings.exe",            # Settings
    "resmon.exe", "perfmon.exe",     # Resource / Performance Monitor
    "mmc.exe",                       # Device Manager, Disk Management, ...
    "control.exe",                   # Control Panel
    "openwith.exe", "pickerhost.exe",  # "Open with" and file picker dialogs
    "consent.exe",                   # admin permission (UAC) prompts
    "rundll32.exe", "dllhost.exe",   # helper processes behind system dialogs
    # Windows' own interface
    "searchhost.exe", "searchapp.exe", "searchui.exe",  # Start search
    "startmenuexperiencehost.exe",   # Start menu
    "shellexperiencehost.exe",       # notifications, clock, quick settings
    "shellhost.exe",
    "textinputhost.exe",             # touch keyboard, emoji picker
    "screenclippinghost.exe",        # snipping overlay
    "applicationframehost.exe",      # frame of Store apps (the app itself is counted)
    "lockapp.exe", "logonui.exe",
}

# Processes of this running AppTime (the tracker and its dashboard window).
OWN_PIDS = {os.getpid()}


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


def visible_apps():
    """{app key: exe path} for every app with a window you can actually see,
    plus the focused app. Empty while the lock screen is up."""
    paths = set()
    fg = user32.GetForegroundWindow()
    fg_path = window_exe_path(fg) if fg and window_pid(fg) not in OWN_PIDS else None
    if fg_path and os.path.basename(fg_path).lower() in LOCK_EXES:
        return {}  # lock screen / sign-in screen
    if fg_path:
        paths.add(fg_path)
    windows = [(hwnd, rect) for hwnd, rect in candidate_windows()]
    for hwnd in visible_owners(monitor_rects(), windows):
        if window_pid(hwnd) not in OWN_PIDS:
            path = window_exe_path(hwnd)
            if path:
                paths.add(path)
    return {os.path.basename(path).lower(): path for path in paths}


def app_name(key, path):
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


# ---------------------------------------------------------------- own resource usage

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


psapi = ctypes.WinDLL("psapi")
kernel32.GetCurrentProcess.restype = wt.HANDLE
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]

def memory_mb():
    """Memory of this process, as Task Manager shows it."""
    mem = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(mem), mem.cb)
    return mem.WorkingSetSize / 2**20


# ---------------------------------------------------------------- dashboard window

def dark_mode():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# Chromium options that keep the dashboard's web view small. The page is a
# simple local dashboard, so it doesn't need GPU acceleration, extensions,
# background services or spare processes.
WEBVIEW_ARGS = os.environ.get("APPTIME_WEBVIEW_ARGS", " ".join([
    "--disable-gpu",
    "--in-process-gpu",
    "--renderer-process-limit=1",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-features=ElasticOverscroll,msSmartScreenProtection,SpareRendererForSitePerProcess,"
    "AutofillServerCommunication",
    "--js-flags=--lite-mode",  # simpler JavaScript engine: plenty for this page, less memory
]))


def lean_webview():
    """Add WEBVIEW_ARGS to the options pywebview passes to WebView2.

    pywebview builds the options and starts WebView2 inside one constructor,
    with no setting for extra Chromium options. Right after it writes its own
    options it reads settings["ALLOW_FILE_URLS"], so that read is where ours
    are appended. (Local file URLs aren't needed: the dashboard is served over
    http from AppTime itself.)
    """
    try:
        from webview.platforms import edgechromium
    except Exception:
        return
    real_props = edgechromium.CoreWebView2CreationProperties
    created = []

    def make_props():
        props = real_props()
        created.append(props)
        return props

    class Settings:
        def __init__(self, inner):
            self.inner = inner

        def __getitem__(self, key):
            if key == "ALLOW_FILE_URLS" and created:
                props = created.pop()
                props.AdditionalBrowserArguments = f"{props.AdditionalBrowserArguments} {WEBVIEW_ARGS}".strip()
                return False
            return self.inner[key]

        def __getattr__(self, name):
            return getattr(self.inner, name)

    edgechromium.CoreWebView2CreationProperties = make_props
    edgechromium.webview_settings = Settings(edgechromium.webview_settings)

def prepare_window():
    """Called in the dashboard window's process before the window opens."""
    # Our own taskbar identity: otherwise Windows may group the window with
    # other apps that run on the same Python, and show their icon.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HusniHeartnet.AppTime")
    except Exception:
        pass
    lean_webview()


# ---------------------------------------------------------------- start with Windows

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled, command):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- installed copy

# Must match AppId in installer/AppTime.iss.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F3A2C4E-9B1D-4E7A-A5C2-3D8E1F0B7A94}_is1"


def record_installed_version(version, exe):
    """After an in-app update, keep the version shown in Settings -> Apps right."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            if os.path.normcase(os.path.abspath(location.rstrip("\\"))) == os.path.normcase(os.path.dirname(exe)):
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
    except OSError:
        pass  # not installed with the installer (portable copy)
