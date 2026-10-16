//! macOS: which apps are on screen, idle time, app names, memory, dark mode
//! and Open at Login. Uses only information macOS gives any app without
//! asking for permissions: the list of on-screen windows with their owner
//! app and position (not their titles or contents), and the time since the
//! last keyboard or mouse input.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::display::CGDisplay;
use core_graphics::geometry::CGRect;
use core_graphics::window::{copy_window_info, kCGNullWindowID, kCGWindowListExcludeDesktopElements, kCGWindowListOptionOnScreenOnly};
use objc2_app_kit::{NSRunningApplication, NSWorkspace};
use objc2_foundation::{NSString, NSUserDefaults};

use crate::geometry::{visible_owners, Rect};

pub const STARTUP_LABEL: &str = "Open at Login";

/// macOS's own tools and interface aren't "apps you spend time on", so they
/// aren't counted at all. Keys are lowercase bundle identifiers.
pub const IGNORED: &[&str] = &[
    "com.husniheartnet.apptime",       // AppTime's own dashboard
    "com.apple.finder",                // Finder and the desktop
    "com.apple.activitymonitor",       // Activity Monitor
    "com.apple.systempreferences",     // System Settings
    "com.apple.systemsettings",
    "com.apple.dock",                  // Dock, Launchpad, Mission Control
    "com.apple.controlcenter",         // Control Center, menu bar items
    "com.apple.notificationcenterui",  // notifications, widgets
    "com.apple.spotlight",             // Spotlight search
    "com.apple.systemuiserver",
    "com.apple.windowmanager",         // Stage Manager
    "com.apple.screencaptureui",       // screenshot toolbar
    "com.apple.screensaver.engine",    // screen saver
    "com.apple.loginwindow",           // lock and login screens
    "com.apple.securityagent",         // password prompts
    "com.apple.coreservices.uiagent",  // "are you sure you want to open" dialogs
    "com.apple.universalaccessauthwarn",
    "com.apple.accessibilityuiserver",   // accessibility overlays
];

pub const WEBVIEW_ARGS: &str = "";

static NAMES: Mutex<Option<HashMap<String, String>>> = Mutex::new(None);

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGEventSourceSecondsSinceLastEventType(state: i32, event_type: u32) -> f64;
    fn CGSessionCopyCurrentDictionary() -> CFDictionaryRef;
}

pub fn init_process() {}

pub fn attach_console() {}

pub fn data_root() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(".")).join("Library/Application Support")
}

pub fn idle_seconds() -> f64 {
    // Combined session state, any input event.
    unsafe { CGEventSourceSecondsSinceLastEventType(0, u32::MAX) }
}

fn screen_locked() -> bool {
    unsafe {
        let raw = CGSessionCopyCurrentDictionary();
        if raw.is_null() {
            return false;
        }
        let dict: CFDictionary<CFString, CFType> = CFDictionary::wrap_under_create_rule(raw);
        dict.find(CFString::from_static_string("CGSSessionScreenIsLocked"))
            .map(|v| {
                v.downcast::<CFBoolean>().map(bool::from).or_else(|| v.downcast::<CFNumber>().and_then(|n| n.to_i64()).map(|n| n != 0))
                    .unwrap_or(false)
            })
            .unwrap_or(false)
    }
}

fn display_rects() -> Vec<Rect> {
    CGDisplay::active_displays()
        .unwrap_or_default()
        .into_iter()
        .map(|id| {
            let b = CGDisplay::new(id).bounds();
            (b.origin.x, b.origin.y, b.origin.x + b.size.width, b.origin.y + b.size.height)
        })
        .collect()
}

/// (key, path, name) for a running app.
fn app_info(app: &NSRunningApplication) -> (String, Option<String>, String) {
    unsafe {
        let name = app.localizedName().map(|n| n.to_string()).unwrap_or_default();
        let key = match app.bundleIdentifier() {
            Some(id) => id.to_string().to_lowercase(),
            None if !name.is_empty() => format!("{}.app", name.to_lowercase()),
            None => String::new(),
        };
        let path = app.bundleURL().or_else(|| app.executableURL()).and_then(|u| u.path()).map(|p| p.to_string());
        (key, path, name)
    }
}

/// {app key: app path} for every app with a window you can actually see, plus
/// the focused app. Empty while the screen is locked.
pub fn visible_apps() -> HashMap<String, Option<String>> {
    objc2::rc::autoreleasepool(|_| {
        if screen_locked() {
            return HashMap::new();
        }
        let mut found: HashMap<String, Option<String>> = HashMap::new();
        let mut names = NAMES.lock().unwrap();
        let names = names.get_or_insert_with(HashMap::new);
        let mut add = |app: &NSRunningApplication| {
            let pid = unsafe { app.processIdentifier() } as u32;
            if super::is_own_pid(pid) {
                return;
            }
            let (key, path, name) = app_info(app);
            if !key.is_empty() {
                names.insert(key.clone(), if name.is_empty() { key.clone() } else { name });
                found.insert(key, path);
            }
        };

        unsafe {
            if let Some(front) = NSWorkspace::sharedWorkspace().frontmostApplication() {
                add(&front);
            }
        }

        // On-screen windows, front to back. Layer 0 is normal app windows
        // (the menu bar, Dock and overlays are on other layers).
        let mut windows: Vec<(i64, Rect)> = Vec::new();
        if let Some(list) = copy_window_info(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID) {
            for item in list.iter() {
                let dict: CFDictionary<CFString, CFType> = unsafe { CFDictionary::wrap_under_get_rule(*item as CFDictionaryRef) };
                let number = |key: &'static str| {
                    dict.find(CFString::from_static_string(key)).and_then(|v| v.downcast::<CFNumber>()).and_then(|n| n.to_f64())
                };
                if number("kCGWindowLayer").unwrap_or(0.0) != 0.0 || number("kCGWindowAlpha").unwrap_or(1.0) <= 0.0 {
                    continue;
                }
                let Some(pid) = number("kCGWindowOwnerPID") else { continue };
                let bounds = dict
                    .find(CFString::from_static_string("kCGWindowBounds"))
                    .and_then(|v| v.downcast::<CFDictionary>())
                    .and_then(|d| CGRect::from_dict_representation(&d));
                let Some(b) = bounds else { continue };
                if b.size.width <= 0.0 || b.size.height <= 0.0 {
                    continue;
                }
                windows.push((pid as i64, (b.origin.x, b.origin.y, b.origin.x + b.size.width, b.origin.y + b.size.height)));
            }
        }
        for pid in visible_owners(display_rects(), &windows) {
            if let Some(app) = unsafe { NSRunningApplication::runningApplicationWithProcessIdentifier(pid as i32) } {
                add(&app);
            }
        }
        found
    })
}

pub fn app_name(key: &str, _path: Option<&str>) -> String {
    NAMES.lock().unwrap().as_ref().and_then(|m| m.get(key).cloned()).unwrap_or_else(|| key.to_string())
}

/// Memory of this process, roughly as Activity Monitor shows it.
pub fn memory_mb() -> f64 {
    std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", &std::process::id().to_string()])
        .output()
        .ok()
        .and_then(|o| String::from_utf8_lossy(&o.stdout).trim().parse::<f64>().ok())
        .map(|kb| kb / 1024.0)
        .unwrap_or(0.0)
}

/// CPU time this process has used, in seconds.
pub fn cpu_seconds() -> f64 {
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) != 0 {
            return 0.0;
        }
        let t = |tv: libc::timeval| tv.tv_sec as f64 + tv.tv_usec as f64 / 1e6;
        t(usage.ru_utime) + t(usage.ru_stime)
    }
}

pub fn dark_mode() -> bool {
    unsafe {
        NSUserDefaults::standardUserDefaults()
            .stringForKey(&NSString::from_str("AppleInterfaceStyle"))
            .map(|s| s.to_string() == "Dark")
            .unwrap_or(false)
    }
}

// ------------------------------------------------------------------ open at login

const AGENT_LABEL: &str = "com.husniheartnet.apptime";

fn agent_path() -> PathBuf {
    data_root().parent().map(|p| p.join("LaunchAgents")).unwrap_or_default().join(format!("{AGENT_LABEL}.plist"))
}

pub fn startup_enabled() -> bool {
    agent_path().exists()
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn set_startup(enabled: bool, command: &[String]) -> std::io::Result<()> {
    let path = agent_path();
    if !enabled {
        return match std::fs::remove_file(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            other => other,
        };
    }
    let args: String = command.iter().map(|a| format!("        <string>{}</string>\n", xml_escape(a))).collect();
    let plist = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
         <plist version=\"1.0\">\n<dict>\n    <key>Label</key>\n    <string>{AGENT_LABEL}</string>\n\
         \x20   <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n\
         \x20   <key>RunAtLoad</key>\n    <true/>\n    <key>ProcessType</key>\n    <string>Interactive</string>\n</dict>\n</plist>\n"
    );
    std::fs::create_dir_all(path.parent().unwrap())?;
    std::fs::write(path, plist)
}

pub fn record_installed_version(_version: &str, _exe: &std::path::Path) {
    // macOS reads the version from the app bundle itself.
}

pub fn prepare_window_process() {}

pub fn watch_session_end(_save: fn()) {}
