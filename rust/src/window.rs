//! The dashboard window. It runs as its own short-lived process (`--window`):
//! closing the window ends that process, so its memory is given back and only
//! the small background tracker keeps running.

use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::{Icon, WindowBuilder};
use wry::WebViewBuilder;

use crate::{backup, own_command, platform, url};

// ------------------------------------------------------------------ in the tracker process

pub struct DashboardWindow {
    child: Mutex<Option<Child>>,
    focus_requested: AtomicBool,
}

impl DashboardWindow {
    pub fn new() -> Self {
        DashboardWindow { child: Mutex::new(None), focus_requested: AtomicBool::new(false) }
    }

    pub fn open(&self) {
        let mut child = self.child.lock().unwrap();
        if let Some(c) = child.as_mut() {
            if matches!(c.try_wait(), Ok(None)) {
                self.focus_requested.store(true, Ordering::Relaxed); // already open: bring it to the front
                return;
            }
        }
        let command = own_command(&["--window"]);
        match std::process::Command::new(&command[0]).args(&command[1..]).spawn() {
            Ok(c) => {
                platform::add_own_pid(c.id());
                *child = Some(c);
            }
            Err(_) => {
                open::that(url()).ok();
            }
        }
    }

    pub fn take_focus_request(&self) -> bool {
        self.focus_requested.swap(false, Ordering::Relaxed)
    }

    pub fn close(&self) {
        if let Some(mut c) = self.child.lock().unwrap().take() {
            c.kill().ok();
        }
    }
}

// ------------------------------------------------------------------ the `--window` process

enum UserEvent {
    Focus,
    Close,
    Ipc(String),
}

/// Lets the dashboard page call `window.pywebview.api.export_data()` /
/// `import_data()` exactly as it did with AppTime 1.x.
const BRIDGE_JS: &str = r#"
(() => {
  const waiting = {};
  let next = 0;
  const call = (action) => new Promise((resolve) => {
    const id = ++next;
    waiting[id] = resolve;
    window.ipc.postMessage(JSON.stringify({ id, action }));
  });
  window.__apptimeReply = (id, result) => { const r = waiting[id]; delete waiting[id]; if (r) r(result); };
  window.pywebview = { api: { export_data: () => call("export"), import_data: () => call("import") } };
  const ready = () => window.dispatchEvent(new Event("pywebviewready"));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
"#;

fn logo_icon() -> Option<Icon> {
    let decoder = png::Decoder::new(&include_bytes!("../../assets/logo.png")[..]);
    let mut reader = decoder.read_info().ok()?;
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).ok()?;
    if info.color_type != png::ColorType::Rgba {
        return None;
    }
    Icon::from_rgba(buf[..info.buffer_size()].to_vec(), info.width, info.height).ok()
}

fn call_tracker(path: &str, payload: Value) -> Value {
    let result = ureq::post(&format!("{}{}", url().trim_end_matches('/'), path))
        .timeout(Duration::from_secs(120))
        .send_json(payload);
    match result {
        Ok(res) => res.into_json().unwrap_or(json!({"error": "Something went wrong."})),
        Err(ureq::Error::Status(_, res)) => res.into_json().unwrap_or(json!({"error": "Something went wrong."})),
        Err(_) => json!({"error": "AppTime isn't running."}),
    }
}

fn tracker_running() -> bool {
    ureq::get(&format!("{}api/window-ping", url())).timeout(Duration::from_secs(3)).call().is_ok()
}

/// Native Save / Open dialogs for backups, asked for by the dashboard page.
fn handle_backup(action: &str) -> Value {
    if action == "export" {
        let name = format!("AppTime backup {}.{}", crate::stats::today().format("%Y-%m-%d"), backup::EXTENSION);
        let Some(mut path) = rfd::FileDialog::new()
            .set_file_name(&name)
            .add_filter("AppTime backup", &[backup::EXTENSION])
            .save_file()
        else {
            return json!({"cancelled": true});
        };
        if path.extension().map(|e| e != backup::EXTENSION).unwrap_or(true) {
            path.set_extension(backup::EXTENSION);
        }
        call_tracker("/api/export", json!({"path": path.to_string_lossy()}))
    } else {
        let Some(path) = rfd::FileDialog::new()
            .add_filter("AppTime backup", &[backup::EXTENSION, "db"])
            .add_filter("All files", &["*"])
            .pick_file()
        else {
            return json!({"cancelled": true});
        };
        call_tracker("/api/import", json!({"path": path.to_string_lossy()}))
    }
}

pub fn run() -> ! {
    platform::prepare_window_process();
    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    #[cfg(target_os = "macos")]
    {
        use tao::platform::macos::{ActivationPolicy, EventLoopExtMacOS};
        // AppTime is a menu bar app; while the dashboard is open, show it in the Dock.
        let mut event_loop = event_loop;
        event_loop.set_activation_policy(ActivationPolicy::Regular);
        return run_loop(event_loop);
    }
    #[cfg(not(target_os = "macos"))]
    run_loop(event_loop)
}

fn run_loop(event_loop: tao::event_loop::EventLoop<UserEvent>) -> ! {
    let window = WindowBuilder::new()
        .with_title("AppTime")
        .with_inner_size(LogicalSize::new(1120.0, 860.0))
        .with_min_inner_size(LogicalSize::new(400.0, 500.0))
        .with_window_icon(logo_icon())
        .build(&event_loop)
        .expect("dashboard window");

    let background = if platform::dark_mode() { (17, 17, 16, 255) } else { (244, 244, 242, 255) };
    let ipc_proxy = event_loop.create_proxy();
    let builder = WebViewBuilder::new()
        .with_url(url())
        .with_initialization_script(BRIDGE_JS)
        .with_background_color(background)
        .with_ipc_handler(move |req: wry::http::Request<String>| {
            ipc_proxy.send_event(UserEvent::Ipc(req.body().clone())).ok();
        })
        .with_new_window_req_handler(|target: String| {
            open::that(target).ok(); // "What's new", privacy policy: open in the normal browser
            false
        });
    #[cfg(windows)]
    let builder = {
        use wry::WebViewBuilderExtWindows;
        builder.with_additional_browser_args(platform::WEBVIEW_ARGS)
    };
    let webview = match builder.build(&window) {
        Ok(w) => w,
        Err(_) => {
            open::that(url()).ok(); // e.g. Windows without the WebView2 runtime
            std::process::exit(0);
        }
    };

    // Lets the tray's "Open dashboard" bring this window forward, and closes the
    // window if AppTime itself was quit.
    let proxy = event_loop.create_proxy();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(1));
        match ureq::get(&format!("{}api/window-ping", url())).timeout(Duration::from_secs(3)).call() {
            Ok(res) => {
                let v: Value = res.into_json().unwrap_or(json!({}));
                if v["focus"].as_bool() == Some(true) {
                    proxy.send_event(UserEvent::Focus).ok();
                }
            }
            Err(_) => {
                if !tracker_running() {
                    proxy.send_event(UserEvent::Close).ok();
                    return;
                }
            }
        }
    });

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            // Window closed: end this process right away so all its memory is freed.
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } | Event::UserEvent(UserEvent::Close) => {
                std::process::exit(0)
            }
            Event::UserEvent(UserEvent::Focus) => {
                window.set_minimized(false);
                window.set_visible(true);
                window.set_always_on_top(true);
                window.set_always_on_top(false);
                window.set_focus();
            }
            Event::UserEvent(UserEvent::Ipc(body)) => {
                let message: Value = serde_json::from_str(&body).unwrap_or(json!({}));
                let id = message["id"].as_i64().unwrap_or(0);
                let result = handle_backup(message["action"].as_str().unwrap_or(""));
                webview.evaluate_script(&format!("window.__apptimeReply({id}, {result})")).ok();
            }
            _ => {}
        }
    })
}
