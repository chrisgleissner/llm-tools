//! Replayed mission summaries rank older than stored ones, the refused count tracks distinct ids, and the eviction
//! query uses its index. Migration v18 numbers existing rows.
mod common;
use bridge_coordinator::db::{Db, MAX_MISSIONS_PER_WORKER};
use common::*;
use serde_json::{json, Value};

fn summary(id: &str, status: &str, updated_at: &str) -> Value {
    json!({"id": id, "name": "mission", "status": status, "folder": "project-dir", "updated_at": updated_at})
}

async fn connect(env: &Env, token: &str, name: &str, missions: Vec<Value>) -> Fw {
    let ws = Fw::raw(env, token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: format!("wid-{name}"),
        generation: 0,
        seq: 0,
    };
    fw.send(
        "hello",
        json!({"worker_id": format!("wid-{name}"), "name": name, "software_version": "test", "protocol_versions": [1],
               "platform": {"system": "Linux"}, "capabilities": caps(), "missions": missions, "event_cursor": 0}),
    )
    .await;
    let welcome = fw.recv().await;
    assert_eq!(welcome["type"], "welcome", "{welcome}");
    fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
    fw
}

/// Ping twice and collect every message up to the second pong (the outbound queue and a ping reply are separate paths).
async fn drain(fw: &mut Fw) -> Vec<Value> {
    let mut seen = Vec::new();
    for _ in 0..2 {
        fw.send("ping", json!({})).await;
        loop {
            let m = fw.recv().await;
            if m["type"] == "pong" {
                break;
            }
            seen.push(m);
        }
    }
    seen
}

async fn send_missions(fw: &mut Fw, missions: Vec<Value>, replay: bool) -> Vec<Value> {
    let mut payload = json!({"after": 0, "through": 0, "events": [], "mission_cursor": 0, "missions": missions});
    if replay {
        payload["replay"] = json!(true);
    }
    fw.send("events", payload).await;
    let seen = drain(fw).await;
    assert!(seen.iter().all(|m| m["type"] != "error"), "{seen:?}");
    seen
}

fn ids(env: &Env, worker: &str) -> std::collections::BTreeSet<String> {
    env.srv
        .state
        .db
        .missions_list()
        .unwrap()
        .into_iter()
        .filter(|(w, _)| w == worker)
        .map(|(_, info)| info["id"].as_str().unwrap().to_string())
        .collect()
}

fn finished(prefix: &str, range: std::ops::Range<usize>) -> Vec<Value> {
    range.map(|i| summary(&format!("{prefix}{i:05}"), "COMPLETED", "2026-01-01")).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn replayed_rows_rank_below_the_hello_rows_and_live_updates() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    // 5000 finished rows in the hello (the newest ones), then 5001 older rows replayed newest first: 10 001 in all.
    let mut fw = connect(&env, &token, "a", finished("h", 0..5000)).await;
    send_missions(&mut fw, finished("r", 0..5001), true).await;
    let stored = ids(&env, "a");
    assert_eq!(stored.len() as u64, MAX_MISSIONS_PER_WORKER);
    assert!(!stored.contains("r05000"), "the last replayed row is the oldest and goes first");
    assert!((0..5000).all(|i| stored.contains(&format!("h{i:05}"))), "no hello row was evicted");
    // A live update is newer than everything: it evicts the next oldest replayed row, again no hello row.
    send_missions(&mut fw, finished("live", 0..1), false).await;
    let stored = ids(&env, "a");
    assert!(stored.contains("live00000") && !stored.contains("r04999"));
    assert!((0..5000).all(|i| stored.contains(&format!("h{i:05}"))));
    env.stop().await;
}

async fn fill_with_active(env: &Env, fw: &mut Fw) {
    for (prefix, count) in [("b", 3000), ("c", 3000), ("d", 1000)] {
        send_missions(
            fw,
            (0..count).map(|i| summary(&format!("{prefix}{i:05}"), "RUNNING", "2026-01-01")).collect(),
            false,
        )
        .await;
    }
    assert_eq!(ids(env, "a").len() as u64, MAX_MISSIONS_PER_WORKER);
}

fn refused(env: &Env) -> Option<u64> {
    env.srv.state.db.diagnostics().unwrap()["warnings"][0]["missions_refused"].as_u64()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_refused_count_is_distinct_ids_and_stays_until_each_is_stored_or_deleted() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    let mut fw = connect(
        &env,
        &token,
        "a",
        (0..3000).map(|i| summary(&format!("a{i:05}"), "RUNNING", "2026-01-01")).collect(),
    )
    .await;
    fill_with_active(&env, &mut fw).await;
    let notices = |seen: &[Value]| seen.iter().filter(|m| m["type"] == "notice").count();
    // The same refused id three times counts once; with the second id the worker was told exactly twice in all
    // (a notice and a pong take separate paths, so the notices are collected over the whole sequence).
    let mut all = Vec::new();
    for _ in 0..3 {
        all.extend(send_missions(&mut fw, vec![summary("x1", "RUNNING", "2026-01-01")], false).await);
    }
    assert_eq!(refused(&env), Some(1));
    all.extend(send_missions(&mut fw, vec![summary("x2", "RUNNING", "2026-01-01")], false).await);
    for _ in 0..3 {
        all.extend(drain(&mut fw).await);
    }
    assert_eq!((refused(&env), notices(&all)), (Some(2), 2), "{all:?}");
    // Still at the cap and nothing was accepted: an update of a stored mission leaves the count at 2.
    send_missions(&mut fw, vec![summary("a00000", "RUNNING", "2026-02-01")], false).await;
    assert_eq!(refused(&env), Some(2));
    // Deleting one refused mission removes it from the count.
    fw.send("events", json!({"after": 0, "through": 0, "events": [], "deleted_missions": ["x1"]}))
        .await;
    drain(&mut fw).await;
    assert_eq!(refused(&env), Some(1));
    // Room for the other one: delete a stored mission, resend it, and the warning is gone.
    fw.send("events", json!({"after": 0, "through": 0, "events": [], "deleted_missions": ["a00001"]}))
        .await;
    drain(&mut fw).await;
    let mut seen = send_missions(&mut fw, vec![summary("x2", "RUNNING", "2026-01-01")], false).await;
    seen.extend(drain(&mut fw).await);
    assert!(ids(&env, "a").contains("x2"));
    assert_eq!(env.srv.state.db.diagnostics().unwrap()["warnings"], json!([]));
    let last = seen
        .iter()
        .rev()
        .find(|m| m["type"] == "notice")
        .expect("the worker is told the warning cleared");
    assert_eq!(last["payload"]["refused"].as_u64(), Some(0));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn the_eviction_query_uses_the_touched_at_index() {
    let env = boot().await;
    let plan = env.srv.state.db.eviction_plan().unwrap();
    assert!(plan.contains("missions_worker_touched"), "{plan}");
    assert!(!plan.contains("USE TEMP B-TREE"), "the order comes from the index: {plan}");
    env.stop().await;
}

#[test]
fn migration_18_numbers_existing_missions_in_updated_at_order_and_adds_the_index() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("old.sqlite3");
    drop(Db::open(&path).unwrap());
    {
        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute_batch(
            "INSERT INTO workers(name, token_hash, created_at) VALUES('w', 'h', 0);
             INSERT INTO missions(worker, mission_id, info) VALUES('w', 'm-new', '{\"updated_at\":\"2026-03-01\"}'), ('w', 'm-old', '{\"updated_at\":\"2026-01-01\"}'), ('w', 'm-mid', '{\"updated_at\":\"2026-02-01\"}');
             DROP INDEX missions_worker_touched; ALTER TABLE missions DROP COLUMN touched_at; DROP TABLE worker_refused_missions; PRAGMA user_version = 17;",
        )
        .unwrap();
    }
    drop(Db::open(&path).unwrap());
    let raw = rusqlite::Connection::open(&path).unwrap();
    let rows: Vec<(String, i64)> = raw
        .prepare("SELECT mission_id, touched_at FROM missions ORDER BY touched_at")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(rows, vec![("m-old".to_string(), 1), ("m-mid".to_string(), 2), ("m-new".to_string(), 3)]);
    let index: i64 = raw
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='missions_worker_touched'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(index, 1);
    let _ = std::fs::remove_dir_all(&dir);
}
