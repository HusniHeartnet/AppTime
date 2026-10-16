//! AppTime: screen time for your Windows PC or Mac.
//!
//! Records how long each app is on screen while you're at the computer, and
//! shows it in a dashboard (day / week / month / year / lifetime).
//!
//!   AppTime                  start (opens the dashboard)
//!   AppTime --background     start quietly in the tray (used at sign-in)
//!   AppTime --selftest       check tracking works, print what it sees, exit
//!   AppTime --no-tray        run without a tray icon (for testing)

#![cfg_attr(windows, windows_subsystem = "windows")]

mod backup;
mod db;
mod geometry;
mod platform;
mod server;
mod share;
mod stats;
mod tracker;
mod tray;
mod updater;
mod window;

use std::sync::atomic::Ordering;
use std::sync::OnceLock;
use std::time::Duration;

use tracker::{SelfUsage, Tracker};
use updater::Updater;
use window::DashboardWindow;

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

pub struct App {
    pub tracker: Tracker,
    pub usage: SelfUsage,
    pub updater: Updater,
    pub window: DashboardWindow,
}

static APP: OnceLock<App> = OnceLock::new();

pub fn app() -> &'static App {
    APP.get().expect("AppTime not started")
}

pub fn port() -> u16 {
    std::env::var("APPTIME_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(47820)
}

pub fn url() -> String {
    format!("http://127.0.0.1:{}/", port())
}

/// The command that starts this AppTime again, with extra arguments.
pub fn own_command(args: &[&str]) -> Vec<String> {
    let exe = std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| "AppTime".into());
    std::iter::once(exe).chain(args.iter().map(|a| a.to_string())).collect()
}

/// The person switched starting at sign-in on or off.
pub fn set_startup(enabled: bool) {
    platform::set_startup(enabled, &own_command(&["--background"])).ok();
    db::save_setting("startup", &enabled);
}

/// Installed app: start automatically at sign-in unless the person switched
/// that off. Runs at every start, so the entry is put back if it went missing
/// and follows the app if its file was moved.
fn ensure_startup() {
    if updater::installed_location().is_none() {
        return; // development build: leave it alone
    }
    let wanted = db::setting::<bool>("startup").unwrap_or(true);
    let command = own_command(&["--background"]);
    if wanted {
        platform::set_startup(true, &command).ok();
    } else if platform::startup_enabled() {
        platform::set_startup(false, &command).ok();
    }
}

/// Save the latest tracked time and close the dashboard.
fn save_and_stop() {
    if let Some(app) = APP.get() {
        app.window.close();
        app.tracker.stop();
    }
}

/// End AppTime cleanly (tray Quit, the installer and uninstaller).
pub fn quit_now() -> ! {
    save_and_stop();
    std::process::exit(0)
}

/// Start the freshly installed version (or its installer) and end this one.
fn restart_into(command: Vec<String>) {
    save_and_stop();
    std::process::Command::new(&command[0]).args(&command[1..]).spawn().ok();
    std::process::exit(0);
}

fn save_on_session_end() {
    if let Some(app) = APP.get() {
        app.tracker.flush();
    }
}

fn selftest() {
    platform::attach_console();
    println!("AppTime {APP_VERSION} on {}", std::env::consts::OS);
    println!("idle seconds: {:.1}", platform::idle_seconds());
    let mut apps: Vec<String> = platform::visible_apps()
        .into_iter()
        .map(|(key, path)| format!("{key} ({})", platform::app_name(&key, path.as_deref())))
        .collect();
    apps.sort();
    println!("apps on screen: {apps:?}");
    println!("memory MB: {:.1} | dark mode: {}", platform::memory_mb(), platform::dark_mode());
    println!("starts at login: {} | sharing available: {}", platform::startup_enabled(), share::sharing_available());
    let conn = db::connect().expect("database");
    let days: i64 = conn.query_row("SELECT COUNT(DISTINCT day) FROM screen", [], |r| r.get(0)).unwrap_or(0);
    println!("database: {} ({days} days tracked)", db::db_path().display());
    let stats = stats::stats(&conn, "all", stats::today(), None).expect("stats");
    println!("lifetime screen time: {:.1} h", stats["total"].as_f64().unwrap_or(0.0) / 3600.0);
    let updater = Updater::new(|_| {});
    match updater.check() {
        Ok(()) => {
            let (latest, asset) = updater.latest_known();
            println!("latest release: {} | update file found: {asset}", latest.unwrap_or_default());
        }
        Err(e) => println!("update check failed (network?): {e}"),
    }
    println!("selftest OK");
}

fn main() {
    platform::init_process();
    let args: Vec<String> = std::env::args().collect();
    let has = |flag: &str| args.iter().any(|a| a == flag);

    if has("--selftest") {
        return selftest();
    }
    if has("--window") {
        window::run();
    }

    // Only one AppTime at a time. After an update the previous version may still be closing.
    let after_update = has("--after-update");
    let mut server = None;
    for _ in 0..(if after_update { 60 } else { 1 }) {
        match tiny_http::Server::http(("127.0.0.1", port())) {
            Ok(s) => {
                server = Some(s);
                break;
            }
            Err(_) if after_update => std::thread::sleep(Duration::from_millis(500)),
            Err(_) => {}
        }
    }
    let Some(server) = server else {
        // Already running: ask that copy to show its dashboard.
        if ureq::post(&format!("{}api/open-window", url())).timeout(Duration::from_secs(5)).call().is_err() {
            open::that(url()).ok();
        }
        return;
    };

    APP.set(App {
        tracker: Tracker::new(),
        usage: SelfUsage::new(),
        updater: Updater::new(restart_into),
        window: DashboardWindow::new(),
    })
    .ok();
    let state = app();

    ensure_startup();
    if let Ok(exe) = std::env::current_exe() {
        if updater::installed_location().is_some() {
            platform::record_installed_version(APP_VERSION, &exe);
        }
    }
    platform::watch_session_end(save_on_session_end);

    std::thread::spawn(|| app().tracker.run());
    server::serve(server);
    if share::sharing_available() {
        std::thread::spawn(|| share::uploader(&app().tracker.stopped));
    }
    std::thread::spawn(|| app().updater.run_checks(&app().tracker.stopped));
    std::thread::spawn(|| {
        std::thread::sleep(Duration::from_secs(15));
        updater::remove_old_copy(); // previous version, if we were just updated (macOS)
    });

    // Opened by the person (Start menu, installer, after an update): show the
    // dashboard. Started at sign-in (--background): stay quietly in the tray,
    // unless the sharing question hasn't been answered yet.
    let background = has("--background");
    let undecided = share::sharing_available() && db::connect().map(|c| !share::get_share(&c).decided).unwrap_or(false);
    if !background || undecided {
        state.window.open();
    }

    if has("--no-tray") {
        println!("AppTime {APP_VERSION} tracking. Dashboard: {}", url());
        while !state.tracker.stopped.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_secs(1));
        }
        return;
    }
    tray::run();
}
