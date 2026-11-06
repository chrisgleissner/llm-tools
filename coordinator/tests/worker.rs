mod common;
use common::*;
use futures_util::{SinkExt, StreamExt};
use reqwest::Method;
use rustls::pki_types::pem::PemObject;
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message as WsMsg;

fn submit(id: &str) -> Value {
    json!({"target": "w1", "request": request(id, "task.add")})
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_authentication() {
    let env = boot().await;
    let api = env.admin().await;
    // no token, wrong token
    for headers in [vec![], vec![("authorization", "Bearer nope")], vec![("authorization", "Basic abc")]] {
        let err = env.ws("/api/v1/worker/connect", &headers).await.expect_err("rejected");
        assert!(format!("{err:?}").contains("401"), "{err:?}");
    }
    let token = api.add_worker("w1").await;
    assert_eq!(api.post("/api/v1/workers", json!({"name": "w1"})).await.0, 409);
    assert_eq!(api.post("/api/v1/workers", json!({"name": "Bad Name"})).await.0, 400);
    let (mut fw, w) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(w["payload"]["generation"], 1);
    let (_, list) = api.get("/api/v1/workers").await;
    assert_eq!(list["workers"][0]["state"], "online");
    assert_eq!(list["workers"][0]["id"], "wid1");
    assert_eq!(list["workers"][0]["host_name"], "test-host");
    assert_eq!(list["workers"][0]["remote_address"], "127.0.0.1");
    let (detail_status, detail) = api.get("/api/v1/workers/w1").await;
    assert_eq!(detail_status, 200, "{detail}");
    assert_eq!(detail["host_name"], "test-host");
    assert_eq!(detail["remote_address"], "127.0.0.1");
    // The stored last_seen is written only every ten minutes; a connected worker still reads as seen now.
    env.srv.state.db.worker_age("w1", 1).unwrap();
    let (_, list) = api.get("/api/v1/workers").await;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64;
    assert!(now - list["workers"][0]["last_seen"].as_i64().unwrap() <= 5, "{list}");
    let (_, missions) = api.get("/api/v1/missions").await;
    assert_eq!(missions["missions"][0]["mission"]["id"], "m1");
    assert_eq!(missions["missions"][0]["stale"], false);

    // revoke closes the live connection and the token stops working
    let (st, _) = api.send(api.rb(Method::DELETE, "/api/v1/workers/w1")).await;
    assert_eq!(st, 200);
    assert_eq!(fw.expect_close().await, Some(1008));
    let (_, offline_detail) = api.get("/api/v1/workers/w1").await;
    assert!(offline_detail["remote_address"].is_null());
    let err = Fw::raw(&env, &token).await.expect_err("revoked");
    assert!(format!("{err:?}").contains("401"));
    assert_eq!(api.get("/api/v1/workers").await.1["workers"].as_array().unwrap().len(), 0);
    let audit = env.srv.state.db.audit_list(50).unwrap();
    assert!(audit.iter().any(|r| r["action"] == "worker.add") && audit.iter().any(|r| r["action"] == "worker.revoke"));
    assert!(!audit.iter().any(|r| r.to_string().contains(&token)));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_disconnect_clears_active_peer_address_but_keeps_enrolment() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(api.get("/api/v1/workers/w1").await.1["remote_address"], "127.0.0.1");

    fw.ws.close(None).await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let worker = api.get("/api/v1/workers/w1").await.1;
            if worker["state"] == "offline" {
                assert!(worker["remote_address"].is_null(), "{worker}");
                assert_eq!(worker["host_name"], "test-host");
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("worker disconnect is reflected");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn event_batch_updates_mission_inventory_and_caught_up_status() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;

    fw.send(
        "events",
        json!({
            "after": 0,
            "through": 4,
            "mission_cursor": 4,
            "events": [
                {"id": 1, "mission_id": "m1", "kind": "mission-status", "data": {"to": "PAUSED"}},
                {"id": 2, "mission_id": "m2", "kind": "mission-created", "data": {}},
                {"id": 3, "mission_id": "m3", "kind": "mission-status", "data": {"to": "RUNNING"}},
                {"id": 4, "mission_id": "m4", "kind": "mission-status", "data": {"to": "STOPPED"}}
            ],
            "missions": [
                {"id": "m1", "name": "mission", "status": "PAUSED", "folder": "proj", "updated_at": "2026-11-06T12:00:00+00:00"},
                {"id": "m2", "name": "created while connected", "status": "CREATED", "folder": "new", "updated_at": "2026-11-06T12:00:00+00:00"},
                {"id": "m3", "name": "started while connected", "status": "RUNNING", "folder": "active", "updated_at": "2026-11-06T12:00:00+00:00"},
                {"id": "m4", "name": "stopped while connected", "status": "STOPPED", "folder": "done", "updated_at": "2026-11-06T12:00:00+00:00"}
            ]
        }),
    )
    .await;
    fw.barrier().await;

    let (_, list) = api.get("/api/v1/missions").await;
    let missions = list["missions"].as_array().unwrap();
    assert_eq!(missions.len(), 4, "{list}");
    assert_eq!(missions[0]["mission"]["status"], "PAUSED");
    assert_eq!(missions[1]["mission"]["id"], "m2");
    assert_eq!(missions[2]["mission"]["status"], "RUNNING");
    assert_eq!(missions[3]["mission"]["status"], "STOPPED");
    assert!(missions.iter().all(|mission| mission["stale"] == false), "{list}");

    // A provider question may appear without a local Bridge event. The worker
    // can replicate just that changed mission summary with an empty event batch.
    fw.send(
        "events",
        json!({
            "after": 4,
            "through": 4,
            "mission_cursor": 4,
            "events": [],
            "missions": [{
                "id": "m3", "name": "started while connected", "status": "RUNNING", "folder": "active",
                "updated_at": "2026-11-06T12:00:00+00:00",
                "needs_you": {"kind": "provider", "message": "Codex asks: Reconnect?", "command": "llm-bridge attach --mission m3"}
            }]
        }),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    let active = list["missions"].as_array().unwrap().iter().find(|m| m["mission"]["id"] == "m3").unwrap();
    assert_eq!(active["mission"]["needs_you"]["message"], "Codex asks: Reconnect?");

    // An event-only legacy batch advances the event cursor but cannot claim its mission
    // inventory is current, so the coordinator accurately marks the copy stale.
    fw.send("events", json!({"after": 4, "through": 5, "events": [{"id": 5}]})).await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    assert!(list["missions"].as_array().unwrap().iter().all(|mission| mission["stale"] == true), "{list}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn command_round_trip() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;

    let (st, entry) = api.post("/api/v1/commands", submit("req-1")).await;
    assert_eq!(st, 202, "{entry}");
    let cmd = fw.recv().await;
    assert_eq!(cmd["type"], "command");
    assert_eq!(cmd["generation"], 1);
    assert_eq!(cmd["worker_id"], "wid1");
    assert_eq!(cmd["payload"]["request_id"], "req-1");
    assert_eq!(cmd["payload"]["target"], "local");
    fw.send("command.ack", json!({"request_id": "req-1", "status": "received"})).await;
    fw.barrier().await;
    assert_eq!(api.get("/api/v1/commands/req-1").await.1["state"], "acked");
    let result = json!({"schema_version": 1, "request_id": "req-1", "operation": "task.add", "outcome": "applied", "result": {"task": 1}, "error": null});
    fw.send("command.result", result.clone()).await;
    fw.barrier().await;
    let (st, done) = api.get("/api/v1/commands/req-1").await;
    assert_eq!((st, done["state"].as_str()), (200, Some("done")));
    assert_eq!(done["result"], result);
    // a repeated or unknown result is ignored
    fw.send("command.result", result.clone()).await;
    fw.send(
        "command.result",
        json!({"schema_version": 1, "request_id": "ghost", "operation": "task.add", "outcome": "ok", "result": null, "error": null}),
    )
    .await;
    fw.barrier().await;
    assert_eq!(api.get("/api/v1/commands/ghost").await.0, 404);
    assert_eq!(api.get("/api/v1/commands/req-1").await.1["result"], result);

    // wait_ms returns the result of a read operation once the worker answers
    let api2 = api.c.clone();
    let rb = api
        .rb(Method::POST, "/api/v1/commands")
        .json(&json!({"target": "w1", "request": request("req-wait", "mission.list"), "wait_ms": 5000}));
    let waiting = tokio::spawn(async move {
        let _ = api2;
        rb.send().await.unwrap().json::<Value>().await.unwrap()
    });
    let cmd = fw.recv().await;
    assert_eq!(cmd["payload"]["request_id"], "req-wait");
    fw.send(
        "command.result",
        json!({"schema_version": 1, "request_id": "req-wait", "operation": "mission.list", "outcome": "ok", "result": {"missions": []}, "error": null}),
    )
    .await;
    let body = waiting.await.unwrap();
    assert_eq!(body["state"], "done");
    assert_eq!(body["mutating"], false);

    // mutating submissions are audited, without parameters
    let audit = env.srv.state.db.audit_list(50).unwrap();
    let rows: Vec<_> = audit.iter().filter(|r| r["action"] == "command.submit").collect();
    assert_eq!(rows.len(), 1, "only mutating operations are audited: {audit:?}");
    assert!(rows[0]["detail"].as_str().unwrap().contains("req-1"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn redelivery_after_reconnect_and_generation() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    // capabilities are only known after a first hello
    assert_eq!(api.post("/api/v1/commands", submit("early")).await.0, 422);
    let (mut a, w) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(w["payload"]["generation"], 1);
    // a second connection of the same worker supersedes the first
    let (mut b, w) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(w["payload"]["generation"], 2);
    assert_eq!(a.expect_close().await, Some(1000));
    assert_eq!(api.get("/api/v1/workers").await.1["workers"][0]["generation"], 2);

    // submit, receive, drop without acknowledging
    assert_eq!(api.post("/api/v1/commands", submit("req-r")).await.0, 202);
    let cmd = b.recv().await;
    assert_eq!(cmd["generation"], 2);
    b.ws.close(None).await.ok();
    drop(b);
    // offline: the command is journaled and queued
    assert_eq!(api.post("/api/v1/commands", submit("req-off")).await.0, 202);
    let (st, e) = api.get("/api/v1/commands/req-off").await;
    assert_eq!((st, e["state"].as_str()), (200, Some("pending")));

    // reconnect: both are redelivered in order with the new generation
    let (mut c, w) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(w["payload"]["generation"], 3);
    let first = c.recv().await;
    let second = c.recv().await;
    assert_eq!(
        (first["payload"]["request_id"].as_str(), second["payload"]["request_id"].as_str()),
        (Some("req-r"), Some("req-off"))
    );
    assert_eq!(first["generation"], 3);
    assert_eq!(api.get("/api/v1/commands/req-r").await.1["state"], "sent");
    // still no ack: another reconnect redelivers again (worker-side idempotence handles duplicates)
    drop(c);
    let (mut d, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let again = d.recv().await;
    assert_eq!(again["payload"]["request_id"], "req-r");
    d.send("command.ack", json!({"request_id": "req-r", "status": "duplicate"})).await;
    d.send(
        "command.result",
        json!({"schema_version": 1, "request_id": "req-r", "operation": "task.add", "outcome": "applied", "result": null, "error": null}),
    )
    .await;
    let second = d.recv().await;
    assert_eq!(second["payload"]["request_id"], "req-off");
    d.barrier().await;
    assert_eq!(api.get("/api/v1/commands/req-r").await.1["state"], "done");
    // done commands are not redelivered
    drop(d);
    let (mut e, _) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(e.recv().await["payload"]["request_id"], "req-off");
    e.barrier().await;
    // graceful shutdown closes live worker connections within the time limit
    let stopped = tokio::spawn(env.stop());
    assert_eq!(e.expect_close().await, Some(1001));
    stopped.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn event_cursor_accept_and_nack() {
    let env = boot_with(|t| t.replace("max_events_per_worker = 50000", "max_events_per_worker = 4")).await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, w) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(w["payload"]["event_cursor"], 0);
    let ev = |ids: &[u64]| ids.iter().map(|i| json!({"id": i, "kind": "test", "mission_id": "m1"})).collect::<Vec<_>>();
    fw.send("events", json!({"after": 0, "through": 3, "events": ev(&[1, 2, 3])})).await;
    fw.barrier().await;
    assert_eq!(api.get("/api/v1/events?worker=w1").await.1["events"].as_array().unwrap().len(), 3);
    // a batch that does not start at the stored cursor is refused with the expected cursor
    fw.send("events", json!({"after": 1, "through": 5, "events": ev(&[4, 5])})).await;
    let nack = fw.recv().await;
    assert_eq!(nack["type"], "events.nack");
    assert_eq!(nack["payload"]["expected_after"], 3);
    fw.send("events", json!({"after": 0, "through": 3, "events": ev(&[1, 2, 3])})).await;
    assert_eq!(fw.recv().await["payload"]["expected_after"], 3, "replays are refused too");
    // holes in the id sequence are legal; the oldest events beyond the cap are pruned
    fw.send("events", json!({"after": 3, "through": 9, "events": ev(&[5, 7, 9])})).await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/events?worker=w1&limit=50").await;
    let ids: Vec<u64> = list["events"].as_array().unwrap().iter().map(|e| e["id"].as_u64().unwrap()).collect();
    assert_eq!(ids, vec![3, 5, 7, 9]);
    assert_eq!(api.get("/api/v1/events?worker=w1&after=5").await.1["events"].as_array().unwrap().len(), 2);
    // malformed batches are protocol errors, not stored
    fw.send("events", json!({"after": 9, "through": 10, "events": ev(&[11])})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message");
    // reconnect resumes from the stored cursor
    drop(fw);
    let (_fw, w) = Fw::connect(&env, &token, "wid1", 9).await;
    assert_eq!(w["payload"]["event_cursor"], 9);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn request_id_idempotence_and_refusals() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let (s1, e1) = api.post("/api/v1/commands", submit("same")).await;
    let (s2, e2) = api.post("/api/v1/commands", submit("same")).await;
    assert_eq!((s1, s2), (202, 202));
    assert_eq!(e1["created_at"], e2["created_at"]);
    assert_eq!(fw.recv().await["payload"]["request_id"], "same");
    fw.barrier().await; // no second delivery queued before the pong
                        // same id, different payload: conflict
    let keyed = api
        .rb(reqwest::Method::POST, "/api/v1/commands")
        .header("Idempotency-Key", "header-key")
        .json(&submit("body-key-a"));
    let response = keyed.send().await.unwrap();
    assert_eq!(response.status().as_u16(), 202);
    assert_eq!(response.headers()["location"], "/api/v1/commands/header-key");
    let first: serde_json::Value = response.json().await.unwrap();
    assert_eq!(first["request_id"], "header-key");
    assert_eq!(fw.recv().await["payload"]["request_id"], "header-key");
    fw.barrier().await;
    let retry = api
        .rb(reqwest::Method::POST, "/api/v1/commands")
        .header("Idempotency-Key", "header-key")
        .json(&submit("body-key-b"));
    let (status, second) = api.send(retry).await;
    assert_eq!(status, 202);
    assert_eq!(first["created_at"], second["created_at"]);
    fw.barrier().await;
    let mut other = submit("same");
    other["request"]["params"] = json!({"title": "different"});
    let (st, body) = api.post("/api/v1/commands", other).await;
    assert_eq!((st, body["error"]["code"].as_str()), (409, Some("request-id-conflict")));
    // unsupported operation is refused without sending
    let (st, body) = api
        .post("/api/v1/commands", json!({"target": "w1", "request": request("u1", "mission.delete")}))
        .await;
    assert_eq!((st, body["error"]["code"].as_str()), (422, Some("unsupported-operation")));
    assert_eq!(api.get("/api/v1/commands/u1").await.0, 404);
    // unknown and malformed targets and requests
    let (st, body) = api
        .post("/api/v1/commands", json!({"target": "nobody", "request": request("u2", "task.add")}))
        .await;
    assert_eq!((st, body["error"]["code"].as_str()), (404, Some("unknown-target")));
    assert_eq!(
        api.post("/api/v1/commands", json!({"target": "all", "request": request("u3", "task.add")}))
            .await
            .0,
        400
    );
    let mut bad = request("u4", "task.add");
    bad["extra"] = json!(1);
    assert_eq!(api.post("/api/v1/commands", json!({"target": "w1", "request": bad})).await.0, 400);
    let mut bad = request("bad id!", "task.add");
    assert_eq!(api.post("/api/v1/commands", json!({"target": "w1", "request": bad.clone()})).await.0, 400);
    bad["request_id"] = json!("ok");
    assert_eq!(
        api.post("/api/v1/commands", json!({"target": "w1", "request": bad, "wait_ms": 31000})).await.0,
        400
    );
    fw.barrier().await;
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn frame_limits_and_malformed_messages() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;

    // before hello: non-hello and garbage get an error and the socket stays usable
    let ws = Fw::raw(&env, &token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: "wid1".into(),
        generation: 0,
        seq: 0,
    };
    fw.send("ping", json!({})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "not-welcomed");
    fw.send_raw("not json".into()).await;
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message");
    fw.send_raw(json!({"v": 2, "type": "hello", "id": "x", "worker_id": "wid1", "generation": 0, "payload": {}}).to_string())
        .await;
    assert_eq!(fw.recv().await["payload"]["code"], "unsupported-version");
    assert_eq!(fw.expect_close().await, Some(1002));

    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    fw.send_raw("{broken".into()).await;
    let e = fw.recv().await;
    assert_eq!((e["type"].as_str(), e["payload"]["code"].as_str()), (Some("error"), Some("invalid-message")));
    fw.send("mystery", json!({})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "unsupported-message");
    fw.send("ping", json!({"extra": 1})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message", "unknown payload fields are rejected");
    fw.send("events", json!({"after": 0, "through": 1, "mission_cursor": 2, "events": [{"id": 1}]}))
        .await;
    assert_eq!(
        fw.recv().await["payload"]["code"],
        "invalid-message",
        "mission cursor cannot exceed the batch cursor"
    );
    fw.send("hello", json!({})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message");
    fw.generation = 99;
    fw.send("ping", json!({})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "stale-generation");
    fw.generation = 1;
    fw.send("events.nack", json!({"expected_after": 0})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "unsupported-message");
    fw.ws.send(WsMsg::Binary(vec![1, 2, 3].into())).await.unwrap();
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message");
    fw.barrier().await; // still healthy after all of that

    // an oversized frame closes the connection with 1009
    fw.send_raw("x".repeat(1024 * 1024 + 1)).await;
    assert_eq!(fw.expect_close().await, Some(1009));

    // a message at the limit is processed (and rejected for its content, not its size)
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let pad = "y".repeat(1024 * 1024 - 200);
    fw.send_raw(
        json!({"v": 1, "type": "error", "id": "big", "worker_id": "wid1", "generation": fw.generation, "payload": {"code": "c", "message": pad}}).to_string(),
    )
    .await;
    fw.barrier().await;

    // a different worker id for the same worker name is refused
    let (mut other, _) = (Fw::raw(&env, &token).await.unwrap(), ());
    other.send(WsMsg::Text(json!({"v": 1, "type": "hello", "id": "h", "worker_id": "impostor", "generation": 0, "payload": {
        "worker_id": "impostor", "name": "w1", "software_version": "t", "protocol_versions": [1], "platform": {}, "capabilities": [], "missions": [], "event_cursor": 0}}).to_string().into())).await.unwrap();
    let reply: Value = loop {
        if let Some(Ok(WsMsg::Text(t))) = other.next().await {
            break serde_json::from_str(t.as_str()).unwrap();
        }
    };
    assert_eq!(reply["payload"]["code"], "worker-id-mismatch");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_broadcast() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let origin = env.origin();
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let mut stream = env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.unwrap();
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let m = next(&mut stream).await;
    assert_eq!((m["type"].as_str(), m["state"].as_str()), (Some("worker"), Some("online")));
    fw.send("events", json!({"after": 0, "through": 1, "events": [{"id": 1}]})).await;
    let m = next(&mut stream).await;
    assert_eq!(m["type"], "event");
    assert_eq!(m["events"][0]["id"], 1);
    api.post("/api/v1/commands", submit("s1")).await;
    assert_eq!(next(&mut stream).await["type"], "command");
    drop(fw);
    loop {
        let m = next(&mut stream).await;
        if m["type"] == "worker" && m["state"] == "offline" {
            break;
        }
    }
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tls_versions_and_suites() {
    let env = boot().await;
    use tokio::net::TcpStream;
    let ca = std::fs::read(env.dir.join("ca.pem")).unwrap();
    let mut roots = rustls::RootCertStore::empty();
    for c in rustls::pki_types::CertificateDer::pem_slice_iter(ca.as_slice()) {
        roots.add(c.unwrap()).unwrap();
    }
    let connect = |versions: &[&'static rustls::SupportedProtocolVersion]| {
        let cfg = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
            .with_protocol_versions(versions)
            .unwrap()
            .with_root_certificates(roots.clone())
            .with_no_client_auth();
        let addr = env.addr;
        async move {
            let tcp = TcpStream::connect(addr).await.unwrap();
            let s = tokio_rustls::TlsConnector::from(std::sync::Arc::new(cfg))
                .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
                .await?;
            Ok::<String, std::io::Error>(format!("{:?}", s.get_ref().1.negotiated_cipher_suite().unwrap().suite()))
        }
    };
    assert!(connect(&[&rustls::version::TLS13]).await.unwrap().starts_with("TLS13_"));
    assert!(
        connect(&[&rustls::version::TLS12]).await.is_err(),
        "TLS 1.2 requires an explicit compatibility opt-in"
    );
    let _ = Method::GET;
    env.stop().await;
}

async fn next(ws: &mut Ws) -> Value {
    match tokio::time::timeout(std::time::Duration::from_secs(10), ws.next()).await.unwrap() {
        Some(Ok(WsMsg::Text(t))) => serde_json::from_str::<Value>(t.as_str()).unwrap(),
        other => panic!("unexpected {other:?}"),
    }
}

/// COORD-004 / DATA-007: completed commands are deleted after 7 days by the maintenance pass;
/// newer completed commands and commands that have not completed are kept.
#[test]
fn completed_command_retention() {
    use bridge_coordinator::db::{Db, COMPLETED_COMMAND_KEEP_SECS};
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let db = Db::open(&bridge_coordinator::db_path(&dir)).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    let now = bridge_coordinator::now();
    let add = |id: &str, done: bool, age: i64| {
        db.command_insert("w1", "mission.list", false, &request(id, "mission.list")).unwrap();
        if done {
            db.command_done("w1", id, &serde_json::json!({"ok": true})).unwrap().expect("done");
        }
        db.command_age(id, now - age).unwrap();
    };
    let keep = COMPLETED_COMMAND_KEEP_SECS;
    add("old-done", true, keep + 3600);
    add("fresh-done", true, keep - 3600);
    add("old-open", false, keep + 3600);
    db.maintenance().unwrap();
    assert!(db.command_get("old-done").unwrap().is_none(), "completed command past retention is deleted");
    assert!(db.command_get("fresh-done").unwrap().is_some(), "completed command inside retention is kept");
    assert!(
        db.command_get("old-open").unwrap().is_some(),
        "a command that has not completed is never pruned"
    );
    assert_eq!(keep, 7 * 86_400, "retention is the documented seven days");
    drop(db);
    let _ = std::fs::remove_dir_all(&dir);
}
