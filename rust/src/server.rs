//! The small local web server the dashboard page talks to (127.0.0.1 only).
//! Same endpoints as AppTime 1.x, so dashboard.html works unchanged.

use std::sync::atomic::Ordering;
use std::sync::Arc;

use serde_json::{json, Value};
use tiny_http::{Header, Request, Response, Server};

use crate::{app, backup, db, platform, share, stats, url};

const DASHBOARD: &str = include_str!("../../dashboard.html");
const LOGO_PNG: &[u8] = include_bytes!("../../assets/logo.png");
const ICON_ICO: &[u8] = include_bytes!("../../assets/AppTime.ico");

pub fn serve(server: Server) {
    let server = Arc::new(server);
    for _ in 0..4 {
        let server = server.clone();
        std::thread::spawn(move || {
            for request in server.incoming_requests() {
                handle(request);
            }
        });
    }
}

fn respond(request: Request, code: u16, body: Vec<u8>, content_type: &str) {
    let response = Response::from_data(body)
        .with_status_code(code)
        .with_header(Header::from_bytes("Content-Type", content_type).unwrap())
        .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
    request.respond(response).ok();
}

fn send(request: Request, code: u16, body: Value) {
    respond(request, code, body.to_string().into_bytes(), "application/json");
}

fn decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
                match hex {
                    Some(b) => {
                        out.push(b);
                        i += 2;
                    }
                    None => out.push(b'%'),
                }
            }
            b => out.push(b),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn query(url: &str) -> std::collections::HashMap<String, String> {
    url.split_once('?')
        .map(|(_, q)| q)
        .unwrap_or("")
        .split('&')
        .filter_map(|pair| pair.split_once('=').map(|(k, v)| (decode(k), decode(v))))
        .collect()
}

fn handle(mut request: Request) {
    let full = request.url().to_string();
    let path = full.split('?').next().unwrap_or("/").to_string();
    let is_post = *request.method() == tiny_http::Method::Post;

    if !is_post {
        return match path.as_str() {
            "/" => respond(request, 200, DASHBOARD.as_bytes().to_vec(), "text/html; charset=utf-8"),
            "/assets/logo.png" => respond(request, 200, LOGO_PNG.to_vec(), "image/png"),
            "/favicon.ico" => respond(request, 200, ICON_ICO.to_vec(), "image/x-icon"),
            "/api/update" => send(request, 200, app().updater.state()),
            // The dashboard window asks whether it should come to the front.
            "/api/window-ping" => send(request, 200, json!({"focus": app().window.take_focus_request()})),
            "/api/stats" => {
                let result = stats_response(&query(&full));
                match result {
                    Ok(data) => send(request, 200, data),
                    Err((code, msg)) => send(request, code, json!({"error": msg})),
                }
            }
            "/api/share" => match db::connect() {
                Ok(conn) => send(request, 200, share::share_state(&conn)),
                Err(e) => send(request, 500, json!({"error": e.to_string()})),
            },
            _ => send(request, 404, json!({"error": "not found"})),
        };
    }

    // Only accept requests from the dashboard itself (or tools without a browser Origin).
    let origin = request.headers().iter().find(|h| h.field.equiv("Origin")).map(|h| h.value.to_string());
    if let Some(origin) = origin {
        if origin != url().trim_end_matches('/') {
            return send(request, 403, json!({"error": "forbidden"}));
        }
    }
    let mut raw = String::new();
    request.as_reader().read_to_string(&mut raw).ok();
    let body: Value = serde_json::from_str(if raw.trim().is_empty() { "{}" } else { &raw }).unwrap_or(json!({}));
    let app = app();

    match path.as_str() {
        "/api/update" => {
            match body["action"].as_str() {
                Some("check") => app.updater.check_quietly(),
                Some("install") => {
                    if app.updater.can_install() {
                        app.updater.start_install();
                    } else {
                        open::that(app.updater.notes_url()).ok(); // e.g. a dev build: download by hand
                        let mut state = app.updater.state();
                        state["manual"] = json!(true);
                        return send(request, 200, state);
                    }
                }
                _ => {}
            }
            send(request, 200, app.updater.state())
        }
        "/api/quit" => {
            std::thread::spawn(|| {
                std::thread::sleep(std::time::Duration::from_millis(300)); // answer first, then quit
                crate::quit_now();
            });
            send(request, 200, json!({"ok": true}))
        }
        "/api/open-window" => {
            app.window.open();
            send(request, 200, json!({"ok": true}))
        }
        "/api/settings" => {
            if let Some(minutes) = body.get("idle_minutes") {
                let minutes = minutes.as_i64().or_else(|| minutes.as_str().and_then(|s| s.parse().ok())).unwrap_or(0);
                if !(1..=60).contains(&minutes) {
                    return send(request, 400, json!({"error": "idle_minutes must be 1-60"}));
                }
                app.tracker.idle_minutes.store(minutes as u32, Ordering::Relaxed);
                db::save_setting("idle_minutes", &minutes);
            }
            if let Some(paused) = body.get("paused").and_then(|v| v.as_bool()) {
                app.tracker.paused.store(paused, Ordering::Relaxed);
            }
            if let Some(startup) = body.get("startup").and_then(|v| v.as_bool()) {
                crate::set_startup(startup);
            }
            if body.get("tray_tip_seen").and_then(|v| v.as_bool()) == Some(true) {
                db::save_setting("tray_tip_seen", &true);
            }
            send(request, 200, json!({"ok": true}))
        }
        "/api/export" | "/api/import" => {
            let file = body["path"].as_str().unwrap_or("").to_string();
            if file.is_empty() {
                return send(request, 400, json!({"error": "No file chosen."}));
            }
            let result = if path == "/api/export" {
                app.tracker.flush();
                backup::export(&file)
            } else {
                backup::import(&file)
            };
            match result {
                Ok(v) => send(request, 200, v),
                Err(e) => send(request, 400, json!({"error": e})),
            }
        }
        "/api/share" if share::sharing_available() => {
            if let Ok(conn) = db::connect() {
                share::update_share(&conn, &body);
            }
            if body.get("enabled").and_then(|v| v.as_bool()) == Some(true) {
                std::thread::spawn(share::upload_pending);
            }
            send(request, 200, json!({"ok": true}))
        }
        "/api/share/delete" if share::sharing_available() => {
            let result = db::connect().map_err(|e| e.to_string()).and_then(|conn| share::delete_shared(&conn));
            match result {
                Ok(()) => send(request, 200, json!({"ok": true})),
                Err(_) => send(request, 502, json!({"error": "Couldn't reach the server. Nothing was changed, try again later."})),
            }
        }
        _ => send(request, 404, json!({"error": "not found"})),
    }
}

fn stats_response(q: &std::collections::HashMap<String, String>) -> Result<Value, (u16, String)> {
    let app = app();
    app.tracker.flush();
    let range = q.get("range").map(String::as_str).unwrap_or("day");
    if !["day", "week", "month", "year", "all"].contains(&range) {
        return Err((400, "bad range".into()));
    }
    let anchor = match q.get("date") {
        Some(d) => chrono::NaiveDate::parse_from_str(d, "%Y-%m-%d").map_err(|_| (400, "bad date".to_string()))?,
        None => stats::today(),
    };
    let exe = q.get("app").map(String::as_str).filter(|s| !s.is_empty());
    let conn = db::connect().map_err(|e| (500, e.to_string()))?;
    let mut data = stats::stats(&conn, range, anchor, exe).map_err(|e| (500, e.to_string()))?;
    data["paused"] = json!(app.tracker.paused.load(Ordering::Relaxed));
    data["idle_minutes"] = json!(app.tracker.idle_minutes.load(Ordering::Relaxed));
    data["usage"] = app.usage.sample();
    data["startup"] = json!(platform::startup_enabled());
    data["startup_label"] = json!(platform::STARTUP_LABEL);
    data["tray_tip"] = json!(!db::get_setting::<bool>(&conn, "tray_tip_seen").unwrap_or(false));
    data["platform"] = json!(if cfg!(target_os = "macos") { "darwin" } else { "win32" });
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_query_values() {
        assert_eq!(decode("some+app.app"), "some app.app");
        assert_eq!(decode("a%20b%2Fc"), "a b/c");
        assert_eq!(decode("100%"), "100%");
    }
}
