//! Touch order survives a slow clock, migration 18 is one atomic statement over a large database, and the refused count
//! never falls below the true number once the refused set is full.
mod common;
use bridge_coordinator::db::{Db, MAX_MISSIONS_PER_WORKER};
use common::*;
use serde_json::{json, Value};

fn old_database(rows: &[(&str, usize)]) -> std::path::PathBuf {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("old.sqlite3");
    drop(Db::open(&path).unwrap());
    let raw = rusqlite::Connection::open(&path).unwrap();
    raw.execute_batch("DROP INDEX missions_worker_touched; ALTER TABLE missions DROP COLUMN touched_at; PRAGMA user_version = 17;")
        .unwrap();
    raw.execute_batch("BEGIN").unwrap();
    for (worker, count) in rows {
        raw.execute("INSERT INTO workers(name, token_hash, created_at) VALUES(?1, 'h-' || ?1, 0)", [worker])
            .unwrap();
        for i in 0..*count {
            // A scrambled order of `updated_at`, so the numbering has to sort.
            let day = (i * 7919) % count;
            raw.execute(
                "INSERT INTO missions(worker, mission_id, info) VALUES(?, ?, ?)",
                rusqlite::params![worker, format!("m{i:06}"), format!("{{\"updated_at\":\"2026-{day:08}\"}}")],
            )
            .unwrap();
        }
    }
    raw.execute_batch("COMMIT").unwrap();
    path
}

#[test]
fn touches_stay_above_every_stored_value_when_the_clock_is_behind() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("clock.sqlite3");
    drop(Db::open(&path).unwrap());
    let ahead: i64 = 4_000_000_000_000_000_000; // far beyond any microsecond clock reading
    {
        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute("INSERT INTO workers(name, token_hash, created_at) VALUES('w', 'h', 0)", [])
            .unwrap();
        raw.execute("INSERT INTO missions(worker, mission_id, info, touched_at) VALUES('w', 'm', '{}', ?)", [ahead])
            .unwrap();
    }
    let db = Db::open(&path).unwrap();
    let first = db.next_touch();
    assert!(first > ahead, "{first} must be above the stored {ahead}");
    assert!(db.next_touch() > first, "and strictly increasing");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn migration_18_numbers_a_large_database_in_updated_at_order_per_worker() {
    let path = old_database(&[("a", 20_000), ("b", 10_000)]);
    drop(Db::open(&path).unwrap());
    let raw = rusqlite::Connection::open(&path).unwrap();
    let version: i64 = raw.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(version, 18);
    for (worker, count) in [("a", 20_000i64), ("b", 10_000)] {
        let (min, max, distinct): (i64, i64, i64) = raw
            .query_row(
                "SELECT MIN(touched_at), MAX(touched_at), COUNT(DISTINCT touched_at) FROM missions WHERE worker=?",
                [worker],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!((min, max, distinct), (1, count, count), "worker {worker} is numbered 1..n once each");
        // The number follows `updated_at` (the day value is a permutation, so the mission with day 0 is number 1).
        let ordered: i64 = raw
            .query_row(
                "SELECT COUNT(*) FROM (SELECT touched_at, LAG(touched_at) OVER (ORDER BY json_extract(info,'$.updated_at')) AS p \
                 FROM missions WHERE worker=?) WHERE p IS NOT NULL AND touched_at <> p + 1",
                [worker],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(ordered, 0);
    }
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
}

#[test]
fn a_failed_backfill_leaves_version_17_and_no_column() {
    let path = old_database(&[("a", 50)]);
    {
        let raw = rusqlite::Connection::open(&path).unwrap();
        // Any update of a mission row fails: the backfill is the only statement that updates them.
        raw.execute_batch("CREATE TRIGGER no_update BEFORE UPDATE ON missions BEGIN SELECT RAISE(ABORT, 'boom'); END;")
            .unwrap();
    }
    assert!(Db::open(&path).is_err(), "the migration fails");
    let raw = rusqlite::Connection::open(&path).unwrap();
    let version: i64 = raw.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(version, 17, "the version bump is in the transaction of the backfill");
    let has_column: i64 = raw
        .query_row("SELECT COUNT(*) FROM pragma_table_info('missions') WHERE name='touched_at'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(has_column, 0, "so is the new column");
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
}

fn summary(id: &str, status: &str) -> Value {
    json!({"id": id, "name": "mission", "status": status, "folder": "project-dir", "updated_at": "2026-01-01"})
}

#[tokio::test(flavor = "multi_thread")]
async fn the_refused_count_is_not_below_the_true_number_when_the_set_is_full() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    let ws = Fw::raw(&env, &token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: "wid-a".into(),
        generation: 0,
        seq: 0,
    };
    let first: Vec<Value> = (0..3000).map(|i| summary(&format!("a{i:05}"), "RUNNING")).collect();
    fw.send(
        "hello",
        json!({"worker_id": "wid-a", "name": "a", "software_version": "test", "protocol_versions": [1],
               "platform": {"system": "Linux"}, "capabilities": caps(), "missions": first, "event_cursor": 0}),
    )
    .await;
    let welcome = fw.recv().await;
    assert_eq!(welcome["type"], "welcome", "{welcome}");
    fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
    // Fill the cap with active missions, then offer 10 050 more: 10 000 fit in the refused set and 50 do not.
    let mut sent = 3000usize;
    let mut next = 0usize;
    while sent < MAX_MISSIONS_PER_WORKER as usize + 10_050 {
        let n = 3000.min(MAX_MISSIONS_PER_WORKER as usize + 10_050 - sent);
        let batch: Vec<Value> = (0..n).map(|i| summary(&format!("b{:06}", next + i), "RUNNING")).collect();
        next += n;
        sent += n;
        fw.send(
            "events",
            json!({"after": 0, "through": 0, "events": [], "mission_cursor": 0, "missions": batch}),
        )
        .await;
        for _ in 0..2 {
            fw.send("ping", json!({})).await;
            loop {
                let m = fw.recv().await;
                if m["type"] == "pong" {
                    break;
                }
                assert_ne!(m["type"], "error", "{m}");
            }
        }
    }
    let warnings = env.srv.state.db.diagnostics().unwrap()["warnings"].clone();
    let refused = warnings[0]["missions_refused"].as_u64().unwrap();
    assert!(refused >= 10_050, "10 050 ids were refused, the warning says {refused}");
    env.stop().await;
}
