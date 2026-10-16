//! The local database. Same file and tables as AppTime 1.x, so everyone's
//! history carries over: %LOCALAPPDATA%\AppTime\apptime.db (Windows),
//! ~/Library/Application Support/AppTime/apptime.db (macOS).

use std::path::PathBuf;
use std::time::Duration;

use rusqlite::{params, Connection, OptionalExtension};
use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::platform;

pub fn data_dir() -> PathBuf {
    platform::data_root().join("AppTime")
}

pub fn db_path() -> PathBuf {
    data_dir().join("apptime.db")
}

pub fn connect() -> rusqlite::Result<Connection> {
    std::fs::create_dir_all(data_dir()).ok();
    let conn = Connection::open(db_path())?;
    conn.busy_timeout(Duration::from_secs(10))?;
    create_tables(&conn)?;
    Ok(conn)
}

pub fn create_tables(conn: &Connection) -> rusqlite::Result<()> {
    let had_usage = table_exists(conn, "usage")?;
    let had_screen = table_exists(conn, "screen")?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS usage (
            day TEXT NOT NULL,          -- YYYY-MM-DD, local time
            hour INTEGER NOT NULL,      -- 0-23
            exe TEXT NOT NULL,          -- app key: code.exe (Windows), com.microsoft.vscode (macOS)
            seconds REAL NOT NULL,
            PRIMARY KEY (day, hour, exe)
        );
        -- Real clock time with at least one app on screen. Several apps can be
        -- on screen at once, so app times can add up to more than this.
        CREATE TABLE IF NOT EXISTS screen (
            day TEXT NOT NULL,
            hour INTEGER NOT NULL,
            seconds REAL NOT NULL,
            PRIMARY KEY (day, hour)
        );
        CREATE TABLE IF NOT EXISTS apps (
            exe TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );",
    )?;
    if had_usage && !had_screen {
        // Very old versions counted only the focused app, so app time was screen time.
        conn.execute("INSERT INTO screen SELECT day, hour, SUM(seconds) FROM usage GROUP BY day, hour", [])?;
    }
    Ok(())
}

pub fn table_exists(conn: &Connection, name: &str) -> rusqlite::Result<bool> {
    conn.query_row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1", [name], |_| Ok(()))
        .optional()
        .map(|r| r.is_some())
}

/// Settings are stored as JSON text, exactly like AppTime 1.x.
pub fn get_setting<T: DeserializeOwned>(conn: &Connection, key: &str) -> Option<T> {
    let raw: Option<String> = conn
        .query_row("SELECT value FROM settings WHERE key = ?1", [key], |r| r.get(0))
        .optional()
        .ok()
        .flatten();
    raw.and_then(|text| serde_json::from_str(&text).ok())
}

pub fn set_setting<T: Serialize>(conn: &Connection, key: &str, value: &T) -> rusqlite::Result<()> {
    let text = serde_json::to_string(value).unwrap_or_else(|_| "null".into());
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?1, ?2)", params![key, text])?;
    Ok(())
}

/// Shorthand: read a setting with its own short-lived connection.
pub fn setting<T: DeserializeOwned>(key: &str) -> Option<T> {
    connect().ok().and_then(|c| get_setting(&c, key))
}

pub fn save_setting<T: Serialize>(key: &str, value: &T) {
    if let Ok(c) = connect() {
        set_setting(&c, key, value).ok();
    }
}
