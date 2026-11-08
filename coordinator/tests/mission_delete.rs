//! Mission deletion (PRD-038): the replicated tombstone, the REST resource and the command result path.
mod common;
use common::*;
use reqwest::Method;
use serde_json::{json, Value};

fn event(id: u64, mission: &str) -> Value {
    json!({"id": id, "mission_id": mission, "kind": "mission-status", "data": {}})
}

fn summary(mission: &str) -> Value {
    json!({"id": mission, "name": mission, "status": "COMPLETED", "folder": "proj", "updated_at": "2026-11-08T00:00:00+00:00"})
}

fn listed(list: &Value) -> Vec<String> {
    list["missions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["mission"]["id"].as_str().unwrap().to_string())
        .collect()
}

fn stored_event_missions(env: &Env) -> Vec<String> {
    let mut found: Vec<String> = env
        .srv
        .state
        .db
        .events_list(Some("w1"), 0, 1000)
        .unwrap()
        .iter()
        .map(|row| row["event"]["mission_id"].as_str().unwrap().to_string())
        .collect();
    found.sort();
    found
}

fn audit_count(env: &Env, action: &str) -> usize {
    env.srv.state.db.audit_list(200).unwrap().iter().filter(|row| row["action"] == action).count()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_tombstone_removes_the_replica_and_blocks_a_late_replication_of_the_mission() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    fw.send(
        "events",
        json!({"after": 0, "through": 3, "mission_cursor": 3, "events": [event(1, "m1"), event(2, "m2"), event(3, "m2")],
               "missions": [summary("m1"), summary("m2")]}),
    )
    .await;
    fw.barrier().await;
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1", "m2"]);
    assert_eq!(stored_event_missions(&env), ["m1", "m2", "m2"]);

    // The worker deleted m2: its summary and stored events go, the cursor does not move.
    fw.send("events", json!({"after": 3, "through": 3, "events": [], "deleted_missions": ["m2"]}))
        .await;
    fw.barrier().await;
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"]);
    assert_eq!(stored_event_missions(&env), ["m1"]);
    assert!(env.srv.state.db.mission_tombstoned("w1", "m2").unwrap());
    assert_eq!(audit_count(&env, "mission.delete"), 1);

    // A batch that was already on its way (or queued on a worker that was offline) still carries m2.
    fw.send(
        "events",
        json!({"after": 3, "through": 6, "mission_cursor": 6, "events": [event(4, "m2"), event(5, "m1"), event(6, "m2")],
               "missions": [summary("m2"), summary("m1")]}),
    )
    .await;
    fw.barrier().await;
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"], "the deleted mission was resurrected");
    assert_eq!(stored_event_missions(&env), ["m1", "m1"]);
    let (_, workers) = api.get("/api/v1/workers").await;
    assert_eq!(
        workers["workers"][0]["event_cursor"], 6,
        "the cursor still moves past the dropped events: {workers}"
    );

    // A repeated tombstone is accepted, changes nothing and is audited once.
    fw.send("events", json!({"after": 6, "through": 6, "events": [], "deleted_missions": ["m2"]}))
        .await;
    fw.barrier().await;
    assert_eq!(audit_count(&env, "mission.delete"), 1);
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"]);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_tombstone_for_a_mission_the_coordinator_never_saw_is_accepted() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    fw.send("events", json!({"after": 0, "through": 0, "events": [], "deleted_missions": ["never-seen"]}))
        .await;
    fw.barrier().await;
    assert!(env.srv.state.db.mission_tombstoned("w1", "never-seen").unwrap());
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"]);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn malformed_deleted_missions_are_rejected_without_changing_anything() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let too_many: Vec<String> = (0..101).map(|n| format!("m{n}")).collect();
    for bad in [json!("m1"), json!(["bad id"]), json!([7]), json!(too_many)] {
        fw.send("events", json!({"after": 0, "through": 0, "events": [], "deleted_missions": bad}))
            .await;
        let reply = loop {
            let m = fw.recv().await;
            if m["type"] == "error" {
                break m;
            }
        };
        assert_eq!(reply["payload"]["code"], "invalid-message", "{reply}");
    }
    assert!(!env.srv.state.db.mission_tombstoned("w1", "m1").unwrap());
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"]);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tombstones_are_bounded_by_age_and_count_and_a_hello_that_lists_the_mission_clears_one() {
    let env = boot().await;
    let api = env.admin().await;
    let _token = api.add_worker("w1").await;
    let db = &env.srv.state.db;

    let ids: Vec<String> = (0..10_005).map(|n| format!("old-{n:05}")).collect();
    db.missions_tombstone("w1", &ids).unwrap();
    let kept = rusqlite::Connection::open(env.dir.join("coordinator.sqlite3"))
        .unwrap()
        .query_row("SELECT COUNT(*) FROM mission_tombstones WHERE worker='w1'", [], |r| r.get::<_, i64>(0))
        .unwrap();
    assert_eq!(kept, bridge_coordinator::db::MAX_TOMBSTONES_PER_WORKER);

    db.missions_tombstone("w1", &["aged".to_string()]).unwrap();
    rusqlite::Connection::open(env.dir.join("coordinator.sqlite3"))
        .unwrap()
        .execute("UPDATE mission_tombstones SET deleted_at=1 WHERE mission_id='aged'", [])
        .unwrap();
    db.maintenance().unwrap();
    assert!(
        !db.mission_tombstoned("w1", "aged").unwrap(),
        "a tombstone older than the retention window is dropped"
    );

    db.missions_tombstone("w1", &["m9".to_string()]).unwrap();
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([summary("m9")]));
    db.worker_hello("w1", "wid1", &hello, &caps(), "127.0.0.1").unwrap();
    assert!(
        !db.mission_tombstoned("w1", "m9").unwrap(),
        "the worker's own list wins over an older tombstone"
    );
    env.stop().await;
}

async fn answer(fw: &mut Fw, outcome: &str, result: Value, error: Value) -> Value {
    let command = loop {
        let m = fw.recv().await;
        if m["type"] == "command" {
            break m;
        }
    };
    let id = command["payload"]["request_id"].as_str().unwrap().to_string();
    fw.send("command.ack", json!({"request_id": id, "status": "received"})).await;
    fw.send(
        "command.result",
        json!({"schema_version": 1, "request_id": id, "operation": "mission.delete", "outcome": outcome, "result": result, "error": error}),
    )
    .await;
    fw.barrier().await;
    command
}

#[tokio::test(flavor = "multi_thread")]
async fn the_delete_resource_needs_a_confirmation_and_the_replica_goes_when_the_worker_reports_the_deletion() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect_settled(&env, &token, "wid1", 0).await;
    fw.send(
        "events",
        json!({"after": 0, "through": 2, "mission_cursor": 2, "events": [event(1, "m2"), event(2, "m3")], "missions": [summary("m2"), summary("m3")]}),
    )
    .await;
    fw.barrier().await;
    let path = "/api/v1/missions/m2?worker=w1";

    // Nothing is queued without an explicit confirm: no body, false and a wrong type are all refused.
    for body in [None, Some(json!({})), Some(json!({"confirm": false})), Some(json!({"confirm": "yes"}))] {
        let rb = api.rb(Method::DELETE, path);
        let (status, response) = api.send(if let Some(b) = body { rb.json(&b) } else { rb }).await;
        assert_eq!(status, 400, "{response}");
        assert_eq!(response["code"], "confirmation-required", "{response}");
    }
    assert_eq!(audit_count(&env, "command.submit"), 0);

    // Confirmed: a typed mission.delete reaches the worker. The worker refuses (the mission is running).
    let (status, queued) = api.send(api.rb(Method::DELETE, path).json(&json!({"confirm": true}))).await;
    assert_eq!(status, 202, "{queued}");
    assert_eq!(queued["operation"], "mission.delete");
    let command = answer(
        &mut fw,
        "rejected",
        Value::Null,
        json!({"code": "conflict", "message": "mission m2 is running, so it cannot be deleted", "details": {}}),
    )
    .await;
    assert_eq!(command["payload"]["operation"], "mission.delete");
    assert_eq!(command["payload"]["mission_id"], "m2");
    assert_eq!(command["payload"]["params"], json!({"confirm": true}));
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1", "m2", "m3"], "a refusal keeps the replica");
    assert!(!env.srv.state.db.mission_tombstoned("w1", "m2").unwrap());

    // Applied: the replica and its events go at once, without waiting for the next replication batch.
    api.send(api.rb(Method::DELETE, path).json(&json!({"confirm": true}))).await;
    answer(&mut fw, "applied", json!({"status": "DELETED", "mission_id": "m2", "name": "m2"}), Value::Null).await;
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1", "m3"]);
    assert_eq!(stored_event_missions(&env), ["m3"]);
    assert!(env.srv.state.db.mission_tombstoned("w1", "m2").unwrap());
    assert_eq!(audit_count(&env, "mission.delete"), 1);

    // The worker no longer knows the mission (its result of an earlier delivery was lost): the stale replica goes too.
    api.send(api.rb(Method::DELETE, "/api/v1/missions/m3?worker=w1").json(&json!({"confirm": true})))
        .await;
    answer(
        &mut fw,
        "rejected",
        Value::Null,
        json!({"code": "not-found", "message": "no mission matches m3", "details": {}}),
    )
    .await;
    assert_eq!(listed(&api.get("/api/v1/missions").await.1), ["m1"]);

    // Unknown query parameters and missing worker are refused like the other mission resources.
    let (status, _) = api.send(api.rb(Method::DELETE, "/api/v1/missions/m2").json(&json!({"confirm": true}))).await;
    assert_eq!(status, 400);
    let (status, _) = api
        .send(api.rb(Method::DELETE, "/api/v1/missions/m2?worker=w1&x=1").json(&json!({"confirm": true})))
        .await;
    assert_eq!(status, 400);
    env.stop().await;
}
