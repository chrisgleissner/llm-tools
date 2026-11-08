//! DOG-024: backup options, the budget refusal, snapshot age check, compaction, incremental vacuum
//! and the retention of command result bodies. The serving-while-backing-up test is `backup_serving.rs`.
mod common;
use bridge_coordinator::db::{Db, MAX_DONE_COMMANDS, MUTATING_RESULT_KEEP_SECS, READ_RESULT_KEEP_SECS};
use bridge_coordinator::ops::{self, BackupOptions};
use bridge_coordinator::{db_path, init, now};
use common::*;
use rusqlite::{params, Connection};
use serde_json::json;
use std::path::Path;
use std::process::{Command, Output};
use std::time::Duration;

fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_bridge-coordinator")).args(args).output().unwrap()
}

fn text(o: &Output) -> String {
    format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr))
}

/// Add `rows` finished commands with a `result_bytes` result each, directly in SQL.
fn fill_commands(path: &Path, rows: i64, result_bytes: usize, age_secs: i64, mutating: bool) {
    let c = Connection::open(path).unwrap();
    c.pragma_update(None, "synchronous", "OFF").unwrap();
    let body = json!({"outcome": "ok", "result": {"text": "x".repeat(result_bytes)}}).to_string();
    let tx = c.unchecked_transaction().unwrap();
    {
        let mut st = tx
            .prepare(
                "INSERT INTO commands(request_id,worker,operation,mutating,request,state,result,created_at,updated_at) \
                 VALUES(?,?,?,?,?,'done',?,?,?)",
            )
            .unwrap();
        let prefix = format!("{age_secs}-{mutating}-{}", rand_suffix());
        for i in 0..rows {
            let at = now() - age_secs;
            st.execute(params![format!("{prefix}-{i}"), "w1", "mission.status", mutating as i64, "{}", body, at, at])
                .unwrap();
        }
    }
    tx.commit().unwrap();
}

fn rand_suffix() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos() as u64
}

fn database_with_free_pages(dir: &Path) -> Db {
    std::fs::create_dir_all(dir).unwrap();
    let db = Db::open(&db_path(dir)).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    fill_commands(&db_path(dir), 1500, 4000, 100, true);
    db
}

fn pragma(path: &Path, name: &str) -> i64 {
    Connection::open(path).unwrap().query_row(&format!("PRAGMA {name}"), [], |r| r.get(0)).unwrap()
}

#[test]
fn backup_copy_is_compact_verified_and_has_incremental_vacuum() {
    let dir = tmp_dir();
    let db = database_with_free_pages(&dir);
    // Free about 5 MiB: finished commands older than the retention are deleted by maintenance, but with
    // a big batch deleted directly the pages stay on the free list of the live file.
    let src = db_path(&dir);
    Connection::open(&src).unwrap().execute("DELETE FROM commands WHERE rowid % 2 = 0", []).unwrap();
    drop(db);
    assert!(pragma(&src, "freelist_count") > 100);
    let out = dir.join("out.sqlite3");
    let report = ops::backup(&dir, &out).unwrap();
    assert!(report.free_bytes > 0 && report.output_bytes < report.source_bytes, "{report:?}");
    assert_eq!(pragma(&out, "freelist_count"), 0, "free pages are not copied");
    assert_eq!(pragma(&out, "auto_vacuum"), 2, "the copy can give pages back after a restore");
    let mode = Connection::open(&out)
        .unwrap()
        .query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))
        .unwrap();
    assert_eq!(mode, "delete", "the copy is a single self-contained file");
    assert!(std::fs::read_dir(&dir)
        .unwrap()
        .flatten()
        .all(|e| !e.file_name().to_string_lossy().contains("partial")));
    assert_eq!(pragma(&out, "user_version"), bridge_coordinator::db::SCHEMA_VERSION);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn backup_is_refused_when_the_estimate_exceeds_the_budget_unless_forced() {
    let dir = tmp_dir();
    drop(database_with_free_pages(&dir));
    let slow = BackupOptions {
        assume_bytes_per_sec: Some(64 * 1024), // 64 KiB/s: the 6 MiB of data cannot finish in a minute
        budget: Duration::from_secs(60),
        ..Default::default()
    };
    let out = dir.join("refused.sqlite3");
    let err = ops::backup_with(&dir, &out, &slow).unwrap_err();
    assert!(
        err.contains("exceeds the budget") && err.contains("--force") && err.contains("compact"),
        "{err}"
    );
    assert!(!out.exists(), "nothing was written");
    assert!(std::fs::read_dir(&dir)
        .unwrap()
        .flatten()
        .all(|e| !e.file_name().to_string_lossy().starts_with(".refused")));
    let forced = BackupOptions {
        force: true,
        max_bytes_per_sec: 0,
        ..slow
    };
    let report = ops::backup_with(&dir, &out, &forced).unwrap();
    assert!(report.estimate > Duration::from_secs(60));
    assert!(out.exists());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn the_estimate_follows_size_and_rate() {
    let mib = 1024 * 1024;
    let a = ops::estimate_backup(100 * mib, 10 * mib, 0);
    assert_eq!(a, Duration::from_secs(15), "100 MiB at 10 MiB/s, copy plus verification");
    assert_eq!(
        ops::estimate_backup(100 * mib, 50 * mib, 5 * mib),
        Duration::from_secs(30),
        "the rate cap applies"
    );
    assert_eq!(ops::estimate_backup(200 * mib, 10 * mib, 0), a * 2);
}

#[test]
fn the_cli_prints_size_and_progress_and_refuses_over_budget() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(cli(&["init", "--data-dir", d, "--hostname", "a.test"]).status.success());
    assert!(Db::open(&db_path(&dir)).unwrap().worker_add("w1", "hash").unwrap());
    fill_commands(&db_path(&dir), 1500, 4000, 100, true);
    let out = dir.join("b.sqlite3");
    let o = cli(&[
        "backup",
        "--data-dir",
        d,
        "--output",
        out.to_str().unwrap(),
        "--assume-mb-s",
        "1",
        "--budget-minutes",
        "0",
    ]);
    assert!(!o.status.success());
    let t = text(&o);
    assert!(
        t.contains("in use") && t.contains("estimated") && t.contains("budget") && t.contains("exceeds the budget"),
        "{t}"
    );
    assert!(!out.exists());
    let o = cli(&[
        "backup",
        "--data-dir",
        d,
        "--output",
        out.to_str().unwrap(),
        "--assume-mb-s",
        "1",
        "--budget-minutes",
        "0",
        "--force",
    ]);
    let t = text(&o);
    assert!(o.status.success(), "{t}");
    assert!(
        t.contains("nice 19") || t.contains("background") || t.contains("normal priority"),
        "the priority that took effect is reported: {t}"
    );
    assert!(t.contains("integrity_check") && t.contains("MiB"), "{t}");
    assert!(!cli(&["backup", "--data-dir", d, "--output", out.to_str().unwrap(), "--max-mb-s", "x"])
        .status
        .success());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn backup_check_enforces_age_integrity_and_ignores_partial_files() {
    let dir = tmp_dir();
    drop(database_with_free_pages(&dir));
    let backups = dir.join("backups");
    std::fs::create_dir_all(&backups).unwrap();
    let day = Duration::from_secs(86_400);
    let err = ops::check_snapshot(&backups, day, false).err().expect("empty directory");
    assert!(err.contains("no finished backup"), "{err}");
    let good = backups.join("coordinator-1.sqlite3");
    ops::backup(&dir, &good).unwrap();
    std::fs::write(backups.join(".coordinator-2.sqlite3.partial-9"), b"half a file").unwrap();
    let ok = ops::check_snapshot(&backups, day, true).unwrap();
    assert_eq!(ok.path, good);
    assert!(ok.age < Duration::from_secs(60) && ok.schema_version == bridge_coordinator::db::SCHEMA_VERSION);
    // The same file is too old for a one-second limit once it has aged.
    let file = std::fs::File::options().write(true).open(&good).unwrap();
    file.set_modified(std::time::SystemTime::now() - Duration::from_secs(3 * 3600)).unwrap();
    let err = ops::check_snapshot(&backups, Duration::from_secs(3600), false).err().expect("too old");
    assert!(err.contains("is 3h00m") && err.contains("old") && err.contains("backup --output"), "{err}");
    // A corrupt newest file fails the check although it is recent.
    let bad = backups.join("coordinator-3.sqlite3");
    std::fs::write(&bad, b"SQLite format 3\0 this is not a database").unwrap();
    assert!(ops::check_snapshot(&backups, day, false).is_err());
    let o = cli(&["backup-check", "--backup-dir", backups.to_str().unwrap(), "--max-age-hours", "24"]);
    assert!(!o.status.success(), "{}", text(&o));
    std::fs::remove_file(&bad).unwrap();
    let o = cli(&["backup-check", "--backup-dir", backups.to_str().unwrap(), "--max-age-hours", "1"]);
    assert!(!o.status.success(), "{}", text(&o));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn compact_shrinks_a_database_that_cannot_give_pages_back_and_turns_on_incremental_vacuum() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    assert!(cli(&["init", "--data-dir", dir.to_str().unwrap(), "--hostname", "a.test"]).status.success());
    // A database made before this change: no incremental auto-vacuum. Created by a plain connection.
    let live = db_path(&dir);
    let _ = std::fs::remove_file(&live);
    {
        let c = Connection::open(&live).unwrap();
        c.pragma_update(None, "journal_mode", "WAL").unwrap();
    }
    let db = Db::open(&live).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    drop(db);
    assert_eq!(pragma(&live, "auto_vacuum"), 0, "an existing file keeps its mode");
    fill_commands(&live, 3000, 4000, 100, true);
    Connection::open(&live).unwrap().execute("DELETE FROM commands", []).unwrap();
    Connection::open(&live)
        .unwrap()
        .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))
        .unwrap();
    let before = std::fs::metadata(&live).unwrap().len();
    assert!(before > 10 * 1024 * 1024);
    // A running server holds the lock; compact refuses.
    let lock = ops::lock_data_dir(&dir).unwrap();
    let err = ops::compact(&dir, &BackupOptions::default()).unwrap_err();
    assert!(err.contains("stop the coordinator"), "{err}");
    drop(lock);
    let (kept, was, now_bytes) = ops::compact(&dir, &BackupOptions::default()).unwrap();
    assert_eq!(was, before);
    assert!(now_bytes * 10 < before, "{now_bytes} vs {before}");
    assert!(kept.file_name().unwrap().to_string_lossy().contains(".before-compact-") && kept.exists());
    assert_eq!(pragma(&live, "auto_vacuum"), 2);
    let db = Db::open(&live).unwrap();
    assert!(db.worker_get("w1").unwrap().is_some());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_new_database_uses_incremental_vacuum_and_maintenance_returns_free_pages_a_step_at_a_time() {
    let dir = tmp_dir();
    let db = database_with_free_pages(&dir);
    let path = db_path(&dir);
    assert_eq!(pragma(&path, "auto_vacuum"), 2);
    fill_commands(&path, 1500, 4000, 100, true); // 12 MiB in all: more than one 8 MiB step
                                                 // Everything is older than the retention: maintenance deletes the rows, then returns pages.
    Connection::open(&path)
        .unwrap()
        .execute(
            "UPDATE commands SET updated_at = updated_at - ?",
            [bridge_coordinator::db::COMPLETED_COMMAND_KEEP_SECS + 200],
        )
        .unwrap();
    let (_, total_before, incremental) = db.free_space().unwrap();
    assert!(incremental && total_before > 5 * 1024 * 1024);
    let mut previous = total_before;
    let mut steps = 0;
    loop {
        db.maintenance().unwrap();
        let (free, total, _) = db.free_space().unwrap();
        assert!(total <= previous, "the file never grows during maintenance");
        let page = 4096i64;
        assert!(
            previous - total <= (bridge_coordinator::db::INCREMENTAL_VACUUM_PAGES + 16) * page, // plus pointer-map pages
            "one pass returns at most {} pages",
            bridge_coordinator::db::INCREMENTAL_VACUUM_PAGES
        );
        previous = total;
        steps += 1;
        if free == 0 || steps > 20 {
            break;
        }
    }
    assert!(steps >= 2, "the 12 MiB are returned in more than one bounded step");
    assert!(previous < total_before / 4, "{previous} of {total_before} left");
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn result_bodies_are_dropped_on_a_schedule_and_finished_commands_are_capped() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    let path = db_path(&dir);
    fill_commands(&path, 3, 3000, READ_RESULT_KEEP_SECS - 600, false); // fresh read-only: kept whole
    fill_commands(&path, 3, 3000, READ_RESULT_KEEP_SECS + 600, false); // read-only past an hour: body dropped
    fill_commands(&path, 3, 3000, MUTATING_RESULT_KEEP_SECS - 600, true); // mutating inside a day: kept
    fill_commands(&path, 3, 3000, MUTATING_RESULT_KEEP_SECS + 600, true); // mutating past a day: body dropped
    fill_commands(&path, 3, 100, MUTATING_RESULT_KEEP_SECS + 600, false); // short result: kept
    db.maintenance().unwrap();
    let c = Connection::open(&path).unwrap();
    let lens = |mutating: i64, older: bool| -> Vec<i64> {
        let (lo, hi) = if mutating == 1 {
            if older {
                (MUTATING_RESULT_KEEP_SECS, i64::MAX)
            } else {
                (0, MUTATING_RESULT_KEEP_SECS)
            }
        } else if older {
            (READ_RESULT_KEEP_SECS, i64::MAX)
        } else {
            (0, READ_RESULT_KEEP_SECS)
        };
        let mut st = c
            .prepare(
                "SELECT length(result) FROM commands WHERE mutating=?1 AND ?2 <= ?4 - updated_at AND ?4 - updated_at < ?3 AND length(request) = 2 ORDER BY 1",
            )
            .unwrap();
        st.query_map(params![mutating, lo, hi, now()], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    };
    assert!(
        lens(0, false).iter().all(|n| *n > 3000),
        "fresh read-only results stay whole: {:?}",
        lens(0, false)
    );
    assert!(lens(1, false).iter().all(|n| *n > 3000), "mutating results stay a day: {:?}", lens(1, false));
    let dropped = lens(1, true);
    assert!(dropped.iter().all(|n| *n < 100), "old results are reduced to the outcome: {dropped:?}");
    let stub: String = c
        .query_row(
            "SELECT result FROM commands WHERE mutating=1 AND ?1 - updated_at > ?2 LIMIT 1",
            params![now(), MUTATING_RESULT_KEEP_SECS],
            |r| r.get(0),
        )
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&stub).unwrap();
    assert_eq!(v["outcome"], "ok");
    assert_eq!(v["body_dropped"], 1);
    // Running it again changes nothing (stubs are below the size threshold).
    db.maintenance().unwrap();
    let total: i64 = c.query_row("SELECT COUNT(*) FROM commands", [], |r| r.get(0)).unwrap();
    assert_eq!(total, 15, "rows stay until the seven day limit");
    // Row cap: more finished commands than the cap are trimmed oldest first.
    fill_commands(&path, MAX_DONE_COMMANDS + 100, 10, 10, false);
    db.maintenance().unwrap();
    let total: i64 = c.query_row("SELECT COUNT(*) FROM commands WHERE state='done'", [], |r| r.get(0)).unwrap();
    assert_eq!(total, MAX_DONE_COMMANDS);
    let oldest_kept: i64 = c.query_row("SELECT MIN(updated_at) FROM commands", [], |r| r.get(0)).unwrap();
    assert!(now() - oldest_kept <= 15, "the oldest rows went first");
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn init_creates_a_database_with_incremental_vacuum() {
    let dir = tmp_dir();
    init::init(&dir, &sans()).unwrap();
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.free_space().unwrap().2);
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_database_restored_from_a_backup_keeps_incremental_vacuum_and_the_rows() {
    let dir = tmp_dir();
    init::init(&dir, &sans()).unwrap();
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    drop(db);
    fill_commands(&db_path(&dir), 200, 1000, 100, true);
    let out = dir.join("b.sqlite3");
    ops::backup(&dir, &out).unwrap();
    // The live file is replaced by one that predates incremental vacuum.
    let live = db_path(&dir);
    for suffix in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{suffix}", live.display()));
    }
    {
        let c = Connection::open(&live).unwrap();
        c.pragma_update(None, "journal_mode", "WAL").unwrap();
        c.execute_batch("CREATE TABLE t(x);").unwrap();
    }
    assert_eq!(pragma(&live, "auto_vacuum"), 0);
    ops::restore(&dir, &out).unwrap();
    assert_eq!(pragma(&live, "auto_vacuum"), 2, "restoring a backup converts the database");
    let n: i64 = Connection::open(&live)
        .unwrap()
        .query_row("SELECT COUNT(*) FROM commands", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 200);
    let _ = std::fs::remove_dir_all(dir);
}
