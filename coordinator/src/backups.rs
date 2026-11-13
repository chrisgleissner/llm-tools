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
    /// Time in the file name (UTC), never later than now.
    pub modified: i64,
    pub bytes: u64,
}

/// A stamp later than this many seconds after now is treated as a wrong clock or a stray file: the file is
/// not counted, aged or rotated.
const FUTURE_SLACK_SECS: i64 = 86_400;

/// `(epoch, sequence)` of an automatic backup name `coordinator-YYYYMMDDTHHMMSSZ[-N].sqlite3`.
pub fn parse_name(name: &str) -> Option<(i64, u32)> {
    let mid = name.strip_prefix(PREFIX)?.strip_suffix(SUFFIX)?;
    let (stamp, n) = match mid.split_once('-') {
        Some((s, n)) => (s, n.parse::<u32>().ok()?),
        None => (mid, 0),
    };
    let b = stamp.as_bytes();
    let digits = |r: std::ops::Range<usize>| b.get(r).is_some_and(|d| d.iter().all(u8::is_ascii_digit));
    if b.len() != 16 || b[8] != b'T' || b[15] != b'Z' || !digits(0..8) || !digits(9..15) {
        return None;
    }
    let num = |r: std::ops::Range<usize>| stamp[r].parse::<i64>().ok();
    let epoch = crate::diagnostics::days_from_civil(num(0..4)?, num(4..6)?, num(6..8)?) * 86_400 + num(9..11)? * 3600 + num(11..13)? * 60 + num(13..15)?;
    Some((epoch, n))
}

/// Finished automatic backups in `dir`, oldest first by the stamp in the file name (never by modification time).
/// Only regular files (not symbolic links) whose names match the automatic pattern exactly count; manual
/// backups and other files are left alone, and so are names stamped more than a day in the future.
pub fn list(dir: &Path) -> Vec<Found> {
    let now = crate::now();
    let mut v: Vec<((i64, u32), Found)> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().into_owned();
            let key = parse_name(&name)?;
            if key.0 > now + FUTURE_SLACK_SECS {
                return None;
            }
            let meta = std::fs::symlink_metadata(e.path()).ok().filter(|m| m.file_type().is_file())?;
            Some((
                key,
                Found {
                    path: e.path(),
                    modified: key.0.min(now),
                    bytes: meta.len(),
                },
            ))
        })
        .collect();
    v.sort_by_key(|a| a.0);
    v.into_iter().map(|(_, f)| f).collect()
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
        at: v["attempted_at"].as_i64()?.min(crate::now()),
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

/// Create `dir` (mode 0700) when it does not exist. The mode of an existing directory is left alone;
/// diagnostics warn when it is looser than 0700.
fn ensure_dir(dir: &Path) -> Result<(), String> {
    if dir.exists() {
        return Ok(());
    }
    let mut b = std::fs::DirBuilder::new();
    b.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        b.mode(0o700);
    }
    b.create(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    // The mode given to `DirBuilder` is reduced by the umask and applies to parents too: set the last one.
    crate::restrict_permissions(dir, 0o700);
    Ok(())
}

/// True when `dir` exists and group or others can access it (Unix only).
pub fn dir_is_loose(dir: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(dir).is_ok_and(|m| m.is_dir() && m.permissions().mode() & 0o077 != 0)
    }
    #[cfg(not(unix))]
    {
        let _ = dir;
        false
    }
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

/// Epoch second at which the next automatic backup is due. `newest` is the time of the newest backup,
/// `last` the last attempt as (time, succeeded) and `anchor` the process start: with nothing to go by, the
/// first backup is due `STARTUP_DELAY_SECS` (at most one interval) after it, a fixed point that does not move
/// while the scheduler waits.
pub fn next_due(newest: Option<i64>, last: Option<(i64, bool)>, interval: u64, anchor: i64) -> i64 {
    let interval = interval as i64;
    let retry = interval.min(RETRY_SECS as i64);
    let Some(newest) = newest else {
        return match last {
            Some((at, false)) => at + retry,
            _ => anchor + (STARTUP_DELAY_SECS as i64).min(interval),
        };
    };
    let due = newest + interval;
    match last {
        Some((at, false)) if at > newest => due.max(at + retry),
        _ => due,
    }
}

pub fn due_at(dir: &Path, interval: u64, anchor: i64) -> i64 {
    let last = read_status(dir).map(|a| (a.at, a.result == AttemptResult::Ok));
    next_due(list(dir).last().map(|f| f.modified), last, interval, anchor)
}

/// Longest single sleep of the scheduler. A monotonic sleep does not count the time a suspended machine
/// spent asleep, so the wall-clock due time is read again after every chunk.
pub const WAKE_CHUNK_SECS: i64 = 60;

/// How long to sleep now for a backup due at `due`: the wall-clock difference, within 200 ms and one chunk.
pub fn sleep_chunk(due: i64, now: i64) -> Duration {
    Duration::from_secs((due - now).clamp(0, WAKE_CHUNK_SECS) as u64).max(Duration::from_millis(200))
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
    let anchor = crate::now() - st.started.elapsed().as_secs() as i64;
    // Held in memory too, so an attempt whose result cannot be written (read-only directory) is not retried in a loop.
    let mut last_try: Option<i64> = None;
    loop {
        let now = crate::now();
        let mut due = due_at(&dir, interval, anchor);
        if let Some(t) = last_try {
            due = due.max(t + interval.min(RETRY_SECS) as i64);
        }
        if due > now {
            tokio::select! {
                _ = tokio::time::sleep(sleep_chunk(due, now)) => {}
                changed = shutdown.changed() => { if changed.is_err() || *shutdown.borrow() { return; } }
            }
            continue;
        }
        if *shutdown.borrow() {
            return;
        }
        last_try = Some(crate::now());
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
        // The first backup is due at a fixed point after the process start, not "a minute from now".
        assert_eq!(next_due(None, None, day as u64, 1000), next_due(None, None, day as u64, 1000));
        assert_eq!(next_due(Some(500), Some((500, true)), day as u64, 1000), 500 + day);
        assert_eq!(next_due(Some(0), Some((100_000, false)), day as u64, 100_010), 100_000 + 3600);
        assert_eq!(next_due(None, Some((50, false)), 5, 52), 55);
    }

    #[test]
    fn sleeps_are_short_chunks_re_read_against_the_wall_clock() {
        assert_eq!(sleep_chunk(1_000_000, 0), Duration::from_secs(WAKE_CHUNK_SECS as u64));
        assert_eq!(sleep_chunk(30, 0), Duration::from_secs(30));
        assert_eq!(sleep_chunk(0, 100), Duration::from_millis(200));
    }

    #[test]
    fn names_parse_exactly() {
        assert!(parse_name("coordinator-20261113T074639Z.sqlite3").is_some());
        assert_eq!(parse_name("coordinator-20261113T074639Z-3.sqlite3").unwrap().1, 3);
        for bad in [
            "coordinator-manual.sqlite3",
            "coordinator-20261113T074639Z.sqlite3.bak",
            "coordinator-20261113T07463Z.sqlite3",
            "x.sqlite3",
        ] {
            assert!(parse_name(bad).is_none(), "{bad}");
        }
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
