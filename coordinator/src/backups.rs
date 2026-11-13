//! Scheduled automatic backups (PRD-013).
//!
//! The serving process takes a backup with [`ops::backup_with`] (`VACUUM INTO` from a read snapshot,
//! paced, budget and free-space checks, `integrity_check`) every `backup_interval_hours` (default 24),
//! keeps the newest `backup_keep` (default 7) in `<data dir>/backups` (mode 0700, files 0600) and
//! records the outcome of every attempt in `backup-status.json` next to them. An attempt that the
//! budget or the free-space check refuses is recorded as `skipped` with the reason; nothing is
//! deleted then. `bridge-coordinator diagnostics` and `GET /api/v1/diagnostics` read the same files.

use crate::http::St;
use crate::ops::{self, BackupOptions};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub const STATUS_FILE: &str = "backup-status.json";
const PREFIX: &str = "coordinator-";
const SUFFIX: &str = ".sqlite3";
/// Delay before the first backup of a process that has none yet.
pub const STARTUP_DELAY_SECS: u64 = 60;
/// Longest wait before retrying an attempt that was skipped or failed.
pub const RETRY_SECS: u64 = 3600;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AttemptResult {
    Ok,
    Skipped,
    Failed,
    Cancelled,
}

impl AttemptResult {
    pub fn as_str(&self) -> &'static str {
        match self {
            AttemptResult::Ok => "ok",
            AttemptResult::Skipped => "skipped",
            AttemptResult::Failed => "failed",
            AttemptResult::Cancelled => "cancelled",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Attempt {
    pub at: i64,
    pub result: AttemptResult,
    pub reason: Option<String>,
    pub file: Option<String>,
    pub bytes: Option<u64>,
}

/// A refusal by the budget or the free-space check is a skip; anything else is a failure.
pub fn classify(error: &str) -> AttemptResult {
    if error == ops::BACKUP_CANCELLED {
        AttemptResult::Cancelled
    } else if error.starts_with("not enough free space") || error.contains("exceeds the budget") {
        AttemptResult::Skipped
    } else {
        AttemptResult::Failed
    }
}

pub struct Found {
    pub path: PathBuf,
    pub modified: i64,
    pub bytes: u64,
}

/// Finished backups in `dir` (`coordinator-*.sqlite3`, not hidden partial files), oldest first.
pub fn list(dir: &Path) -> Vec<Found> {
    let mut v: Vec<Found> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().into_owned();
            if !name.starts_with(PREFIX) || !name.ends_with(SUFFIX) {
                return None;
            }
            let meta = e.metadata().ok().filter(|m| m.is_file())?;
            let modified = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_secs() as i64;
            Some(Found {
                path: e.path(),
                modified,
                bytes: meta.len(),
            })
        })
        .collect();
    v.sort_by(|a, b| (a.modified, &a.path).cmp(&(b.modified, &b.path)));
    v
}

/// Delete all but the newest `keep` backups; returns how many were removed.
pub fn rotate(dir: &Path, keep: usize) -> usize {
    let all = list(dir);
    let excess = all.len().saturating_sub(keep.max(1));
    let mut removed = 0;
    for f in all.into_iter().take(excess) {
        match std::fs::remove_file(&f.path) {
            Ok(()) => removed += 1,
            Err(e) => eprintln!("bridge-coordinator: cannot remove old backup {}: {e}", f.path.display()),
        }
    }
    removed
}

pub fn read_status(dir: &Path) -> Option<Attempt> {
    let v: Value = serde_json::from_slice(&std::fs::read(dir.join(STATUS_FILE)).ok()?).ok()?;
    Some(Attempt {
        at: v["attempted_at"].as_i64()?,
        result: match v["result"].as_str()? {
            "ok" => AttemptResult::Ok,
            "skipped" => AttemptResult::Skipped,
            "cancelled" => AttemptResult::Cancelled,
            _ => AttemptResult::Failed,
        },
        reason: v["reason"].as_str().map(str::to_string),
        file: v["file"].as_str().map(str::to_string),
        bytes: v["bytes"].as_u64(),
    })
}

fn write_status(dir: &Path, a: &Attempt) {
    let body = json!({"attempted_at": a.at, "result": a.result.as_str(), "reason": a.reason, "file": a.file, "bytes": a.bytes});
    if let Err(e) = crate::write_replace(&dir.join(STATUS_FILE), body.to_string().as_bytes(), 0o600) {
        eprintln!("bridge-coordinator: cannot write {}: {e}", dir.join(STATUS_FILE).display());
    }
}

fn ensure_dir(dir: &Path) -> Result<(), String> {
    if !dir.exists() {
        let mut b = std::fs::DirBuilder::new();
        b.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            b.mode(0o700);
        }
        b.create(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    }
    crate::restrict_permissions(dir, 0o700);
    Ok(())
}

/// Remove hidden partial files that a crashed backup left behind (older than an hour).
fn clean_partials(dir: &Path) {
    let limit = std::time::SystemTime::now() - Duration::from_secs(3600);
    for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') && name.contains(".partial-") && e.metadata().and_then(|m| m.modified()).is_ok_and(|t| t < limit) {
            let _ = std::fs::remove_file(e.path());
        }
    }
}

/// Take one backup into `backup_dir`, rotate on success and record the outcome. Blocking.
pub fn attempt(data_dir: &Path, backup_dir: &Path, keep: usize, opts: &BackupOptions) -> Attempt {
    let at = crate::now();
    let outcome = ensure_dir(backup_dir).and_then(|()| {
        clean_partials(backup_dir);
        let mut name = format!("{PREFIX}{}{SUFFIX}", crate::utc_stamp());
        let mut n = 1;
        while backup_dir.join(&name).exists() {
            name = format!("{PREFIX}{}-{n}{SUFFIX}", crate::utc_stamp());
            n += 1;
        }
        ops::backup_with(data_dir, &backup_dir.join(&name), opts).map(|r| (name, r))
    });
    let result = match outcome {
        Ok((name, report)) => {
            let removed = rotate(backup_dir, keep);
            eprintln!(
                "bridge-coordinator: automatic backup {name} written ({:.1} MiB, {removed} old removed)",
                report.output_bytes as f64 / (1024.0 * 1024.0)
            );
            Attempt {
                at,
                result: AttemptResult::Ok,
                reason: None,
                file: Some(name),
                bytes: Some(report.output_bytes),
            }
        }
        Err(e) => {
            let result = classify(&e);
            eprintln!("bridge-coordinator: automatic backup {}: {e}", result.as_str());
            Attempt {
                at,
                result,
                reason: Some(e),
                file: None,
                bytes: None,
            }
        }
    };
    write_status(backup_dir, &result);
    result
}

/// Epoch second at which the next automatic backup is due. `newest` is the modification time of the
/// newest backup, `last` the last attempt as (time, succeeded).
pub fn next_due(newest: Option<i64>, last: Option<(i64, bool)>, interval: u64, now: i64) -> i64 {
    let interval = interval as i64;
    let retry = interval.min(RETRY_SECS as i64);
    let Some(newest) = newest else {
        return match last {
            Some((at, false)) => at + retry,
            _ => now + (STARTUP_DELAY_SECS as i64).min(interval),
        };
    };
    let due = newest + interval;
    match last {
        Some((at, false)) if at > newest => due.max(at + retry),
        _ => due,
    }
}

pub fn due_at(dir: &Path, interval: u64, now: i64) -> i64 {
    let last = read_status(dir).map(|a| (a.at, a.result == AttemptResult::Ok));
    next_due(list(dir).last().map(|f| f.modified), last, interval, now)
}

/// The scheduler task: sleeps until the next backup is due, then backs up on a blocking thread.
pub async fn run(st: St) {
    let Some(interval) = st.cfg.backup_interval() else {
        eprintln!("bridge-coordinator: automatic backups are off (backup_interval_hours = 0)");
        return;
    };
    let dir = st.cfg.backup_directory(&st.data_dir);
    let keep = st.cfg.backup_keep.max(1) as usize;
    let mut shutdown = st.shutdown.clone();
    loop {
        let now = crate::now();
        let wait = Duration::from_secs((due_at(&dir, interval, now) - now).max(0) as u64).max(Duration::from_millis(200));
        tokio::select! {
            _ = tokio::time::sleep(wait) => {}
            changed = shutdown.changed() => { if changed.is_err() || *shutdown.borrow() { return; } }
        }
        if *shutdown.borrow() {
            return;
        }
        let (data_dir, backup_dir) = (st.data_dir.clone(), dir.clone());
        let cancel = Arc::new(AtomicBool::new(false));
        let opts = BackupOptions {
            cancel: Some(cancel.clone()),
            ..Default::default()
        };
        let mut run = tokio::task::spawn_blocking(move || attempt(&data_dir, &backup_dir, keep, &opts));
        // On shutdown the copy is interrupted at its next progress step, so it never delays the stop.
        let finished = tokio::select! {
            r = &mut run => r,
            _ = shutdown.changed() => {
                cancel.store(true, Ordering::Relaxed);
                run.await
            }
        };
        if finished.is_err() {
            eprintln!("bridge-coordinator: automatic backup task panicked");
        }
        if *shutdown.borrow() {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn next_due_rules() {
        let day = 86_400;
        assert_eq!(next_due(None, None, day as u64, 1000), 1000 + 60);
        assert_eq!(next_due(None, None, 10, 1000), 1010);
        assert_eq!(next_due(Some(500), Some((500, true)), day as u64, 1000), 500 + day);
        assert_eq!(next_due(Some(0), Some((100_000, false)), day as u64, 100_010), 100_000 + 3600);
        assert_eq!(next_due(None, Some((50, false)), 5, 52), 55);
    }

    #[test]
    fn classify_cancel_is_its_own_result() {
        assert_eq!(classify(ops::BACKUP_CANCELLED), AttemptResult::Cancelled);
    }

    #[test]
    fn classify_budget_and_space_are_skips() {
        assert_eq!(classify("not enough free space in /x: the backup needs about 1 MiB"), AttemptResult::Skipped);
        assert_eq!(classify("the estimated time 20m00s exceeds the budget 15m00s."), AttemptResult::Skipped);
        assert_eq!(classify("copy failed: disk I/O error"), AttemptResult::Failed);
    }
}
