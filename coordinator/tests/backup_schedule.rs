//! PRD-013 scheduled backups and PRD-011 diagnostics thresholds.
mod common;
use bridge_coordinator::backups;
use bridge_coordinator::ops::BackupOptions;
use common::*;
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::Path;
use std::time::{Duration, SystemTime};

fn set_age(path: &Path, secs: u64) {
    let f = std::fs::OpenOptions::new().write(true).open(path).unwrap();
    f.set_modified(SystemTime::now() - Duration::from_secs(secs)).unwrap();
}

fn names(dir: &Path) -> BTreeSet<String> {
    backups::list(dir)
        .into_iter()
        .map(|f| f.path.file_name().unwrap().to_string_lossy().into_owned())
        .collect()
}

fn fake_backup(dir: &Path, name: &str, age_secs: u64) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(dir.join(name), b"x").unwrap();
    set_age(&dir.join(name), age_secs);
}

#[tokio::test(flavor = "multi_thread")]
async fn schedule_fires_and_rotation_keeps_n() {
    let env = boot_with(|t| format!("backup_interval_seconds = 2\n{}", t.replace("backup_keep = 7", "backup_keep = 2"))).await;
    let dir = env.dir.join("backups");
    let mut seen = BTreeSet::new();
    let deadline = std::time::Instant::now() + Duration::from_secs(40);
    while std::time::Instant::now() < deadline {
        seen.extend(names(&dir));
        if seen.len() >= 3 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(seen.len() >= 3, "the schedule did not produce three backups: {seen:?}");
    // Rotation: never more than `backup_keep` finished backups stay.
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(names(&dir).len() <= 2, "{:?}", names(&dir));
    assert!(names(&dir).len() == 2, "{:?}", names(&dir));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        for f in backups::list(&dir) {
            assert_eq!(std::fs::metadata(&f.path).unwrap().permissions().mode() & 0o777, 0o600);
        }
    }
    let api = env.admin().await;
    let (st, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(st, 200);
    let b = &d["backup"];
    assert_eq!(b["enabled"], true);
    assert_eq!(b["last_result"], "ok");
    assert!(b["count"].as_u64().unwrap() <= 2 && b["last_backup_bytes"].as_u64().unwrap() > 0);
    assert!(b["next_due_at"].as_i64().unwrap() > 0 && b["last_backup_age_seconds"].is_number());
    // The kept files are real, readable backups.
    for f in backups::list(&dir) {
        let c = rusqlite::Connection::open_with_flags(&f.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
        let ok: String = c.query_row("PRAGMA integrity_check", [], |r| r.get(0)).unwrap();
        assert_eq!(ok, "ok");
    }
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn interval_zero_disables_automatic_backups() {
    let env = boot_with(|t| t.replace("backup_interval_hours = 24", "backup_interval_hours = 0")).await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(names(&env.dir.join("backups")).is_empty());
    let api = env.admin().await;
    let (_, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(d["backup"]["enabled"], false);
    assert_eq!(d["backup"]["next_due_at"], Value::Null);
    assert_eq!(d["checks"]["last_backup"]["status"], "warn");
    env.stop().await;
}

#[test]
fn rotation_removes_only_the_oldest_finished_backups() {
    let dir = tmp_dir();
    for (i, age) in [500u64, 400, 300, 200, 100].iter().enumerate() {
        fake_backup(&dir, &format!("coordinator-{i}.sqlite3"), *age);
    }
    // Files that are not finished backups are never touched.
    fake_backup(&dir, ".coordinator-x.partial-1", 900);
    fake_backup(&dir, "notes.txt", 900);
    assert_eq!(backups::rotate(&dir, 3), 2);
    let left = names(&dir);
    assert_eq!(
        left,
        ["coordinator-2.sqlite3", "coordinator-3.sqlite3", "coordinator-4.sqlite3"]
            .map(String::from)
            .into()
    );
    assert!(dir.join("notes.txt").exists() && dir.join(".coordinator-x.partial-1").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn budget_refusal_is_recorded_as_a_skip_and_keeps_old_backups() {
    let env = boot().await;
    let dir = env.dir.join("backups");
    fake_backup(&dir, "coordinator-old.sqlite3", 60);
    let opts = BackupOptions {
        budget: Duration::ZERO,
        assume_bytes_per_sec: Some(1024 * 1024),
        ..Default::default()
    };
    let a = backups::attempt(&env.dir, &dir, 1, &opts);
    assert_eq!(a.result.as_str(), "skipped");
    assert!(a.reason.as_deref().unwrap().contains("exceeds the budget"));
    assert_eq!(names(&dir).len(), 1, "a skipped attempt must not rotate");
    let api = env.admin().await;
    let (_, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(d["backup"]["last_result"], "skipped");
    assert!(d["backup"]["last_reason"].as_str().unwrap().contains("budget"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn diagnostics_carry_the_fields_and_warn_on_backup_age() {
    let env = boot_with(|t| t.replace("backup_interval_hours = 24", "backup_interval_hours = 0")).await;
    let dir = env.dir.join("backups");
    fake_backup(&dir, "coordinator-a.sqlite3", 3600);
    let api = env.admin().await;
    let (st, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(st, 200);
    for name in [
        "uptime_seconds",
        "connected_workers",
        "command_queue_depth",
        "event_lag",
        "database_size_bytes",
        "database_growth_bytes_per_day",
        "disk_free_bytes",
        "last_backup",
        "server_certificate",
        "ca_certificate",
        "version",
    ] {
        let c = &d["checks"][name];
        assert!(c.get("value").is_some() && c["status"].is_string() && c.get("fix").is_some(), "{name}: {c}");
    }
    assert!(d["checks"]["version"]["value"]["coordinator"].is_string());
    assert_eq!(d["checks"]["last_backup"]["status"], "ok");
    assert!(d["checks"]["server_certificate"]["value"]["days_left"].as_i64().unwrap() > 30);
    // A three-day-old backup with automatic backups off is a warning with a fix.
    set_age(&dir.join("coordinator-a.sqlite3"), 3 * 86_400);
    let (_, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(d["checks"]["last_backup"]["status"], "warn");
    assert!(d["checks"]["last_backup"]["fix"].as_str().unwrap().contains("backup"));
    assert_eq!(d["status"], "warn");
    env.stop().await;
}

fn civil_from_days(z: i64) -> (i32, u8, u8) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    ((yoe + era * 400 + i64::from(m <= 2)) as i32, m as u8, d as u8)
}

#[tokio::test(flavor = "multi_thread")]
async fn diagnostics_warn_when_the_ca_certificate_is_close_to_expiry() {
    let env = boot().await;
    let api = env.admin().await;
    let (_, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(d["checks"]["ca_certificate"]["status"], "ok");
    // Replace ca.pem (only read by diagnostics while worker mTLS is off) with one that expires in 60 days.
    let mut params = rcgen::CertificateParams::new(vec!["localhost".to_string()]).unwrap();
    let (y, m, day) = civil_from_days(bridge_coordinator::now() / 86_400 + 60);
    params.not_after = rcgen::date_time_ymd(y, m, day);
    let key = rcgen::KeyPair::generate().unwrap();
    std::fs::write(env.dir.join("ca.pem"), params.self_signed(&key).unwrap().pem()).unwrap();
    let (_, d) = api.get("/api/v1/diagnostics").await;
    let c = &d["checks"]["ca_certificate"];
    assert_eq!(c["status"], "warn", "{c}");
    assert!(c["fix"].as_str().unwrap().contains("new-ca"));
    assert!((58..=60).contains(&c["value"]["days_left"].as_i64().unwrap()));
    env.stop().await;
}

#[test]
fn diagnostics_command_prints_checks_without_a_running_service() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    let bin = env!("CARGO_BIN_EXE_bridge-coordinator");
    let o = std::process::Command::new(bin)
        .args(["diagnostics", "--json", "--data-dir"])
        .arg(&dir)
        .output()
        .unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert!(v["checks"]["database_size_bytes"]["value"].as_u64().unwrap() > 0);
    assert_eq!(v["checks"]["uptime_seconds"]["value"], Value::Null);
    assert_eq!(v["backup"]["enabled"], true);
    let t = std::process::Command::new(bin).args(["diagnostics", "--data-dir"]).arg(&dir).output().unwrap();
    let text = String::from_utf8_lossy(&t.stdout);
    assert!(
        text.contains("overall:") && text.contains("server_certificate") && text.contains("backups:"),
        "{text}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// Add `mib` MiB of rows so that a paced copy takes a while.
fn fill(db: &Path, mib: usize) {
    let c = rusqlite::Connection::open(db).unwrap();
    c.busy_timeout(Duration::from_secs(10)).unwrap();
    c.execute("CREATE TABLE IF NOT EXISTS filler(x BLOB)", []).unwrap();
    let blob = vec![7u8; 1024 * 1024];
    for _ in 0..mib {
        c.execute("INSERT INTO filler VALUES (?1)", [&blob]).unwrap();
    }
}

fn partials(dir: &Path) -> Vec<String> {
    std::fs::read_dir(dir)
        .map(|r| {
            r.flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.contains(".partial-"))
                .collect()
        })
        .unwrap_or_default()
}

#[test]
fn a_cancelled_backup_stops_early_and_leaves_no_file() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    drop(bridge_coordinator::db::Db::open(&bridge_coordinator::db_path(&dir)).unwrap());
    fill(&bridge_coordinator::db_path(&dir), 8);
    // The flag is already set when the copy starts, so the first progress step interrupts it.
    let cancel = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
    let opts = BackupOptions {
        assume_bytes_per_sec: Some(100 * 1024 * 1024),
        force: true,
        cancel: Some(cancel),
        ..Default::default()
    };
    let backup_dir = dir.join("backups");
    let started = std::time::Instant::now();
    let a = backups::attempt(&dir, &backup_dir, 7, &opts);
    assert_eq!(a.result.as_str(), "cancelled", "{a:?}");
    assert!(started.elapsed() < Duration::from_secs(8), "{:?}", started.elapsed());
    assert!(names(&backup_dir).is_empty() && partials(&backup_dir).is_empty());
    assert_eq!(backups::read_status(&backup_dir).unwrap().result.as_str(), "cancelled");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn shutdown_cancels_a_running_scheduled_backup() {
    let env = boot_with(|t| format!("backup_interval_seconds = 1\n{t}")).await;
    fill(&bridge_coordinator::db_path(&env.dir), 80);
    let backup_dir = env.dir.join("backups");
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while partials(&backup_dir).is_empty() {
        assert!(std::time::Instant::now() < deadline, "no backup started");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let Env { dir, srv, .. } = env;
    let started = std::time::Instant::now();
    tokio::time::timeout(Duration::from_secs(10), srv.stop())
        .await
        .expect("stop returns while a backup runs");
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(partials(&backup_dir).is_empty(), "{:?}", partials(&backup_dir));
    assert_eq!(backups::read_status(&backup_dir).unwrap().result.as_str(), "cancelled");
    let _ = std::fs::remove_dir_all(&dir);
}
