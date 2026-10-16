//! The background tracker: every second, add the time to each app on screen
//! (and to screen time), and save to the database every 30 seconds.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use chrono::{Local, Timelike};
use rusqlite::params;

use crate::{db, platform};

const POLL: Duration = Duration::from_secs(1);
const FLUSH_EVERY: Duration = Duration::from_secs(30);
/// Longer gaps between polls mean sleep or hibernate: not counted.
const MAX_GAP_SECONDS: f64 = 5.0;
pub const DEFAULT_IDLE_MINUTES: u32 = 5;

/// (day, hour, app key or None for screen time) -> seconds not yet saved.
type Pending = HashMap<(String, u32, Option<String>), f64>;

pub struct Tracker {
    pending: Mutex<Pending>,
    pub paused: AtomicBool,
    pub idle_minutes: AtomicU32,
    pub stopped: AtomicBool,
}

impl Tracker {
    pub fn new() -> Self {
        let idle = db::setting::<u32>("idle_minutes").unwrap_or(DEFAULT_IDLE_MINUTES);
        // Time recorded for apps that are now ignored (Windows' own tools) is removed.
        if let Ok(conn) = db::connect() {
            for key in platform::IGNORED {
                conn.execute("DELETE FROM usage WHERE exe = ?1", [key]).ok();
            }
        }
        Tracker {
            pending: Mutex::new(HashMap::new()),
            paused: AtomicBool::new(false),
            idle_minutes: AtomicU32::new(idle),
            stopped: AtomicBool::new(false),
        }
    }

    pub fn run(&self) {
        let mut known: HashSet<String> = HashSet::new();
        if let Ok(conn) = db::connect() {
            if let Ok(mut stmt) = conn.prepare("SELECT exe FROM apps") {
                if let Ok(rows) = stmt.query_map([], |r| r.get::<_, String>(0)) {
                    known.extend(rows.flatten());
                }
            }
        }
        let mut last = Instant::now();
        let mut last_flush = last;
        while !self.stopped.load(Ordering::Relaxed) {
            thread::sleep(POLL);
            let now = Instant::now();
            let gap = now.duration_since(last).as_secs_f64();
            last = now;

            let counting = !self.paused.load(Ordering::Relaxed)
                && gap <= MAX_GAP_SECONDS
                && platform::idle_seconds() < self.idle_minutes.load(Ordering::Relaxed) as f64 * 60.0;
            if counting {
                let apps: Vec<(String, Option<String>)> =
                    platform::visible_apps().into_iter().filter(|(key, _)| !platform::is_ignored(key)).collect();
                if !apps.is_empty() {
                    for (key, path) in &apps {
                        if known.insert(key.clone()) {
                            let name = platform::app_name(key, path.as_deref());
                            if let Ok(conn) = db::connect() {
                                conn.execute(
                                    "INSERT OR IGNORE INTO apps (exe, name, path) VALUES (?1, ?2, ?3)",
                                    params![key, name, path],
                                )
                                .ok();
                            }
                        }
                    }
                    let stamp = Local::now();
                    let day = stamp.format("%Y-%m-%d").to_string();
                    let hour = stamp.hour();
                    let mut pending = self.pending.lock().unwrap();
                    for (key, _) in &apps {
                        *pending.entry((day.clone(), hour, Some(key.clone()))).or_insert(0.0) += gap;
                    }
                    *pending.entry((day, hour, None)).or_insert(0.0) += gap;
                }
            }
            if now.duration_since(last_flush) >= FLUSH_EVERY {
                self.flush();
                last_flush = now;
            }
        }
        self.flush();
    }

    /// Save everything not yet written.
    pub fn flush(&self) {
        let items: Pending = std::mem::take(&mut *self.pending.lock().unwrap());
        if items.is_empty() {
            return;
        }
        let Ok(mut conn) = db::connect() else { return };
        let Ok(tx) = conn.transaction() else { return };
        for ((day, hour, key), seconds) in items {
            let result = match key {
                Some(key) => tx.execute(
                    "INSERT INTO usage (day, hour, exe, seconds) VALUES (?1, ?2, ?3, ?4)
                     ON CONFLICT (day, hour, exe) DO UPDATE SET seconds = seconds + excluded.seconds",
                    params![day, hour, key, seconds],
                ),
                None => tx.execute(
                    "INSERT INTO screen (day, hour, seconds) VALUES (?1, ?2, ?3)
                     ON CONFLICT (day, hour) DO UPDATE SET seconds = seconds + excluded.seconds",
                    params![day, hour, seconds],
                ),
            };
            result.ok();
        }
        tx.commit().ok();
    }

    pub fn stop(&self) {
        self.stopped.store(true, Ordering::Relaxed);
        self.flush();
    }
}

/// CPU and memory used by this background process, for the dashboard.
pub struct SelfUsage {
    last: Mutex<(Instant, f64, f64)>, // (when, cpu seconds then, last cpu %)
}

impl SelfUsage {
    pub fn new() -> Self {
        SelfUsage { last: Mutex::new((Instant::now(), platform::cpu_seconds(), 0.0)) }
    }

    pub fn sample(&self) -> serde_json::Value {
        let mut last = self.last.lock().unwrap();
        let now = Instant::now();
        let wall = now.duration_since(last.0).as_secs_f64();
        if wall >= 2.0 {
            let cpu = platform::cpu_seconds();
            let cores = thread::available_parallelism().map(|n| n.get()).unwrap_or(1) as f64;
            last.2 = ((cpu - last.1) / wall / cores * 100.0).max(0.0);
            *last = (now, cpu, last.2);
        }
        serde_json::json!({
            "cpu_percent": (last.2 * 100.0).round() / 100.0,
            "memory_mb": (platform::memory_mb() * 10.0).round() / 10.0,
        })
    }
}
