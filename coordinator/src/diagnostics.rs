//! Operational diagnostics (PRD-011, backend part) shared by `GET /api/v1/diagnostics` and
//! `bridge-coordinator diagnostics`.
//!
//! Every check is `{value, status: ok|warn|fail, fix}`. All thresholds are in [`THRESHOLDS`] and are
//! listed in `docs/distributed/coordinator.md` (Diagnostics thresholds). Nothing leaves the host: the
//! values are computed from the local database, files and process, and returned to the caller only.
//! An update-availability check is deliberately absent; it would need an outbound request and is
//! opt-in only, not implemented.

use crate::backups::{self, Attempt, AttemptResult};
use crate::config::Config;
use crate::db::Db;
use serde_json::{json, Map, Value};
use std::collections::VecDeque;
use std::path::Path;
use std::sync::Mutex;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Status {
    Ok,
    Warn,
    Fail,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Ok => "ok",
            Status::Warn => "warn",
            Status::Fail => "fail",
        }
    }
}

/// The single table of thresholds. A value at or beyond `*_warn` is a warning, at or beyond `*_fail` a failure
/// (for the "below" entries, at or below).
pub struct Thresholds {
    /// Uptime below this many seconds: the service restarted recently.
    pub uptime_warn_below_secs: u64,
    /// Command queue depth (all workers, commands not finished).
    pub queue_warn: u64,
    pub queue_fail: u64,
    /// Seconds since the last message from a connected worker. Connected workers refresh `last_seen` every
    /// 600 s, so these are multiples of that.
    pub worker_silence_warn_secs: i64,
    pub worker_silence_fail_secs: i64,
    pub db_size_warn_bytes: u64,
    pub db_size_fail_bytes: u64,
    /// Projected growth per day, judged only after an hour of samples.
    pub db_growth_warn_bytes_per_day: u64,
    pub growth_min_window_secs: i64,
    pub disk_free_warn_bytes: u64,
    pub disk_free_fail_bytes: u64,
    /// Backup age as multiples of the configured interval.
    pub backup_age_warn_intervals: u64,
    pub backup_age_fail_intervals: u64,
    /// Backup age limits when automatic backups are off (backups made by cron or by hand).
    pub backup_manual_warn_secs: i64,
    pub backup_manual_fail_secs: i64,
    /// Seconds without any backup before a missing first backup is a warning.
    pub backup_first_grace_secs: u64,
    pub server_cert_warn_days: i64,
    pub server_cert_fail_days: i64,
    pub ca_cert_warn_days: i64,
    pub ca_cert_fail_days: i64,
}

pub const THRESHOLDS: Thresholds = Thresholds {
    uptime_warn_below_secs: 60,
    queue_warn: 50,
    queue_fail: 100,
    worker_silence_warn_secs: 1500,
    worker_silence_fail_secs: 3600,
    db_size_warn_bytes: 1 << 30,
    db_size_fail_bytes: 4 << 30,
    db_growth_warn_bytes_per_day: 128 << 20,
    growth_min_window_secs: 3600,
    disk_free_warn_bytes: 1 << 30,
    disk_free_fail_bytes: 256 << 20,
    backup_age_warn_intervals: 2,
    backup_age_fail_intervals: 7,
    backup_manual_warn_secs: 48 * 3600,
    backup_manual_fail_secs: 7 * 86_400,
    backup_first_grace_secs: 3600,
    server_cert_warn_days: 30,
    server_cert_fail_days: 7,
    ca_cert_warn_days: 90,
    ca_cert_fail_days: 30,
};

fn check(value: Value, status: Status, fix: &str) -> (Value, Status) {
    (
        json!({"value": value, "status": status.as_str(), "fix": if status == Status::Ok { Value::Null } else { json!(fix) }}),
        status,
    )
}

fn grade_high(v: u64, warn: u64, fail: u64) -> Status {
    if v >= fail {
        Status::Fail
    } else if v >= warn {
        Status::Warn
    } else {
        Status::Ok
    }
}

fn grade_low(v: i64, warn: i64, fail: i64) -> Status {
    if v <= fail {
        Status::Fail
    } else if v <= warn {
        Status::Warn
    } else {
        Status::Ok
    }
}

pub struct WorkerInfo {
    pub name: String,
    pub last_seen: Option<i64>,
    pub last_event_at: Option<i64>,
}

pub struct BackupInputs {
    pub interval_secs: Option<u64>,
    pub keep: u32,
    pub dir: String,
    pub count: usize,
    pub newest_at: Option<i64>,
    pub newest_bytes: Option<u64>,
    pub last: Option<Attempt>,
    pub next_due_at: Option<i64>,
}

pub struct Inputs {
    pub now: i64,
    pub uptime_secs: Option<u64>,
    pub versions: Value,
    pub workers: Vec<WorkerInfo>,
    /// `None` when the process is not the serving one (the command line).
    pub connected: Option<Vec<String>>,
    pub queue_depths: Value,
    pub db_bytes: u64,
    pub growth_per_day: Option<f64>,
    pub disk_free: Option<u64>,
    pub backup: BackupInputs,
    pub server_cert_not_after: Option<i64>,
    pub ca_cert_not_after: Option<i64>,
}

const NEEDS_SERVICE: &str = "known only to the running service: read GET /api/v1/diagnostics";

fn mib(b: u64) -> String {
    format!("{:.0} MiB", b as f64 / (1024.0 * 1024.0))
}

/// Build the `checks`, `backup` and overall `status` from collected inputs. Pure: tests call it directly.
pub fn build(i: &Inputs, t: &Thresholds) -> Value {
    let mut checks = Map::new();
    let mut worst = Status::Ok;
    let mut add = |name: &str, c: (Value, Status)| {
        worst = worst.max(c.1);
        checks.insert(name.into(), c.0);
    };

    add("version", check(i.versions.clone(), Status::Ok, ""));
    add(
        "uptime_seconds",
        match i.uptime_secs {
            Some(u) => check(
                json!(u),
                if u < t.uptime_warn_below_secs { Status::Warn } else { Status::Ok },
                "the service restarted a moment ago; if it keeps restarting, read the journal (journalctl -u bridge-coordinator)",
            ),
            None => check(Value::Null, Status::Ok, NEEDS_SERVICE),
        },
    );

    let registered = i.workers.len();
    add(
        "connected_workers",
        match &i.connected {
            Some(c) => {
                let status = if registered > 0 && c.is_empty() {
                    Status::Fail
                } else if c.len() < registered {
                    Status::Warn
                } else {
                    Status::Ok
                };
                check(
                    json!({"connected": c.len(), "registered": registered, "names": c}),
                    status,
                    "a registered worker is not connected: check that `llm-bridge worker run` is running on it and that it reaches this coordinator (llm-bridge worker status)",
                )
            }
            None => check(json!({"registered": registered}), Status::Ok, NEEDS_SERVICE),
        },
    );

    let queue_total: u64 = i
        .queue_depths
        .as_object()
        .map(|o| o.values().filter_map(Value::as_object).flat_map(|s| s.values()).filter_map(Value::as_u64).sum())
        .unwrap_or(0);
    add(
        "command_queue_depth",
        check(
            json!({"total": queue_total, "by_worker": i.queue_depths}),
            grade_high(queue_total, t.queue_warn, t.queue_fail),
            "commands are waiting for a worker: reconnect the worker named in by_worker; commands for a worker that stays away are expired after a day",
        ),
    );

    // Event lag per connected worker: seconds since the coordinator last heard from it. The coordinator does
    // not know a worker's newest event id, so the silence of a connected link is what it can measure.
    let mut lag = Map::new();
    let mut lag_status = Status::Ok;
    let mut lag_worst: Option<String> = None;
    if let Some(connected) = &i.connected {
        for w in i.workers.iter().filter(|w| connected.contains(&w.name)) {
            let silence = w.last_seen.map(|s| (i.now - s).max(0));
            let s = silence.map_or(Status::Ok, |s| {
                if s >= t.worker_silence_fail_secs {
                    Status::Fail
                } else if s >= t.worker_silence_warn_secs {
                    Status::Warn
                } else {
                    Status::Ok
                }
            });
            if s > lag_status {
                lag_status = s;
                lag_worst = Some(w.name.clone());
            }
            lag.insert(
                w.name.clone(),
                json!({"silent_seconds": silence, "last_event_age_seconds": w.last_event_at.map(|e| (i.now - e).max(0))}),
            );
        }
    }
    add(
        "event_lag",
        if i.connected.is_some() {
            check(
                Value::Object(lag),
                lag_status,
                &format!(
                    "worker {} is connected but silent: restart its `llm-bridge worker run`, or check the network path",
                    lag_worst.unwrap_or_default()
                ),
            )
        } else {
            check(Value::Null, Status::Ok, NEEDS_SERVICE)
        },
    );

    add(
        "database_size_bytes",
        check(
            json!(i.db_bytes),
            grade_high(i.db_bytes, t.db_size_warn_bytes, t.db_size_fail_bytes),
            &format!(
                "the database is {}: stop the service and run `bridge-coordinator compact`, and check the retention settings",
                mib(i.db_bytes)
            ),
        ),
    );
    add(
        "database_growth_bytes_per_day",
        match i.growth_per_day {
            Some(g) => check(
                json!(g.round() as i64),
                if g >= t.db_growth_warn_bytes_per_day as f64 {
                    Status::Warn
                } else {
                    Status::Ok
                },
                "the database is growing faster than 128 MiB a day: check which worker floods events (GET /api/v1/workers) and the retention settings",
            ),
            None => check(Value::Null, Status::Ok, "needs the running service and an hour of samples"),
        },
    );
    add(
        "disk_free_bytes",
        match i.disk_free {
            Some(f) => check(
                json!(f),
                if f <= t.disk_free_fail_bytes {
                    Status::Fail
                } else if f <= t.disk_free_warn_bytes {
                    Status::Warn
                } else {
                    Status::Ok
                },
                &format!(
                    "only {} are free on the data directory's file system: free space, or move the data directory; backups are skipped when space is short",
                    mib(f)
                ),
            ),
            None => check(Value::Null, Status::Ok, ""),
        },
    );

    add("last_backup", backup_check(i, t));

    for (name, expiry, warn, fail, fix) in [
        (
            "server_certificate",
            i.server_cert_not_after,
            t.server_cert_warn_days,
            t.server_cert_fail_days,
            "renew it with `bridge-coordinator cert renew` (the service picks up the new file at the next restart); an operator-supplied certificate is renewed by its issuer",
        ),
        (
            "ca_certificate",
            i.ca_cert_not_after,
            t.ca_cert_warn_days,
            t.ca_cert_fail_days,
            "a new CA needs `bridge-coordinator cert new-ca`, a new `ca.pem` in every browser and re-enrolment of every worker; plan it before the CA expires",
        ),
    ] {
        add(
            name,
            match expiry {
                Some(e) => {
                    let days = (e - i.now).div_euclid(86_400);
                    check(json!({"not_after": e, "days_left": days}), grade_low(days, warn, fail), fix)
                }
                None => check(Value::Null, Status::Ok, "no certificate file to read"),
            },
        );
    }

    let b = &i.backup;
    json!({
        "status": worst.as_str(),
        "checks": checks,
        "backup": {
            "enabled": b.interval_secs.is_some(),
            "interval_seconds": b.interval_secs,
            "keep": b.keep,
            "dir": b.dir,
            "count": b.count,
            "last_backup_at": b.newest_at,
            "last_backup_age_seconds": b.newest_at.map(|a| (i.now - a).max(0)),
            "last_backup_bytes": b.newest_bytes,
            "last_attempt_at": b.last.as_ref().map(|a| a.at),
            "last_result": b.last.as_ref().map(|a| a.result.as_str()),
            "last_reason": b.last.as_ref().and_then(|a| a.reason.clone()),
            "next_due_at": b.next_due_at,
        },
    })
}

fn backup_check(i: &Inputs, t: &Thresholds) -> (Value, Status) {
    let b = &i.backup;
    let age = b.newest_at.map(|a| (i.now - a).max(0));
    let value = json!({"age_seconds": age, "bytes": b.newest_bytes, "last_result": b.last.as_ref().map(|a| a.result.as_str()), "last_reason": b.last.as_ref().and_then(|a| a.reason.clone())});
    let failed_since = b
        .last
        .as_ref()
        .filter(|a| !matches!(a.result, AttemptResult::Ok | AttemptResult::Cancelled) && b.newest_at.is_none_or(|n| a.at > n));
    if let Some(a) = failed_since {
        let why = a.reason.clone().unwrap_or_default();
        return check(
            value,
            Status::Warn,
            &format!(
                "the last automatic backup was {}: {why}. Free disk space, or raise backup limits with a manual `bridge-coordinator backup --force`",
                a.result.as_str()
            ),
        );
    }
    let (status, fix) = match (b.interval_secs, age) {
        (Some(_), None) => (
            if i.uptime_secs.is_some_and(|u| u < t.backup_first_grace_secs) { Status::Ok } else { Status::Warn },
            "no backup exists yet: the first one is taken a minute after start; if none appears, read the journal for `automatic backup`",
        ),
        (Some(iv), Some(a)) => (
            grade_high(a as u64, iv * t.backup_age_warn_intervals, iv * t.backup_age_fail_intervals),
            "the newest backup is older than the schedule allows: read the journal for `automatic backup`, check free disk space, or take one with `bridge-coordinator backup --output FILE`",
        ),
        (None, None) => (Status::Warn, "automatic backups are off and no backup exists: set backup_interval_hours = 24 in config.toml, or schedule `bridge-coordinator backup`"),
        (None, Some(a)) => (
            if a >= t.backup_manual_fail_secs {
                Status::Fail
            } else if a >= t.backup_manual_warn_secs {
                Status::Warn
            } else {
                Status::Ok
            },
            "automatic backups are off and the newest backup is old: set backup_interval_hours = 24 in config.toml, or run `bridge-coordinator backup`",
        ),
    };
    check(value, status, fix)
}

// ---------------------------------------------------------------- growth samples

#[derive(Default)]
pub struct Monitor {
    samples: Mutex<VecDeque<(i64, u64)>>,
}

impl Monitor {
    pub fn record(&self, now: i64, bytes: u64) {
        let mut s = self.samples.lock().unwrap_or_else(|e| e.into_inner());
        if s.back().is_none_or(|(t, _)| now - t >= 600) {
            s.push_back((now, bytes));
            while s.len() > 288 {
                s.pop_front();
            }
        }
    }

    /// Projected bytes per day from the oldest sample, after at least `min_window` seconds of samples.
    pub fn growth_per_day(&self, now: i64, bytes: u64, min_window: i64) -> Option<f64> {
        let s = self.samples.lock().unwrap_or_else(|e| e.into_inner());
        let (t, b) = *s.front()?;
        (now - t >= min_window).then(|| (bytes as f64 - b as f64) / (now - t) as f64 * 86_400.0)
    }
}

pub async fn sample_loop(st: crate::http::St) {
    let mut shutdown = st.shutdown.clone();
    let mut tick = tokio::time::interval(std::time::Duration::from_secs(600));
    loop {
        tokio::select! {
            _ = tick.tick() => {
                if let Ok(d) = st.db.diagnostics() {
                    st.monitor.record(crate::now(), d["db_bytes"].as_u64().unwrap_or(0));
                }
            }
            changed = shutdown.changed() => { if changed.is_err() || *shutdown.borrow() { return; } }
        }
    }
}

// ---------------------------------------------------------------- certificates

fn tlv(b: &[u8]) -> Option<(u8, &[u8], &[u8])> {
    let tag = *b.first()?;
    let l = *b.get(1)? as usize;
    let (len, off) = if l < 0x80 {
        (l, 2)
    } else {
        let n = l & 0x7f;
        if n == 0 || n > 4 {
            return None;
        }
        let mut len = 0usize;
        for k in 0..n {
            len = len << 8 | *b.get(2 + k)? as usize;
        }
        (len, 2 + n)
    };
    let end = off.checked_add(len)?;
    Some((tag, b.get(off..end)?, b.get(end..)?))
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn asn1_time(tag: u8, s: &[u8]) -> Option<i64> {
    let text = std::str::from_utf8(s).ok()?.strip_suffix('Z')?;
    let (year, rest) = match tag {
        0x17 if text.len() == 12 => {
            let yy: i64 = text[..2].parse().ok()?;
            (if yy >= 50 { 1900 + yy } else { 2000 + yy }, &text[2..])
        }
        0x18 if text.len() == 14 => (text[..4].parse().ok()?, &text[4..]),
        _ => return None,
    };
    let n = |a: usize| rest.get(a..a + 2)?.parse::<i64>().ok();
    Some(days_from_civil(year, n(0)?, n(2)?) * 86_400 + n(4)? * 3600 + n(6)? * 60 + n(8)?)
}

/// `notAfter` of the first certificate in a PEM file, as epoch seconds.
pub fn cert_not_after(pem: &[u8]) -> Option<i64> {
    use rustls::pki_types::pem::PemObject;
    let der = rustls::pki_types::CertificateDer::pem_slice_iter(pem).next()?.ok()?;
    let (_, cert, _) = tlv(der.as_ref())?;
    let (_, tbs, _) = tlv(cert)?;
    let mut rest = tbs;
    let (tag, _, after) = tlv(rest)?;
    if tag == 0xa0 {
        rest = after; // explicit version
    }
    for _ in 0..3 {
        rest = tlv(rest)?.2; // serial, signature algorithm, issuer
    }
    let (_, validity, _) = tlv(rest)?;
    let (_, _, after_start) = tlv(validity)?;
    let (tag, end, _) = tlv(after_start)?;
    asn1_time(tag, end)
}

fn cert_expiry(path: &Path) -> Option<i64> {
    cert_not_after(&std::fs::read(path).ok()?)
}

// ---------------------------------------------------------------- collection

pub struct Runtime<'a> {
    pub uptime_secs: u64,
    pub connected: Vec<String>,
    pub monitor: &'a Monitor,
}

/// Collect the inputs from the database, files and (when serving) the process, and build the report.
pub fn collect(data_dir: &Path, cfg: &Config, db: &Db, runtime: Option<Runtime<'_>>) -> Result<Value, String> {
    let now = crate::now();
    let d = db.diagnostics().map_err(|e| e.to_string())?;
    let db_bytes = d["db_bytes"].as_u64().unwrap_or(0);
    let workers = db
        .workers_list()
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(|w| WorkerInfo {
            name: w.name,
            last_seen: w.last_seen,
            last_event_at: w.last_event_at,
        })
        .collect();
    let dir = cfg.backup_directory(data_dir);
    let found = backups::list(&dir);
    let interval = cfg.backup_interval();
    let server_cert = cfg
        .tls_cert_file
        .as_deref()
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| data_dir.join("server.pem"));
    let inputs = Inputs {
        now,
        uptime_secs: runtime.as_ref().map(|r| r.uptime_secs),
        versions: json!({"coordinator": env!("CARGO_PKG_VERSION"), "protocol": crate::protocol::PROTOCOL_VERSION, "sqlite": rusqlite::version()}),
        workers,
        connected: runtime.as_ref().map(|r| r.connected.clone()),
        queue_depths: d["command_queue_depths"].clone(),
        db_bytes,
        growth_per_day: runtime
            .as_ref()
            .and_then(|r| r.monitor.growth_per_day(now, db_bytes, THRESHOLDS.growth_min_window_secs)),
        disk_free: crate::ops::free_disk_bytes(data_dir),
        backup: BackupInputs {
            interval_secs: interval,
            keep: cfg.backup_keep,
            dir: dir.display().to_string(),
            count: found.len(),
            newest_at: found.last().map(|f| f.modified),
            newest_bytes: found.last().map(|f| f.bytes),
            last: backups::read_status(&dir),
            next_due_at: interval.map(|iv| backups::due_at(&dir, iv, now)),
        },
        server_cert_not_after: cert_expiry(&server_cert),
        ca_cert_not_after: cert_expiry(&data_dir.join("ca.pem")),
    };
    Ok(build(&inputs, &THRESHOLDS))
}

/// Plain-text rendering for the command line.
pub fn render_text(report: &Value) -> String {
    let mut out = format!("overall: {}\n", report["status"].as_str().unwrap_or("?"));
    if let Some(checks) = report["checks"].as_object() {
        for (name, c) in checks {
            let value = c["value"].to_string();
            let value = if value.len() > 100 { format!("{}...", &value[..100]) } else { value };
            out.push_str(&format!("{:<32} {:<5} {}\n", name, c["status"].as_str().unwrap_or("?"), value));
            if let Some(fix) = c["fix"].as_str() {
                out.push_str(&format!("{:<32}       fix: {fix}\n", ""));
            }
        }
    }
    let b = &report["backup"];
    out.push_str(&format!(
        "backups: {} in {} (keep {}); last {} ({} s old); last attempt {}; next due {}\n",
        b["count"],
        b["dir"].as_str().unwrap_or("?"),
        b["keep"],
        b["last_backup_at"],
        b["last_backup_age_seconds"],
        b["last_result"],
        b["next_due_at"]
    ));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Inputs {
        Inputs {
            now: 10_000_000,
            uptime_secs: Some(100_000),
            versions: json!({}),
            workers: vec![],
            connected: Some(vec![]),
            queue_depths: json!({}),
            db_bytes: 1000,
            growth_per_day: None,
            disk_free: Some(50 << 30),
            backup: BackupInputs {
                interval_secs: Some(86_400),
                keep: 7,
                dir: "/b".into(),
                count: 1,
                newest_at: Some(10_000_000 - 3600),
                newest_bytes: Some(10),
                last: None,
                next_due_at: None,
            },
            server_cert_not_after: Some(10_000_000 + 400 * 86_400),
            ca_cert_not_after: Some(10_000_000 + 1000 * 86_400),
        }
    }

    fn status(r: &Value, name: &str) -> String {
        r["checks"][name]["status"].as_str().unwrap().to_string()
    }

    #[test]
    fn healthy_inputs_are_ok_with_no_fix_text() {
        let r = build(&base(), &THRESHOLDS);
        assert_eq!(r["status"], "ok");
        assert_eq!(r["checks"]["last_backup"]["fix"], Value::Null);
    }

    #[test]
    fn backup_age_warns_after_two_intervals_and_fails_after_seven() {
        let mut i = base();
        i.backup.newest_at = Some(i.now - 2 * 86_400 - 1);
        assert_eq!(status(&build(&i, &THRESHOLDS), "last_backup"), "warn");
        i.backup.newest_at = Some(i.now - 7 * 86_400);
        assert_eq!(status(&build(&i, &THRESHOLDS), "last_backup"), "fail");
        i.backup.newest_at = Some(i.now - 86_400);
        assert_eq!(status(&build(&i, &THRESHOLDS), "last_backup"), "ok");
    }

    #[test]
    fn certificate_expiry_thresholds() {
        let mut i = base();
        i.server_cert_not_after = Some(i.now + 29 * 86_400);
        let r = build(&i, &THRESHOLDS);
        assert_eq!(status(&r, "server_certificate"), "warn");
        assert!(r["checks"]["server_certificate"]["fix"].as_str().unwrap().contains("cert renew"));
        assert_eq!(r["status"], "warn");
        i.server_cert_not_after = Some(i.now + 6 * 86_400);
        assert_eq!(status(&build(&i, &THRESHOLDS), "server_certificate"), "fail");
        i.server_cert_not_after = Some(i.now - 1);
        assert_eq!(status(&build(&i, &THRESHOLDS), "server_certificate"), "fail");
    }

    #[test]
    fn queue_workers_and_size_thresholds() {
        let mut i = base();
        i.workers = vec![WorkerInfo {
            name: "w".into(),
            last_seen: Some(i.now - 1600),
            last_event_at: None,
        }];
        assert_eq!(status(&build(&i, &THRESHOLDS), "connected_workers"), "fail");
        i.connected = Some(vec!["w".into()]);
        let r = build(&i, &THRESHOLDS);
        assert_eq!(status(&r, "connected_workers"), "ok");
        assert_eq!(status(&r, "event_lag"), "warn");
        i.queue_depths = json!({"w": {"sent": 60}});
        assert_eq!(status(&build(&i, &THRESHOLDS), "command_queue_depth"), "warn");
        i.db_bytes = 5 << 30;
        assert_eq!(status(&build(&i, &THRESHOLDS), "database_size_bytes"), "fail");
    }

    #[test]
    fn disabled_backups_and_failed_attempts() {
        let mut i = base();
        i.backup.interval_secs = None;
        assert_eq!(status(&build(&i, &THRESHOLDS), "last_backup"), "ok");
        i.backup.newest_at = None;
        assert_eq!(status(&build(&i, &THRESHOLDS), "last_backup"), "warn");
        let mut i = base();
        i.backup.last = Some(Attempt {
            at: i.now,
            result: AttemptResult::Skipped,
            reason: Some("not enough free space".into()),
            file: None,
            bytes: None,
        });
        let r = build(&i, &THRESHOLDS);
        assert_eq!(status(&r, "last_backup"), "warn");
        assert!(r["checks"]["last_backup"]["fix"].as_str().unwrap().contains("not enough free space"));
    }

    #[test]
    fn growth_needs_an_hour_of_samples() {
        let m = Monitor::default();
        m.record(0, 1000);
        assert_eq!(m.growth_per_day(600, 2000, 3600), None);
        let g = m.growth_per_day(3600, 1000 + 1_000_000, 3600).unwrap();
        assert!((g - 24_000_000.0).abs() < 1.0);
    }

    #[test]
    fn reads_not_after_from_a_generated_certificate() {
        let dir = std::env::temp_dir().join(format!("bridge-diag-cert-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        crate::init::init(
            &dir,
            &crate::certs::Sans {
                hostnames: vec![],
                ips: vec![],
            },
        )
        .unwrap();
        let na = cert_expiry(&dir.join("server.pem")).unwrap();
        let days = (na - crate::now()) / 86_400;
        assert!(days > 300 && days < 3000, "{days}");
        assert!(cert_expiry(&dir.join("ca.pem")).unwrap() > na - 86_400 * 3000);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
