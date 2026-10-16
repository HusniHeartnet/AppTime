//! Export / Import: a backup is a copy of the database (.apptime file).

use rusqlite::{params, Connection, OpenFlags};
use serde_json::{json, Value};

use crate::{db, platform};

pub const EXTENSION: &str = "apptime";

/// Save a copy of all tracked time to `path`.
pub fn export(path: &str) -> Result<Value, String> {
    let src = db::connect().map_err(|e| e.to_string())?;
    if std::path::Path::new(path).exists() {
        std::fs::remove_file(path).map_err(|e| format!("Couldn't replace that file ({e})."))?;
    }
    src.backup(rusqlite::DatabaseName::Main, path, None).map_err(|e| format!("Couldn't save there ({e})."))?;
    let dest = Connection::open(path).map_err(|e| e.to_string())?;
    // The community-sharing ID and its secret stay on this computer only:
    // anyone holding them could delete this person's shared data.
    dest.execute("DELETE FROM settings WHERE key = 'share'", []).ok();
    let days: i64 = dest.query_row("SELECT COUNT(DISTINCT day) FROM screen", [], |r| r.get(0)).unwrap_or(0);
    Ok(json!({"path": path, "days": days}))
}

/// Merge a backup into this computer's data. For each app and hour the larger
/// value is kept, so importing the same backup twice never double-counts.
pub fn import(path: &str) -> Result<Value, String> {
    let not_backup = || "That file isn't an AppTime backup.".to_string();
    let src = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(|_| not_backup())?;
    let has = |t: &str| db::table_exists(&src, t).unwrap_or(false);
    if !(has("usage") && has("apps")) {
        return Err(not_backup());
    }
    let usage: Vec<(String, i64, String, f64)> = src
        .prepare("SELECT day, hour, exe, seconds FROM usage")
        .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect())
        .map_err(|_| not_backup())?;
    let screen_sql = if has("screen") {
        "SELECT day, hour, seconds FROM screen"
    } else {
        "SELECT day, hour, SUM(seconds) FROM usage GROUP BY day, hour" // backups from before screen time was separate
    };
    let screen: Vec<(String, i64, f64)> = src
        .prepare(screen_sql)
        .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect())
        .map_err(|_| not_backup())?;
    let apps: Vec<(String, String, Option<String>)> = src
        .prepare("SELECT exe, name, path FROM apps")
        .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect())
        .map_err(|_| not_backup())?;

    let mut conn = db::connect().map_err(|e| e.to_string())?;
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    for (day, hour, exe, secs) in usage.iter().filter(|u| !platform::is_ignored(&u.2)) {
        tx.execute(
            "INSERT INTO usage (day, hour, exe, seconds) VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT (day, hour, exe) DO UPDATE SET seconds = MAX(seconds, excluded.seconds)",
            params![day, hour, exe, secs],
        )
        .map_err(|e| e.to_string())?;
    }
    for (day, hour, secs) in &screen {
        tx.execute(
            "INSERT INTO screen (day, hour, seconds) VALUES (?1, ?2, ?3)
             ON CONFLICT (day, hour) DO UPDATE SET seconds = MAX(seconds, excluded.seconds)",
            params![day, hour, secs],
        )
        .map_err(|e| e.to_string())?;
    }
    for (exe, name, path) in &apps {
        tx.execute("INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?1, ?2, ?3)", params![exe, name, path])
            .map_err(|e| e.to_string())?;
    }
    tx.commit().map_err(|e| e.to_string())?;
    let days: std::collections::HashSet<&String> = screen.iter().map(|s| &s.0).collect();
    Ok(json!({"days": days.len()}))
}
