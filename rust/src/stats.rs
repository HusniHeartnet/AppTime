//! Numbers for the dashboard: totals, chart buckets, app list and recaps for a
//! day, week, month, year or lifetime. Same output as AppTime 1.x.

use std::collections::HashMap;

use chrono::{Datelike, Duration, Local, NaiveDate};
use rusqlite::{params_from_iter, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

pub fn today() -> NaiveDate {
    Local::now().date_naive()
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn month_start(d: NaiveDate) -> NaiveDate {
    d.with_day(1).unwrap()
}

fn add_months(d: NaiveDate, n: i32) -> NaiveDate {
    let m = d.month0() as i32 + n;
    NaiveDate::from_ymd_opt(d.year() + m.div_euclid(12), m.rem_euclid(12) as u32 + 1, 1).unwrap()
}

struct Period {
    start: NaiveDate,
    end: NaiveDate,
    label: String,
    prev: Option<NaiveDate>,
    next: Option<NaiveDate>,
}

fn period(range: &str, anchor: NaiveDate, first_day: NaiveDate) -> Period {
    match range {
        "day" => Period {
            start: anchor,
            end: anchor,
            label: anchor.format("%A, %d %B %Y").to_string(),
            prev: Some(anchor - Duration::days(1)),
            next: Some(anchor + Duration::days(1)),
        },
        "week" => {
            let start = anchor - Duration::days(anchor.weekday().num_days_from_monday() as i64);
            let end = start + Duration::days(6);
            Period {
                start,
                end,
                label: format!("{} \u{2013} {}", start.format("%d %b"), end.format("%d %b %Y")),
                prev: Some(start - Duration::days(7)),
                next: Some(start + Duration::days(7)),
            }
        }
        "month" => {
            let start = month_start(anchor);
            Period {
                start,
                end: add_months(start, 1) - Duration::days(1),
                label: start.format("%B %Y").to_string(),
                prev: Some(add_months(start, -1)),
                next: Some(add_months(start, 1)),
            }
        }
        "year" => {
            let y = anchor.year();
            Period {
                start: NaiveDate::from_ymd_opt(y, 1, 1).unwrap(),
                end: NaiveDate::from_ymd_opt(y, 12, 31).unwrap(),
                label: y.to_string(),
                prev: NaiveDate::from_ymd_opt(y - 1, 1, 1),
                next: NaiveDate::from_ymd_opt(y + 1, 1, 1),
            }
        }
        _ => Period { start: first_day, end: today(), label: "Lifetime".into(), prev: None, next: None },
    }
}

/// The chart's x axis: (unit, [(key, label)]).
fn buckets_for(range: &str, start: NaiveDate, end: NaiveDate) -> (&'static str, Vec<(Value, String)>) {
    match range {
        "day" => ("hour", (0..24).map(|h| (json!(h), format!("{h:02}"))).collect()),
        "week" | "month" => {
            let mut out = Vec::new();
            let mut d = start;
            while d <= end {
                let label = if range == "week" {
                    WEEKDAYS[d.weekday().num_days_from_monday() as usize].to_string()
                } else {
                    d.day().to_string()
                };
                out.push((json!(iso(d)), label));
                d += Duration::days(1);
            }
            ("day", out)
        }
        "year" => ("month", (1..=12).map(|m| (json!(format!("{}-{m:02}", start.year())), MONTHS[m - 1].to_string())).collect()),
        _ => {
            // Lifetime: months if the history is short, years otherwise.
            let months = (end.year() - start.year()) * 12 + end.month() as i32 - start.month() as i32 + 1;
            if months <= 36 {
                let mut out = Vec::new();
                let mut d = month_start(start);
                while d <= end {
                    out.push((json!(d.format("%Y-%m").to_string()), format!("{} {}", MONTHS[d.month0() as usize], d.format("%y"))));
                    d = add_months(d, 1);
                }
                ("month", out)
            } else {
                ("year", (start.year()..=end.year()).map(|y| (json!(y.to_string()), y.to_string())).collect())
            }
        }
    }
}

/// Screen time, or one app's time when `exe` is given.
fn total_between(conn: &Connection, start: NaiveDate, end: NaiveDate, exe: Option<&str>) -> f64 {
    let result = match exe {
        Some(exe) => conn.query_row(
            "SELECT COALESCE(SUM(seconds), 0) FROM usage WHERE day BETWEEN ?1 AND ?2 AND exe = ?3",
            rusqlite::params![iso(start), iso(end), exe],
            |r| r.get(0),
        ),
        None => conn.query_row(
            "SELECT COALESCE(SUM(seconds), 0) FROM screen WHERE day BETWEEN ?1 AND ?2",
            rusqlite::params![iso(start), iso(end)],
            |r| r.get(0),
        ),
    };
    result.unwrap_or(0.0)
}

/// Bucket keys come back from SQL as text (days, months, years) or numbers (hours).
fn key_of(value: rusqlite::types::Value) -> Value {
    match value {
        rusqlite::types::Value::Integer(i) => json!(i),
        rusqlite::types::Value::Text(t) => json!(t),
        rusqlite::types::Value::Real(f) => json!(f),
        _ => Value::Null,
    }
}

pub fn stats(conn: &Connection, range: &str, anchor: NaiveDate, exe: Option<&str>) -> rusqlite::Result<Value> {
    let today = today();
    let first: Option<String> = conn.query_row("SELECT MIN(day) FROM screen", [], |r| r.get(0)).optional()?.flatten();
    let first_day = first.and_then(|d| NaiveDate::parse_from_str(&d, "%Y-%m-%d").ok()).unwrap_or(today);
    let p = period(range, anchor, first_day);

    // Bar heights and totals: one app's time when filtered, otherwise screen time.
    let mut filter = "day BETWEEN ? AND ?".to_string();
    let mut args = vec![iso(p.start), iso(p.end)];
    if let Some(exe) = exe {
        filter.push_str(" AND exe = ?");
        args.push(exe.to_string());
    }
    let height_table = if exe.is_some() { "usage" } else { "screen" };

    let (unit, bucket_list) = buckets_for(range, p.start, p.end);
    let expr = match unit {
        "hour" => "hour",
        "day" => "day",
        "month" => "substr(day, 1, 7)",
        _ => "substr(day, 1, 4)",
    };

    let mut heights: HashMap<String, f64> = HashMap::new();
    {
        let mut stmt = conn.prepare(&format!("SELECT {expr}, SUM(seconds) FROM {height_table} WHERE {filter} GROUP BY 1"))?;
        let rows = stmt.query_map(params_from_iter(args.iter()), |r| Ok((key_of(r.get(0)?), r.get::<_, f64>(1)?)))?;
        for (k, v) in rows.flatten() {
            heights.insert(k.to_string(), v);
        }
    }
    let mut per_app: HashMap<String, Map<String, Value>> = HashMap::new();
    {
        let mut stmt = conn.prepare(&format!("SELECT {expr}, exe, SUM(seconds) FROM usage WHERE {filter} GROUP BY 1, 2"))?;
        let rows = stmt.query_map(params_from_iter(args.iter()), |r| {
            Ok((key_of(r.get(0)?), r.get::<_, String>(1)?, r.get::<_, f64>(2)?))
        })?;
        for (k, app, secs) in rows.flatten() {
            per_app.entry(k.to_string()).or_default().insert(app, json!(secs));
        }
    }
    let buckets: Vec<Value> = bucket_list
        .into_iter()
        .map(|(key, label)| {
            let k = key.to_string();
            json!({
                "key": key,
                "label": label,
                "seconds": heights.get(&k).copied().unwrap_or(0.0),
                "apps": per_app.remove(&k).unwrap_or_default(),
            })
        })
        .collect();

    // Each app keeps one color everywhere: the 7 most-used apps of all time get a
    // color slot (in that order), everything else is "Other".
    let mut colors = Map::new();
    {
        let mut stmt = conn.prepare("SELECT exe FROM usage GROUP BY exe ORDER BY SUM(seconds) DESC LIMIT 7")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        for (i, e) in rows.flatten().enumerate() {
            colors.insert(e, json!(i));
        }
    }

    // The app list always covers every app, so you can switch between them.
    let mut apps = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT u.exe, a.name, SUM(u.seconds) FROM usage u LEFT JOIN apps a ON a.exe = u.exe
             WHERE u.day BETWEEN ?1 AND ?2 GROUP BY u.exe ORDER BY 3 DESC",
        )?;
        let rows = stmt.query_map(rusqlite::params![args[0], args[1]], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, f64>(2)?))
        })?;
        for (e, n, s) in rows.flatten() {
            apps.push(json!({"exe": e, "name": n.unwrap_or_else(|| e.clone()), "seconds": s}));
        }
    }

    let screen_total = total_between(conn, p.start, p.end, None);
    let total: f64 = buckets.iter().map(|b| b["seconds"].as_f64().unwrap_or(0.0)).sum();
    let days_tracked: i64 = conn.query_row(
        &format!("SELECT COUNT(DISTINCT day) FROM {height_table} WHERE {filter}"),
        params_from_iter(args.iter()),
        |r| r.get(0),
    )?;

    let mut previous_total = Value::Null;
    let mut has_prev = false;
    if matches!(range, "day" | "week" | "month" | "year") {
        if let Some(prev) = p.prev {
            let pp = period(range, prev, first_day);
            has_prev = pp.end >= first_day;
            if has_prev {
                previous_total = json!(total_between(conn, pp.start, pp.end, exe));
            }
        }
    }

    let busiest = if total > 0.0 {
        buckets
            .iter()
            .fold(None::<&Value>, |best, b| match best {
                Some(x) if x["seconds"].as_f64() >= b["seconds"].as_f64() => Some(x),
                _ => Some(b),
            })
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let app_name = match exe {
        Some(e) => {
            let n: Option<String> = conn.query_row("SELECT name FROM apps WHERE exe = ?1", [e], |r| r.get(0)).optional()?;
            json!(n.unwrap_or_else(|| e.to_string()))
        }
        None => Value::Null,
    };

    Ok(json!({
        "range": range,
        "anchor": iso(anchor),
        "label": p.label,
        "start": iso(p.start),
        "end": iso(p.end),
        "prev": if has_prev { json!(p.prev.map(iso)) } else { Value::Null },
        "next": match p.next { Some(n) if n <= today => json!(iso(n)), _ => Value::Null },
        "today": iso(today),
        "first_day": iso(first_day),
        "unit": unit,
        "total": total,
        "screen_total": screen_total,
        "previous_total": previous_total,
        "days_tracked": days_tracked,
        "busiest": busiest,
        "buckets": buckets,
        "apps": apps,
        "colors": colors,
        "app": exe,
        "app_name": app_name,
    }))
}
