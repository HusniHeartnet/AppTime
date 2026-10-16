//! Community sharing: anonymous daily app totals, sent once a day to the
//! AppTime server, only if the person agreed. Same behaviour as AppTime 1.x.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::Local;
use rusqlite::Connection;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{db, stats, APP_VERSION};

/// The community server. AppTime trusts only this server's own certificate.
pub const COMMUNITY_URL: &str = "https://139.190.100.30:8443";
const SERVER_CERT_PEM: &[u8] = include_bytes!("../../server_cert.pem");
pub const PRIVACY_URL: &str = "https://github.com/HusniHeartnet/AppTime/blob/main/PRIVACY.md";
/// Optional: where results are posted, e.g. a Discord invite.
pub const COMMUNITY_LINK: &str = "";
pub const UPLOAD_CHECK: Duration = Duration::from_secs(15 * 60);

#[derive(Serialize, Deserialize, Clone, Default)]
#[serde(default)]
pub struct Share {
    pub decided: bool,                 // has the person answered the consent question?
    pub enabled: bool,
    pub since: Option<String>,         // first day that may be shared (the day they agreed)
    pub install_id: Option<String>,
    pub token: Option<String>,
    pub hidden: Vec<String>,           // apps never shared
    pub last_uploaded: Option<String>, // last day sent successfully
    pub last_status: Option<String>,
}

static SHARE_LOCK: Mutex<()> = Mutex::new(());

pub fn get_share(conn: &Connection) -> Share {
    db::get_setting(conn, "share").unwrap_or_default()
}

fn set_share(conn: &Connection, share: &Share) {
    db::set_setting(conn, "share", share).ok();
}

fn now_label(fmt: &str) -> String {
    Local::now().format(fmt).to_string()
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    getrandom::getrandom(&mut buf).expect("random bytes");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// The day records that would be sent, oldest first (at most 60).
pub fn share_days(conn: &Connection, share: &Share, include_today: bool) -> Vec<Value> {
    let Some(since) = &share.since else { return Vec::new() };
    let Ok(since_date) = chrono::NaiveDate::parse_from_str(since, "%Y-%m-%d") else { return Vec::new() };
    let today = stats::today().format("%Y-%m-%d").to_string();
    let day_before_since = (since_date - chrono::Duration::days(1)).format("%Y-%m-%d").to_string();
    let after = std::cmp::max(share.last_uploaded.clone().unwrap_or_default(), day_before_since);
    let op = if include_today { "<=" } else { "<" };
    let hidden: HashSet<&str> = share.hidden.iter().map(|s| s.as_str()).collect();

    let days: Vec<String> = conn
        .prepare(&format!("SELECT day FROM screen WHERE day > ?1 AND day {op} ?2 GROUP BY day ORDER BY day LIMIT 60"))
        .and_then(|mut s| s.query_map(rusqlite::params![after, today], |r| r.get(0))?.collect())
        .unwrap_or_default();
    days.into_iter()
        .map(|day| {
            let screen: f64 = conn
                .query_row("SELECT COALESCE(SUM(seconds), 0) FROM screen WHERE day = ?1", [&day], |r| r.get(0))
                .unwrap_or(0.0);
            let apps: Vec<Value> = conn
                .prepare(
                    "SELECT u.exe, a.name, SUM(u.seconds) FROM usage u LEFT JOIN apps a ON a.exe = u.exe
                     WHERE u.day = ?1 GROUP BY u.exe ORDER BY 3 DESC",
                )
                .and_then(|mut s| {
                    s.query_map([&day], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, f64>(2)?)))?
                        .collect::<Result<Vec<_>, _>>()
                })
                .unwrap_or_default()
                .into_iter()
                .filter(|(e, _, secs)| !hidden.contains(e.as_str()) && *secs >= 1.0)
                .map(|(e, n, secs)| json!({"exe": e, "name": n.unwrap_or_else(|| e.clone()), "seconds": secs as i64}))
                .collect();
            json!({"day": day, "screen": screen as i64, "apps": apps})
        })
        .collect()
}

// ------------------------------------------------------------------ talking to the server

/// Accepts exactly the AppTime server's certificate and nothing else (still
/// checking the connection's signatures), so data can't be sent anywhere else.
#[derive(Debug)]
struct PinnedServer {
    cert: CertificateDer<'static>,
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for PinnedServer {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        if end_entity.as_ref() == self.cert.as_ref() {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(rustls::Error::General("not the AppTime server".into()))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

fn server_agent() -> Option<ureq::Agent> {
    let cert = rustls_pemfile::certs(&mut &SERVER_CERT_PEM[..]).next()?.ok()?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .ok()?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(PinnedServer { cert, provider }))
        .with_no_client_auth();
    Some(ureq::AgentBuilder::new().tls_config(Arc::new(config)).timeout(Duration::from_secs(20)).build())
}

pub fn sharing_available() -> bool {
    !COMMUNITY_URL.is_empty() && !SERVER_CERT_PEM.is_empty()
}

fn post_json(path: &str, payload: &Value) -> Result<Value, String> {
    let agent = server_agent().ok_or("no server certificate")?;
    let res = agent
        .post(&format!("{COMMUNITY_URL}{path}"))
        .set("User-Agent", &format!("AppTime/{APP_VERSION}"))
        .send_json(payload.clone())
        .map_err(|e| e.to_string())?;
    res.into_json::<Value>().map_err(|e| e.to_string())
}

/// Send finished days that haven't been shared yet. Safe to call any time.
pub fn upload_pending() {
    if !sharing_available() {
        return;
    }
    let _guard = SHARE_LOCK.lock().unwrap();
    let Ok(conn) = db::connect() else { return };
    let mut share = get_share(&conn);
    if !share.enabled {
        return;
    }
    let days = share_days(&conn, &share, false);
    if days.is_empty() {
        return;
    }
    let payload = json!({
        "install_id": share.install_id, "token": share.token, "app_version": APP_VERSION, "days": days,
    });
    match post_json("/api/upload", &payload) {
        Ok(_) => {
            share.last_uploaded = days.last().and_then(|d| d["day"].as_str()).map(String::from);
            share.last_status = Some(format!("Sent {} day(s) on {}", days.len(), now_label("%d %b %Y %H:%M")));
        }
        Err(_) => share.last_status = Some(format!("Couldn't reach the server ({}), will retry", now_label("%d %b %H:%M"))),
    }
    set_share(&conn, &share);
}

pub fn uploader(stopped: &std::sync::atomic::AtomicBool) {
    std::thread::sleep(Duration::from_secs(60)); // let startup settle
    while !stopped.load(std::sync::atomic::Ordering::Relaxed) {
        upload_pending();
        std::thread::sleep(UPLOAD_CHECK);
    }
}

// ------------------------------------------------------------------ dashboard

pub fn share_state(conn: &Connection) -> Value {
    let share = get_share(conn);
    let mut preview = if share.enabled { share_days(conn, &share, true) } else { Vec::new() };
    if preview.is_empty() {
        // Nothing waiting: show what today would look like.
        let sample = Share { since: Some(stats::today().format("%Y-%m-%d").to_string()), last_uploaded: None, ..share.clone() };
        let mut today = share_days(conn, &sample, true);
        preview = today.split_off(today.len().saturating_sub(1));
    }
    let apps: Vec<Value> = conn
        .prepare("SELECT u.exe, a.name FROM usage u LEFT JOIN apps a ON a.exe = u.exe GROUP BY u.exe ORDER BY SUM(u.seconds) DESC")
        .and_then(|mut s| {
            s.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)))?.collect::<Result<Vec<_>, _>>()
        })
        .unwrap_or_default()
        .into_iter()
        .map(|(e, n)| json!({"exe": e, "name": n.unwrap_or_else(|| e.clone())}))
        .collect();
    json!({
        "available": sharing_available(),
        "decided": share.decided,
        "enabled": share.enabled,
        "has_id": share.install_id.is_some(),
        "since": share.since,
        "hidden": share.hidden,
        "last_uploaded": share.last_uploaded,
        "last_status": share.last_status,
        "community_url": if COMMUNITY_LINK.is_empty() { Value::Null } else { json!(COMMUNITY_LINK) },
        "privacy_url": PRIVACY_URL,
        "preview": preview,
        "today": stats::today().format("%Y-%m-%d").to_string(),
        "apps": apps,
    })
}

pub fn update_share(conn: &Connection, body: &Value) {
    let _guard = SHARE_LOCK.lock().unwrap();
    let mut share = get_share(conn);
    if let Some(enabled) = body.get("enabled").and_then(|v| v.as_bool()) {
        share.decided = true;
        if enabled && !share.enabled {
            share.since = Some(stats::today().format("%Y-%m-%d").to_string());
            share.last_uploaded = None;
            if share.install_id.is_none() {
                share.install_id = Some(random_hex(16));
                share.token = Some(random_hex(32));
            }
        }
        share.enabled = enabled;
    }
    if let Some(hidden) = body.get("hidden").and_then(|v| v.as_array()) {
        let mut set: Vec<String> = hidden.iter().filter_map(|v| v.as_str()).map(|s| s.to_lowercase()).collect();
        set.sort();
        set.dedup();
        share.hidden = set;
    }
    set_share(conn, &share);
}

/// Ask the server to delete everything sent from this install, then start over with a new ID.
pub fn delete_shared(conn: &Connection) -> Result<(), String> {
    let _guard = SHARE_LOCK.lock().unwrap();
    let mut share = get_share(conn);
    if share.install_id.is_some() {
        post_json("/api/delete", &json!({"install_id": share.install_id, "token": share.token}))?;
    }
    share = Share {
        decided: true,
        hidden: share.hidden,
        last_status: Some(format!("Shared data deleted on {}", now_label("%d %b %Y %H:%M"))),
        ..Share::default()
    };
    set_share(conn, &share);
    Ok(())
}
