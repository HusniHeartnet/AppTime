//! Checks GitHub for a newer AppTime and installs it.
//!
//! Windows: downloads the installer (AppTime-Setup.exe) and runs it silently; it
//! closes this copy, upgrades the installed AppTime and starts it again.
//! macOS: swaps the AppTime.app bundle for the new one and starts it.
//! The download is checked against the size and SHA-256 checksum GitHub
//! publishes for the release file before anything is replaced.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::APP_VERSION;

const REPO: &str = "HusniHeartnet/AppTime";
pub const RELEASES_PAGE: &str = "https://github.com/HusniHeartnet/AppTime/releases/latest";
const CHECK_EVERY: Duration = Duration::from_secs(12 * 3600);
#[cfg(target_os = "macos")]
const ASSET_NAME: &str = "AppTime-mac.zip";
#[cfg(not(target_os = "macos"))]
const ASSET_NAME: &str = "AppTime-Setup.exe";

fn api_url() -> String {
    std::env::var("APPTIME_UPDATE_API").unwrap_or_else(|_| format!("https://api.github.com/repos/{REPO}/releases/latest"))
}

fn version_tuple(text: &str) -> Vec<u64> {
    text.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()).take(3).filter_map(|s| s.parse().ok()).collect()
}

/// The AppTime.exe / AppTime.app this copy runs from, or None when running from a dev build.
pub fn installed_location() -> Option<PathBuf> {
    // APPTIME_DEV: a test build, so don't touch autostart or install updates.
    if cfg!(debug_assertions) || std::env::var_os("APPTIME_DEV").is_some() {
        return None;
    }
    let exe = std::env::current_exe().ok()?;
    if cfg!(target_os = "macos") {
        let bundle = exe.parent()?.parent()?.parent()?.to_path_buf();
        return bundle.extension().map(|e| e == "app").unwrap_or(false).then_some(bundle);
    }
    Some(exe)
}

/// After an update on macOS: delete the previous version left next to this one.
pub fn remove_old_copy() {
    if let Some(location) = installed_location() {
        let old = location.with_file_name(format!(
            "AppTime.old.{}",
            location.extension().and_then(|e| e.to_str()).unwrap_or("app")
        ));
        if old.is_dir() {
            std::fs::remove_dir_all(old).ok();
        } else if old.exists() {
            std::fs::remove_file(old).ok();
        }
    }
}

#[derive(Default)]
struct State {
    latest: Option<String>,
    notes_url: Option<String>,
    asset: Option<Value>,
    status: String, // idle | downloading | installing | error
    message: String,
    progress: f64,
}

pub struct Updater {
    state: Mutex<State>,
    busy: AtomicBool,
    restart: fn(Vec<String>),
}

impl Updater {
    pub fn new(restart: fn(Vec<String>)) -> Self {
        Updater {
            state: Mutex::new(State { status: "idle".into(), ..Default::default() }),
            busy: AtomicBool::new(false),
            restart,
        }
    }

    fn agent() -> ureq::Agent {
        ureq::AgentBuilder::new().timeout_connect(Duration::from_secs(20)).build()
    }

    pub fn check(&self) -> Result<(), String> {
        let release: Value = Self::agent()
            .get(&api_url())
            .set("Accept", "application/vnd.github+json")
            .set("User-Agent", &format!("AppTime/{APP_VERSION}"))
            .timeout(Duration::from_secs(20))
            .call()
            .map_err(|e| e.to_string())?
            .into_json()
            .map_err(|e| e.to_string())?;
        let mut s = self.state.lock().unwrap();
        s.latest = release["tag_name"].as_str().map(|t| t.trim_start_matches('v').to_string()).filter(|t| !t.is_empty());
        s.notes_url = release["html_url"].as_str().map(String::from);
        s.asset = release["assets"].as_array().and_then(|a| a.iter().find(|x| x["name"] == ASSET_NAME).cloned());
        Ok(())
    }

    pub fn check_quietly(&self) {
        self.check().ok();
    }

    pub fn run_checks(&self, stopped: &AtomicBool) {
        std::thread::sleep(Duration::from_secs(20)); // let startup settle
        while !stopped.load(Ordering::Relaxed) {
            self.check_quietly();
            std::thread::sleep(CHECK_EVERY);
        }
    }

    fn available_locked(s: &State) -> bool {
        match (&s.latest, &s.asset) {
            (Some(latest), Some(_)) => version_tuple(latest) > version_tuple(APP_VERSION),
            _ => false,
        }
    }

    pub fn notes_url(&self) -> String {
        self.state.lock().unwrap().notes_url.clone().unwrap_or_else(|| RELEASES_PAGE.into())
    }

    pub fn can_install(&self) -> bool {
        let Some(location) = installed_location() else { return false };
        if cfg!(target_os = "macos") {
            if location.to_string_lossy().contains("/AppTranslocation/") {
                return false; // macOS runs un-moved downloads from a read-only copy
            }
            return location.parent().map(|p| !p.metadata().map(|m| m.permissions().readonly()).unwrap_or(true)).unwrap_or(false);
        }
        true // Windows: the installer takes care of it
    }

    pub fn state(&self) -> Value {
        let s = self.state.lock().unwrap();
        json!({
            "current": APP_VERSION,
            "latest": s.latest,
            "available": Self::available_locked(&s),
            "can_install": self.can_install(),
            "notes_url": s.notes_url.clone().unwrap_or_else(|| RELEASES_PAGE.into()),
            "status": s.status,
            "message": s.message,
            "progress": (s.progress * 1000.0).round() / 1000.0,
        })
    }

    pub fn latest_known(&self) -> (Option<String>, bool) {
        let s = self.state.lock().unwrap();
        (s.latest.clone(), s.asset.is_some())
    }

    pub fn start_install(&'static self) {
        {
            let mut s = self.state.lock().unwrap();
            if self.busy.swap(true, Ordering::SeqCst) || !Self::available_locked(&s) {
                return;
            }
            s.status = "downloading".into();
            s.message.clear();
            s.progress = 0.0;
        }
        std::thread::spawn(move || {
            match self.install() {
                Ok(command) => (self.restart)(command),
                Err(e) => {
                    let mut s = self.state.lock().unwrap();
                    s.status = "error".into();
                    s.message = format!("Update failed: {e}");
                }
            }
            self.busy.store(false, Ordering::SeqCst);
        });
    }

    fn install(&self) -> Result<Vec<String>, String> {
        let location = installed_location().ok_or("not an installed copy")?;
        let folder = std::env::temp_dir().join(format!("apptime-update-{}", std::process::id()));
        std::fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
        let download = self.download(&folder)?;
        self.state.lock().unwrap().status = "installing".into();
        if cfg!(target_os = "macos") {
            swap_mac(&location, &download)
        } else {
            // /APPTIMELAUNCH: start AppTime again when done (see installer/AppTime.iss).
            Ok(vec![
                download.to_string_lossy().into_owned(),
                "/VERYSILENT".into(),
                "/SUPPRESSMSGBOXES".into(),
                "/NORESTART".into(),
                "/APPTIMELAUNCH".into(),
            ])
        }
    }

    fn download(&self, folder: &Path) -> Result<PathBuf, String> {
        let asset = self.state.lock().unwrap().asset.clone().ok_or("no release file")?;
        let url = asset["browser_download_url"].as_str().ok_or("no download link")?.to_string();
        let size = asset["size"].as_u64().unwrap_or(0);
        let expected = asset["digest"].as_str().unwrap_or("").to_lowercase();
        let target = folder.join(ASSET_NAME);
        let res = Self::agent()
            .get(&url)
            .set("User-Agent", &format!("AppTime/{APP_VERSION}"))
            .timeout(Duration::from_secs(600))
            .call()
            .map_err(|e| e.to_string())?;
        let mut reader = res.into_reader();
        let mut out = std::fs::File::create(&target).map_err(|e| e.to_string())?;
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 256 * 1024];
        let mut got: u64 = 0;
        loop {
            let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n]).map_err(|e| e.to_string())?;
            hasher.update(&buf[..n]);
            got += n as u64;
            if size > 0 {
                self.state.lock().unwrap().progress = got as f64 / size as f64;
            }
        }
        drop(out);
        let digest: String = hasher.finalize().iter().map(|b| format!("{b:02x}")).collect();
        let problem = if size > 0 && got != size {
            Some("the download was incomplete")
        } else if expected.starts_with("sha256:") && expected[7..] != digest {
            Some("the download didn't match the published file")
        } else {
            None
        };
        if let Some(problem) = problem {
            std::fs::remove_file(&target).ok();
            return Err(problem.into());
        }
        Ok(target)
    }
}

fn swap_mac(bundle: &Path, download: &Path) -> Result<Vec<String>, String> {
    let unpacked = download.with_file_name("unpacked");
    std::fs::create_dir_all(&unpacked).map_err(|e| e.to_string())?;
    let ok = std::process::Command::new("ditto")
        .args(["-x", "-k"])
        .arg(download)
        .arg(&unpacked)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    let new_app = unpacked.join("AppTime.app");
    if !ok || !new_app.is_dir() {
        return Err("the download didn't contain AppTime.app".into());
    }
    let old = bundle.with_file_name("AppTime.old.app");
    if old.exists() {
        std::fs::remove_dir_all(&old).map_err(|e| e.to_string())?;
    }
    std::fs::rename(bundle, &old).map_err(|e| e.to_string())?;
    let moved = std::fs::rename(&new_app, bundle).or_else(|_| {
        std::process::Command::new("mv").arg(&new_app).arg(bundle).status().map(|_| ())
    });
    if moved.is_err() || !bundle.exists() {
        std::fs::rename(&old, bundle).ok();
        return Err("couldn't replace the app".into());
    }
    std::fs::remove_dir_all(&unpacked).ok();
    std::fs::remove_file(download).ok();
    Ok(vec!["open".into(), "-n".into(), bundle.to_string_lossy().into_owned(), "--args".into(), "--after-update".into()])
}
