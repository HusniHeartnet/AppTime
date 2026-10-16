//! Operating-system parts. Each platform module provides the same functions:
//! which apps are on screen, idle time, app names, memory use, dark mode,
//! starting at sign-in, and window-process setup.

use std::sync::Mutex;

#[cfg(windows)]
mod win;
#[cfg(windows)]
pub use win::*;

#[cfg(target_os = "macos")]
mod mac;
#[cfg(target_os = "macos")]
pub use mac::*;

/// Processes of this running AppTime (the tracker and its dashboard window):
/// their windows are never counted.
static OWN_PIDS: Mutex<Vec<u32>> = Mutex::new(Vec::new());

pub fn add_own_pid(pid: u32) {
    OWN_PIDS.lock().unwrap().push(pid);
}

pub fn is_own_pid(pid: u32) -> bool {
    pid == std::process::id() || OWN_PIDS.lock().unwrap().contains(&pid)
}

pub fn is_ignored(key: &str) -> bool {
    key.is_empty() || IGNORED.contains(&key)
}
