//! Windows: which apps are on screen, idle time, app names, memory, dark mode,
//! start with Windows, Settings > Apps version, and saving on shutdown.

use std::collections::HashMap;
use std::ffi::c_void;
use std::os::windows::ffi::OsStrExt;
use std::path::PathBuf;

use windows_sys::Win32::Foundation::{CloseHandle, BOOL, FILETIME, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS};
use windows_sys::Win32::Graphics::Gdi::{EnumDisplayMonitors, HDC, HMONITOR};
use windows_sys::Win32::Storage::FileSystem::{GetFileVersionInfoSizeW, GetFileVersionInfoW, VerQueryValueW};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
use windows_sys::Win32::System::SystemInformation::GetTickCount;
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetProcessTimes, OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
    PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows_sys::Win32::UI::HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
use windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, EnumChildWindows, EnumWindows, GetClassNameW,
    GetForegroundWindow, GetMessageW, GetWindowLongW, GetWindowRect, GetWindowThreadProcessId, IsIconic,
    IsWindowVisible, RegisterClassW, TranslateMessage, GWL_EXSTYLE, MSG, WM_ENDSESSION, WM_QUERYENDSESSION,
    WNDCLASSW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_OVERLAPPED,
};
use winreg::enums::{HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE};
use winreg::RegKey;

use crate::geometry::{area, visible_owners, Rect};

pub const STARTUP_LABEL: &str = "Start with Windows";

/// The lock and sign-in screens: nothing counts while they're up.
const LOCK_EXES: &[&str] = &["lockapp.exe", "logonui.exe"];

/// Windows' own tools and interface aren't "apps you spend time on", so they
/// aren't counted at all (not as app time, not as screen time, never shared).
pub const IGNORED: &[&str] = &[
    "apptime.exe",                 // AppTime's own dashboard
    "explorer.exe",                // File Explorer and the desktop
    "taskmgr.exe",                 // Task Manager
    "systemsettings.exe",          // Settings
    "resmon.exe", "perfmon.exe",   // Resource / Performance Monitor
    "mmc.exe",                     // Device Manager, Disk Management, ...
    "control.exe",                 // Control Panel
    "openwith.exe", "pickerhost.exe", // "Open with" and file picker dialogs
    "consent.exe",                 // admin permission (UAC) prompts
    "rundll32.exe", "dllhost.exe", // helper processes behind system dialogs
    "searchhost.exe", "searchapp.exe", "searchui.exe", // Start search
    "startmenuexperiencehost.exe", // Start menu
    "shellexperiencehost.exe",     // notifications, clock, quick settings
    "shellhost.exe",
    "textinputhost.exe",           // touch keyboard, emoji picker
    "screenclippinghost.exe",      // snipping overlay
    "applicationframehost.exe",    // frame of Store apps (the app itself is counted)
    "lockapp.exe", "logonui.exe",
];

const SHELL_CLASSES: &[&str] = &["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"];

// Must match AppId in installer/AppTime.iss.
const UNINSTALL_KEY: &str =
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F3A2C4E-9B1D-4E7A-A5C2-3D8E1F0B7A94}_is1";
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

fn wide(s: &str) -> Vec<u16> {
    std::ffi::OsStr::new(s).encode_wide().chain(Some(0)).collect()
}

pub fn init_process() {
    // Real pixel coordinates on scaled (125%, 150%...) displays.
    unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
}

/// `--selftest` prints to the console it was started from.
pub fn attach_console() {
    use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
    unsafe { AttachConsole(ATTACH_PARENT_PROCESS) };
}

pub fn data_root() -> PathBuf {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
}

// ------------------------------------------------------------------ input and processes

pub fn idle_seconds() -> f64 {
    let mut info = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
    unsafe {
        if GetLastInputInfo(&mut info) == 0 {
            return 0.0;
        }
        GetTickCount().wrapping_sub(info.dwTime) as f64 / 1000.0
    }
}

fn window_pid(hwnd: HWND) -> u32 {
    let mut pid = 0u32;
    unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
    pid
}

fn process_path(pid: u32) -> Option<String> {
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let mut buf = [0u16; 1024];
        let mut size = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buf.as_mut_ptr(), &mut size);
        CloseHandle(handle);
        (ok != 0).then(|| String::from_utf16_lossy(&buf[..size as usize]))
    }
}

fn exe_key(path: &str) -> String {
    path.rsplit('\\').next().unwrap_or(path).to_lowercase()
}

unsafe extern "system" fn first_other_child(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let state = &mut *(lparam as *mut (u32, u32)); // (frame pid, found child pid)
    let pid = window_pid(hwnd);
    if pid != 0 && pid != state.0 {
        state.1 = pid;
        return 0;
    }
    1
}

/// The exe behind a window. Store apps (Settings, Calculator, ...) live inside
/// ApplicationFrameHost; the real app is a child window owned by another process.
fn window_exe_path(hwnd: HWND) -> Option<String> {
    let pid = window_pid(hwnd);
    let path = process_path(pid)?;
    if exe_key(&path) == "applicationframehost.exe" {
        let mut state = (pid, 0u32);
        unsafe { EnumChildWindows(hwnd, Some(first_other_child), &mut state as *mut _ as LPARAM) };
        if state.1 != 0 {
            if let Some(child) = process_path(state.1) {
                return Some(child);
            }
        }
    }
    Some(path)
}

// ------------------------------------------------------------------ visible windows

unsafe extern "system" fn collect_monitor(_m: HMONITOR, _dc: HDC, rect: *mut RECT, lparam: LPARAM) -> BOOL {
    let out = &mut *(lparam as *mut Vec<Rect>);
    let r = &*rect;
    out.push((r.left as f64, r.top as f64, r.right as f64, r.bottom as f64));
    1
}

fn monitor_rects() -> Vec<Rect> {
    let mut rects: Vec<Rect> = Vec::new();
    unsafe {
        EnumDisplayMonitors(std::ptr::null_mut(), std::ptr::null(), Some(collect_monitor), &mut rects as *mut _ as LPARAM)
    };
    rects
}

fn window_rect(hwnd: HWND) -> Rect {
    let mut r = RECT { left: 0, top: 0, right: 0, bottom: 0 };
    unsafe {
        // The frame bounds exclude Windows 10/11's invisible resize borders.
        let hr = DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS as u32,
            &mut r as *mut _ as *mut c_void,
            std::mem::size_of::<RECT>() as u32,
        );
        if hr != 0 {
            GetWindowRect(hwnd, &mut r);
        }
    }
    (r.left as f64, r.top as f64, r.right as f64, r.bottom as f64)
}

/// Top-level app windows that could be on screen, front to back.
unsafe extern "system" fn collect_window(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let out = &mut *(lparam as *mut Vec<(usize, Rect)>);
    if IsWindowVisible(hwnd) == 0 || IsIconic(hwnd) != 0 {
        return 1;
    }
    let ex = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
    if ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE) != 0 {
        return 1;
    }
    // Cloaked = hidden by Windows: other virtual desktops, suspended Store apps.
    let mut cloaked = 0u32;
    if DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED as u32, &mut cloaked as *mut _ as *mut c_void, 4) == 0 && cloaked != 0 {
        return 1;
    }
    let mut cls = [0u16; 256];
    let n = GetClassNameW(hwnd, cls.as_mut_ptr(), cls.len() as i32);
    let class = String::from_utf16_lossy(&cls[..n.max(0) as usize]);
    if SHELL_CLASSES.contains(&class.as_str()) {
        return 1;
    }
    let rect = window_rect(hwnd);
    if area(rect) > 0.0 {
        out.push((hwnd as usize, rect));
    }
    1
}

/// {app key: exe path} for every app with a window you can actually see, plus
/// the focused app. Empty while the lock screen is up.
pub fn visible_apps() -> HashMap<String, Option<String>> {
    let mut found: HashMap<String, Option<String>> = HashMap::new();
    unsafe {
        let fg = GetForegroundWindow();
        if !fg.is_null() && !super::is_own_pid(window_pid(fg)) {
            if let Some(path) = window_exe_path(fg) {
                if LOCK_EXES.contains(&exe_key(&path).as_str()) {
                    return HashMap::new();
                }
                found.insert(exe_key(&path), Some(path));
            }
        }
        let mut windows: Vec<(usize, Rect)> = Vec::new();
        EnumWindows(Some(collect_window), &mut windows as *mut _ as LPARAM);
        for hwnd in visible_owners(monitor_rects(), &windows) {
            let hwnd = hwnd as HWND;
            if super::is_own_pid(window_pid(hwnd)) {
                continue;
            }
            if let Some(path) = window_exe_path(hwnd) {
                found.insert(exe_key(&path), Some(path));
            }
        }
    }
    found
}

/// The "File description" from the exe's version info, e.g. "Visual Studio Code".
pub fn app_name(key: &str, path: Option<&str>) -> String {
    let fallback = key.trim_end_matches(".exe").to_string();
    let Some(path) = path else { return fallback };
    let wpath = wide(path);
    unsafe {
        let size = GetFileVersionInfoSizeW(wpath.as_ptr(), std::ptr::null_mut());
        if size == 0 {
            return fallback;
        }
        let mut data = vec![0u8; size as usize];
        if GetFileVersionInfoW(wpath.as_ptr(), 0, size, data.as_mut_ptr() as *mut c_void) == 0 {
            return fallback;
        }
        let mut langs: Vec<(u16, u16)> = vec![(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)];
        let mut ptr: *mut c_void = std::ptr::null_mut();
        let mut len = 0u32;
        let translation = wide("\\VarFileInfo\\Translation");
        if VerQueryValueW(data.as_ptr() as *const c_void, translation.as_ptr(), &mut ptr, &mut len) != 0 && len >= 4 {
            let pair = ptr as *const u16;
            langs.insert(0, (*pair, *pair.add(1)));
        }
        for (lang, codepage) in langs {
            let query = wide(&format!("\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"));
            if VerQueryValueW(data.as_ptr() as *const c_void, query.as_ptr(), &mut ptr, &mut len) != 0 && len > 1 {
                let text = std::slice::from_raw_parts(ptr as *const u16, (len - 1) as usize);
                let text = String::from_utf16_lossy(text).trim().to_string();
                if !text.is_empty() {
                    return text;
                }
            }
        }
    }
    fallback
}

// ------------------------------------------------------------------ own resource usage

/// Memory of this process, as Task Manager shows it (working set).
pub fn memory_mb() -> f64 {
    unsafe {
        let mut counters: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
        counters.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        if GetProcessMemoryInfo(GetCurrentProcess(), &mut counters, counters.cb) == 0 {
            return 0.0;
        }
        counters.WorkingSetSize as f64 / 1048576.0
    }
}

/// CPU time this process has used, in seconds.
pub fn cpu_seconds() -> f64 {
    let zero = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
    let (mut created, mut exited, mut kernel, mut user) = (zero, zero, zero, zero);
    unsafe {
        if GetProcessTimes(GetCurrentProcess(), &mut created, &mut exited, &mut kernel, &mut user) == 0 {
            return 0.0;
        }
    }
    let ticks = |t: FILETIME| ((t.dwHighDateTime as u64) << 32 | t.dwLowDateTime as u64) as f64;
    (ticks(kernel) + ticks(user)) / 10_000_000.0
}

pub fn dark_mode() -> bool {
    RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        .and_then(|k| k.get_value::<u32, _>("AppsUseLightTheme"))
        .map(|v| v == 0)
        .unwrap_or(false)
}

// ------------------------------------------------------------------ start with Windows

fn command_line(command: &[String]) -> String {
    command
        .iter()
        .map(|a| if a.contains(' ') { format!("\"{a}\"") } else { a.clone() })
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn startup_enabled() -> bool {
    RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(RUN_KEY)
        .and_then(|k| k.get_value::<String, _>("AppTime"))
        .is_ok()
}

pub fn set_startup(enabled: bool, command: &[String]) -> std::io::Result<()> {
    let (key, _) = RegKey::predef(HKEY_CURRENT_USER).create_subkey(RUN_KEY)?;
    if enabled {
        key.set_value("AppTime", &command_line(command))
    } else {
        match key.delete_value("AppTime") {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            other => other,
        }
    }
}

/// After an in-app update, keep the version shown in Settings > Apps right.
pub fn record_installed_version(version: &str, exe: &std::path::Path) {
    let Ok(key) = RegKey::predef(HKEY_CURRENT_USER).open_subkey_with_flags(UNINSTALL_KEY, KEY_QUERY_VALUE | KEY_SET_VALUE)
    else {
        return; // not installed with the installer
    };
    let Ok(location) = key.get_value::<String, _>("InstallLocation") else { return };
    let same = |a: &std::path::Path, b: &std::path::Path| {
        a.to_string_lossy().trim_end_matches('\\').to_lowercase() == b.to_string_lossy().trim_end_matches('\\').to_lowercase()
    };
    if let Some(dir) = exe.parent() {
        if same(std::path::Path::new(&location), dir) {
            key.set_value("DisplayVersion", &version.to_string()).ok();
        }
    }
}

// ------------------------------------------------------------------ dashboard window

/// Chromium options that keep the dashboard's web view small: a simple local
/// page doesn't need GPU acceleration, extensions, background services or spare processes.
pub const WEBVIEW_ARGS: &str = "--disable-gpu --in-process-gpu --renderer-process-limit=1 --disable-extensions \
    --disable-background-networking --disable-component-update \
    --disable-features=ElasticOverscroll,msSmartScreenProtection,SpareRendererForSitePerProcess,AutofillServerCommunication \
    --js-flags=--lite-mode";

/// Called in the dashboard window's process before the window opens: our own
/// taskbar identity, so Windows doesn't group the window with other apps.
pub fn prepare_window_process() {
    let id = wide("HusniHeartnet.AppTime");
    unsafe { SetCurrentProcessExplicitAppUserModelID(id.as_ptr()) };
}

// ------------------------------------------------------------------ shutdown / restart

static mut ON_SESSION_END: Option<fn()> = None;

unsafe extern "system" fn session_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_QUERYENDSESSION => {
            // Windows asks whether it may shut down: save first, then say yes.
            if let Some(save) = ON_SESSION_END {
                save();
            }
            1
        }
        WM_ENDSESSION => {
            if wparam != 0 {
                if let Some(save) = ON_SESSION_END {
                    save();
                }
            }
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

/// Save the latest tracked time when Windows shuts down, restarts or signs out.
/// Uses a hidden top-level window, because only those are asked by Windows.
pub fn watch_session_end(save: fn()) {
    unsafe { ON_SESSION_END = Some(save) };
    std::thread::spawn(|| unsafe {
        let class = wide("AppTimeSessionWatcher");
        let instance = GetModuleHandleW(std::ptr::null());
        let wc = WNDCLASSW {
            lpfnWndProc: Some(session_proc),
            hInstance: instance,
            lpszClassName: class.as_ptr(),
            ..std::mem::zeroed()
        };
        RegisterClassW(&wc);
        let hwnd = CreateWindowExW(
            0, class.as_ptr(), class.as_ptr(), WS_OVERLAPPED, 0, 0, 0, 0,
            std::ptr::null_mut(), std::ptr::null_mut(), instance, std::ptr::null(),
        );
        if hwnd.is_null() {
            return;
        }
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    });
}
