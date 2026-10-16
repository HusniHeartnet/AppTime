//! AppTime background tracker, Rust prototype (Windows).
//!
//! Same logic as the Python tracker (platform_win.py + geometry.py + the
//! Tracker class): every second, find the apps whose windows you can actually
//! see, skip Windows' own tools, stop counting when idle or locked, and save to
//! SQLite every 30 seconds. Plus a tray icon with Quit.
//!
//! Not included yet: dashboard, sharing, updates, autostart.
//! Writes to %LOCALAPPDATA%\AppTime-prototype\apptime.db, never to AppTime's real data.
//!
//!   AppTimeRs.exe              run in the tray
//!   AppTimeRs.exe --selftest   print what it sees on screen right now, then exit

#![windows_subsystem = "windows"]

use std::collections::{HashMap, HashSet};
use std::ffi::c_void;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use chrono::{Local, Timelike};
use rusqlite::{params, Connection};
use tray_icon::menu::{Menu, MenuEvent, MenuItem};
use tray_icon::{Icon, TrayIconBuilder};
use windows_sys::Win32::Foundation::{CloseHandle, BOOL, HWND, LPARAM, RECT};
use windows_sys::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS};
use windows_sys::Win32::Graphics::Gdi::{EnumDisplayMonitors, HDC, HMONITOR};
use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
use windows_sys::Win32::System::SystemInformation::GetTickCount;
use windows_sys::Win32::System::Threading::{
    OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows_sys::Win32::UI::HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, EnumChildWindows, EnumWindows, GetClassNameW, GetForegroundWindow, GetMessageW,
    GetWindowLongW, GetWindowRect, GetWindowThreadProcessId, IsIconic, IsWindowVisible, PostQuitMessage,
    TranslateMessage, GWL_EXSTYLE, MSG, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT,
};

const POLL: Duration = Duration::from_secs(1);
const FLUSH_EVERY: Duration = Duration::from_secs(30);
const MAX_GAP_SECONDS: f64 = 5.0;
const IDLE_SECONDS: f64 = 5.0 * 60.0;
const MIN_VISIBLE_FRACTION: f64 = 0.15;
const MIN_VISIBLE_PIXELS: i64 = 200 * 200;

const LOCK_EXES: &[&str] = &["lockapp.exe", "logonui.exe"];
const IGNORED_EXES: &[&str] = &[
    "apptime.exe", "apptimers.exe", "explorer.exe", "taskmgr.exe", "systemsettings.exe", "resmon.exe",
    "perfmon.exe", "mmc.exe", "control.exe", "openwith.exe", "pickerhost.exe", "consent.exe", "rundll32.exe",
    "dllhost.exe", "searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "shellhost.exe", "textinputhost.exe", "screenclippinghost.exe",
    "applicationframehost.exe", "lockapp.exe", "logonui.exe",
];
const SHELL_CLASSES: &[&str] = &["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"];

type Rect = (i32, i32, i32, i32); // left, top, right, bottom

// ------------------------------------------------------------------ geometry (same as geometry.py)

fn area(r: Rect) -> i64 {
    ((r.2 - r.0).max(0) as i64) * ((r.3 - r.1).max(0) as i64)
}

fn intersect(a: Rect, b: Rect) -> Rect {
    (a.0.max(b.0), a.1.max(b.1), a.2.min(b.2), a.3.min(b.3))
}

fn subtract(rects: &[Rect], cut: Rect) -> Vec<Rect> {
    let mut out = Vec::new();
    for &r in rects {
        let i = intersect(r, cut);
        if area(i) == 0 {
            out.push(r);
            continue;
        }
        let (left, top, right, bottom) = r;
        if i.1 > top { out.push((left, top, right, i.1)); }
        if i.3 < bottom { out.push((left, i.3, right, bottom)); }
        if i.0 > left { out.push((left, i.1, i.0, i.3)); }
        if i.2 < right { out.push((i.2, i.1, right, i.3)); }
    }
    out
}

// ------------------------------------------------------------------ Win32 (same as platform_win.py)

fn idle_seconds() -> f64 {
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
        if ok == 0 {
            return None;
        }
        Some(String::from_utf16_lossy(&buf[..size as usize]))
    }
}

fn exe_name(path: &str) -> String {
    path.rsplit('\\').next().unwrap_or(path).to_lowercase()
}

unsafe extern "system" fn first_other_child(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let state = &mut *(lparam as *mut (u32, u32)); // (parent pid, found child pid)
    let pid = window_pid(hwnd);
    if pid != 0 && pid != state.0 {
        state.1 = pid;
        return 0;
    }
    1
}

/// The exe behind a window. Store apps live inside ApplicationFrameHost;
/// the real app is a child window owned by another process.
fn window_exe_path(hwnd: HWND) -> Option<String> {
    let pid = window_pid(hwnd);
    let path = process_path(pid)?;
    if exe_name(&path) == "applicationframehost.exe" {
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

unsafe extern "system" fn collect_monitor(_m: HMONITOR, _dc: HDC, rect: *mut RECT, lparam: LPARAM) -> BOOL {
    let out = &mut *(lparam as *mut Vec<Rect>);
    let r = &*rect;
    out.push((r.left, r.top, r.right, r.bottom));
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
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS as u32, &mut r as *mut _ as *mut c_void, std::mem::size_of::<RECT>() as u32,
        );
        if hr != 0 {
            GetWindowRect(hwnd, &mut r);
        }
    }
    (r.left, r.top, r.right, r.bottom)
}

unsafe extern "system" fn collect_window(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let out = &mut *(lparam as *mut Vec<(HWND, Rect)>);
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
    if area(rect) > 0 {
        out.push((hwnd, rect));
    }
    1
}

/// {exe name: path} for every app with a window you can actually see, plus the
/// focused app. Empty while the lock screen is up.
fn visible_apps(own_pid: u32) -> HashMap<String, String> {
    let mut paths: HashSet<String> = HashSet::new();
    unsafe {
        let fg = GetForegroundWindow();
        if !fg.is_null() && window_pid(fg) != own_pid {
            if let Some(p) = window_exe_path(fg) {
                if LOCK_EXES.contains(&exe_name(&p).as_str()) {
                    return HashMap::new();
                }
                paths.insert(p);
            }
        }
        let mut windows: Vec<(HWND, Rect)> = Vec::new();
        EnumWindows(Some(collect_window), &mut windows as *mut _ as LPARAM);
        let mut uncovered = monitor_rects();
        for (hwnd, rect) in windows {
            if uncovered.is_empty() {
                break;
            }
            let shown: i64 = uncovered.iter().map(|r| area(intersect(*r, rect))).sum();
            if shown >= MIN_VISIBLE_PIXELS && shown as f64 >= MIN_VISIBLE_FRACTION * area(rect) as f64 && window_pid(hwnd) != own_pid {
                if let Some(p) = window_exe_path(hwnd) {
                    paths.insert(p);
                }
            }
            uncovered = subtract(&uncovered, rect);
        }
    }
    paths.into_iter().map(|p| (exe_name(&p), p)).collect()
}

// ------------------------------------------------------------------ storage + tracker

fn data_dir() -> PathBuf {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into());
    PathBuf::from(base).join("AppTime-prototype")
}

fn open_db() -> Connection {
    std::fs::create_dir_all(data_dir()).ok();
    let conn = Connection::open(data_dir().join("apptime.db")).expect("open database");
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, hour INTEGER NOT NULL, exe TEXT NOT NULL,
             seconds REAL NOT NULL, PRIMARY KEY (day, hour, exe));
         CREATE TABLE IF NOT EXISTS screen (day TEXT NOT NULL, hour INTEGER NOT NULL, seconds REAL NOT NULL,
             PRIMARY KEY (day, hour));
         CREATE TABLE IF NOT EXISTS apps (exe TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT);",
    )
    .expect("create tables");
    conn
}

type Pending = HashMap<(String, u32, Option<String>), f64>; // (day, hour, exe or None for screen time)

fn flush(conn: &mut Connection, pending: &mut Pending) {
    if pending.is_empty() {
        return;
    }
    let tx = conn.transaction().expect("transaction");
    for ((day, hour, exe), secs) in pending.drain() {
        match exe {
            Some(exe) => tx.execute(
                "INSERT INTO usage (day, hour, exe, seconds) VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT (day, hour, exe) DO UPDATE SET seconds = seconds + excluded.seconds",
                params![day, hour, exe, secs],
            ),
            None => tx.execute(
                "INSERT INTO screen (day, hour, seconds) VALUES (?1, ?2, ?3)
                 ON CONFLICT (day, hour) DO UPDATE SET seconds = seconds + excluded.seconds",
                params![day, hour, secs],
            ),
        }
        .expect("write");
    }
    tx.commit().expect("commit");
}

fn track(stop: Arc<AtomicBool>) {
    let own_pid = std::process::id();
    let mut conn = open_db();
    let mut pending: Pending = HashMap::new();
    let mut known: HashSet<String> = HashSet::new();
    let mut last = Instant::now();
    let mut last_flush = last;
    while !stop.load(Ordering::Relaxed) {
        thread::sleep(POLL);
        let now = Instant::now();
        let gap = now.duration_since(last).as_secs_f64();
        last = now;
        if gap > MAX_GAP_SECONDS || idle_seconds() >= IDLE_SECONDS {
            continue; // asleep or away
        }
        let apps: Vec<(String, String)> = visible_apps(own_pid)
            .into_iter()
            .filter(|(exe, _)| !IGNORED_EXES.contains(&exe.as_str()))
            .collect();
        if apps.is_empty() {
            continue;
        }
        let stamp = Local::now();
        let day = stamp.format("%Y-%m-%d").to_string();
        let hour = stamp.hour();
        for (exe, path) in &apps {
            if known.insert(exe.clone()) {
                let name = exe.trim_end_matches(".exe").to_string();
                conn.execute("INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?1, ?2, ?3)", params![exe, name, path])
                    .ok();
            }
            *pending.entry((day.clone(), hour, Some(exe.clone()))).or_insert(0.0) += gap;
        }
        *pending.entry((day, hour, None)).or_insert(0.0) += gap;
        if now.duration_since(last_flush) >= FLUSH_EVERY {
            flush(&mut conn, &mut pending);
            last_flush = now;
        }
    }
    flush(&mut conn, &mut pending);
}

// ------------------------------------------------------------------ tray + main

fn tray_icon() -> Icon {
    let decoder = png::Decoder::new(&include_bytes!("../../assets/tray.png")[..]);
    let mut reader = decoder.read_info().expect("tray.png");
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).expect("tray.png frame");
    Icon::from_rgba(buf[..info.buffer_size()].to_vec(), info.width, info.height).expect("tray icon")
}

fn main() {
    unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };

    if std::env::args().any(|a| a == "--selftest") {
        unsafe { AttachConsole(ATTACH_PARENT_PROCESS) };
        let mut apps: Vec<String> = visible_apps(std::process::id())
            .into_keys()
            .filter(|e| !IGNORED_EXES.contains(&e.as_str()))
            .collect();
        apps.sort();
        println!("idle seconds: {:.1}", idle_seconds());
        println!("apps on screen: {:?}", apps);
        let mut conn = open_db();
        flush(&mut conn, &mut HashMap::new());
        println!("database: {}", data_dir().join("apptime.db").display());
        println!("selftest OK");
        return;
    }

    let stop = Arc::new(AtomicBool::new(false));
    let tracker = {
        let stop = stop.clone();
        thread::spawn(move || track(stop))
    };

    let menu = Menu::new();
    let quit = MenuItem::new("Quit AppTime prototype", true, None);
    menu.append(&quit).expect("menu");
    let _tray = TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_tooltip("AppTime (Rust prototype)")
        .with_icon(tray_icon())
        .build()
        .expect("tray icon");

    // Windows message loop: keeps the tray icon working.
    unsafe {
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
            while let Ok(event) = MenuEvent::receiver().try_recv() {
                if event.id == quit.id() {
                    PostQuitMessage(0);
                }
            }
        }
    }
    stop.store(true, Ordering::Relaxed);
    tracker.join().ok();
}
