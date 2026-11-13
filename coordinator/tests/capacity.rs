//! Caps from the capacity plan (docs/distributed/capacity-plan.md, findings F1 to F10): each test fails when
//! the cap it names is raised or removed.
mod common;
use bridge_coordinator::db::{EnrolOutcome, HelloOutcome, MAX_ENROLLED_WORKERS, MAX_MISSIONS_PER_WORKER};
use bridge_coordinator::hub::MAX_WORKER_CONNECTIONS;
use bridge_coordinator::protocol::{MAX_EVENTS_PER_BATCH, MAX_MISSIONS_PER_HELLO};
use common::*;
use serde_json::{json, Value};
use std::time::Duration;

fn relaxed(text: String) -> String {
    text.replace("max_connections_per_ip = 32", "max_connections_per_ip = 1000")
        .replace("mutation_limit_per_token = 30", "mutation_limit_per_token = 10000")
        .replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 10000")
}

fn hello_payload(name: &str, missions: usize) -> Value {
    let list: Vec<Value> = (0..missions)
        .map(|i| json!({"id": format!("m{i}"), "name": "mission", "status": "running", "folder": "proj", "updated_at": null}))
        .collect();
    json!({"worker_id": format!("wid-{name}"), "name": name, "software_version": "test", "protocol_versions": [1],
           "platform": {"system": "Linux"}, "capabilities": caps(), "missions": list, "event_cursor": 0})
}

async fn connect_named(env: &Env, token: &str, name: &str) -> Fw {
    let ws = Fw::raw(env, token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: format!("wid-{name}"),
        generation: 0,
        seq: 0,
    };
    fw.send("hello", hello_payload(name, 1)).await;
    let welcome = fw.recv().await;
    assert_eq!(welcome["type"], "welcome", "{welcome}");
    fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
    fw
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_connection_cap_admits_64_and_refuses_65th() {
    assert_eq!(MAX_WORKER_CONNECTIONS, 64);
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    let mut workers = Vec::new();
    for i in 0..=MAX_WORKER_CONNECTIONS {
        let name = format!("w{i}");
        let token = api.add_worker(&name).await;
        workers.push((name, token));
    }
    let mut live = Vec::new();
    for (name, token) in &workers[..MAX_WORKER_CONNECTIONS] {
        live.push(connect_named(&env, token, name).await);
    }
    let (_, token) = &workers[MAX_WORKER_CONNECTIONS];
    match Fw::raw(&env, token).await {
        Err(tokio_tungstenite::tungstenite::Error::Http(response)) => {
            assert_eq!(response.status().as_u16(), 503);
            let body = String::from_utf8_lossy(response.body().as_deref().unwrap_or_default()).to_string();
            assert!(body.contains("too many worker connections"), "{body}");
        }
        other => panic!("the 65th connection must be refused with 503: {:?}", other.map(|_| ())),
    }
    drop(live.pop());
    let mut admitted = None;
    for _ in 0..50 {
        match Fw::raw(&env, token).await {
            Ok(ws) => {
                admitted = Some(ws);
                break;
            }
            Err(_) => tokio::time::sleep(Duration::from_millis(100)).await,
        }
    }
    assert!(admitted.is_some(), "a slot is free once one worker disconnects");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn hello_accepts_5000_missions_and_refuses_5001() {
    assert_eq!(MAX_MISSIONS_PER_HELLO, 5000);
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    for (count, welcomed) in [(5001usize, false), (5000, true)] {
        let ws = Fw::raw(&env, &token).await.unwrap();
        let mut fw = Fw {
            ws,
            worker_id: "wid-w1".into(),
            generation: 0,
            seq: 0,
        };
        fw.send("hello", hello_payload("w1", count)).await;
        let reply = fw.recv().await;
        if welcomed {
            assert_eq!(reply["type"], "welcome", "{reply}");
        } else {
            assert_eq!(reply["payload"]["code"], "invalid-message", "{reply}");
            assert!(reply["payload"]["message"].as_str().unwrap().contains("too many missions"), "{reply}");
        }
    }
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn event_batch_accepts_500_events_and_refuses_501() {
    assert_eq!(MAX_EVENTS_PER_BATCH, 500);
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let events = |count: u64| -> Vec<Value> { (1..=count).map(|id| json!({"id": id, "mission_id": "m1", "kind": "task-note"})).collect() };
    fw.send("events", json!({"after": 0, "through": 501, "events": events(501)})).await;
    assert_eq!(fw.recv().await["payload"]["code"], "invalid-message");
    fw.send("events", json!({"after": 0, "through": 500, "events": events(500)})).await;
    fw.barrier().await;
    let (_, body) = api.get("/api/v1/events?worker=w1&after=0&limit=1000").await;
    assert_eq!(body["events"].as_array().unwrap().len(), 500, "{body}");
    env.stop().await;
}

fn summary(id: &str, status: &str, updated_at: &str) -> Value {
    json!({"id": id, "name": "mission", "status": status, "folder": "proj", "updated_at": updated_at})
}

fn hello_with(name: &str, missions: Vec<Value>) -> serde_json::Map<String, Value> {
    let mut hello = hello_payload(name, 0);
    hello["missions"] = Value::Array(missions);
    hello.as_object().unwrap().clone()
}

fn indexed(env: &Env, worker: &str) -> Vec<String> {
    let mut ids: Vec<String> = env
        .srv
        .state
        .db
        .missions_list()
        .unwrap()
        .into_iter()
        .filter(|(w, _)| w == worker)
        .map(|(_, info)| info["id"].as_str().unwrap().to_string())
        .collect();
    ids.sort();
    ids
}

#[tokio::test(flavor = "multi_thread")]
async fn two_workers_with_6000_missions_each_both_connect() {
    assert_eq!(MAX_MISSIONS_PER_WORKER, 10_000);
    let env = boot().await;
    let api = env.admin().await;
    let db = &env.srv.state.db;
    for name in ["a", "b"] {
        api.add_worker(name).await;
        let list = (0..6000).map(|i| summary(&format!("m{i}"), "RUNNING", "2026-01-01")).collect();
        let outcome = db
            .worker_hello(name, &format!("wid-{name}"), &hello_with(name, list), &caps(), "127.0.0.1")
            .unwrap();
        assert!(matches!(outcome, HelloOutcome::Ok { .. }));
    }
    let diag = db.diagnostics().unwrap();
    assert_eq!(diag["missions_indexed"], 12_000);
    assert_eq!(diag["warnings"], json!([]));
    let (status, body) = api.get("/api/v1/missions").await;
    assert_eq!(status, 200);
    assert_eq!(
        body["missions"].as_array().unwrap().len(),
        100,
        "an unpaged list returns the default page, not every row"
    );
    assert_eq!(body["total"], 12_000);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_hello_over_the_cap_drops_the_oldest_finished_summaries_and_is_never_refused() {
    let env = boot().await;
    let api = env.admin().await;
    let db = &env.srv.state.db;
    api.add_worker("a").await;
    // 10 001 summaries: ids sort like their update times; the oldest finished one is m00000.
    let list = (0..=10_000)
        .map(|i| summary(&format!("m{i:05}"), "COMPLETED", &format!("2026-01-01T{i:08}")))
        .collect();
    let outcome = db.worker_hello("a", "wid-a", &hello_with("a", list), &caps(), "127.0.0.1").unwrap();
    assert!(matches!(outcome, HelloOutcome::Ok { .. }));
    let ids = indexed(&env, "a");
    assert_eq!(ids.len(), 10_000);
    assert!(!ids.contains(&"m00000".to_string()) && ids.contains(&"m00001".to_string()) && ids.contains(&"m10000".to_string()));
    assert_eq!(db.diagnostics().unwrap()["warnings"], json!([]));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_hello_of_active_missions_over_the_cap_refuses_only_the_excess_and_warns() {
    let env = boot().await;
    let api = env.admin().await;
    let db = &env.srv.state.db;
    api.add_worker("a").await;
    let list = (0..=10_000).map(|i| summary(&format!("m{i:05}"), "RUNNING", "2026-01-01")).collect();
    let outcome = db.worker_hello("a", "wid-a", &hello_with("a", list), &caps(), "127.0.0.1").unwrap();
    assert!(matches!(outcome, HelloOutcome::Ok { .. }), "the hello is never rejected");
    let ids = indexed(&env, "a");
    assert_eq!(ids.len(), 10_000);
    assert!(!ids.contains(&"m10000".to_string()), "the last (newest) row is the one refused");
    let warnings = db.diagnostics().unwrap()["warnings"].clone();
    assert_eq!(warnings[0]["code"], "missions-over-cap", "{warnings}");
    assert_eq!(warnings[0]["worker"], "a");
    assert!(warnings[0]["fix"].as_str().unwrap().contains("mission delete"));
    // A later hello that fits clears the warning.
    let outcome = db
        .worker_hello(
            "a",
            "wid-a",
            &hello_with("a", vec![summary("m1", "RUNNING", "2026-01-01")]),
            &caps(),
            "127.0.0.1",
        )
        .unwrap();
    assert!(matches!(outcome, HelloOutcome::Ok { .. }));
    assert_eq!(db.diagnostics().unwrap()["warnings"], json!([]));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn the_events_upsert_path_enforces_the_cap_too() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    let old: Vec<Value> = (0..5000)
        .map(|i| summary(&format!("a{i:05}"), "COMPLETED", &format!("2026-01-01T{i:08}")))
        .collect();
    let ws = Fw::raw(&env, &token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: "wid-a".into(),
        generation: 0,
        seq: 0,
    };
    fw.send("hello", Value::Object(hello_with("a", old))).await;
    let welcome = fw.recv().await;
    assert_eq!(welcome["type"], "welcome", "{welcome}");
    fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
    let more: Vec<Value> = (0..5000)
        .map(|i| summary(&format!("b{i:05}"), "COMPLETED", &format!("2026-02-01T{i:08}")))
        .collect();
    fw.send("events", json!({"after": 0, "through": 0, "events": [], "missions": more})).await;
    fw.barrier().await;
    assert_eq!(indexed(&env, "a").len(), 10_000);
    let newer: Vec<Value> = (0..3).map(|i| summary(&format!("c{i}"), "COMPLETED", "2026-03-01")).collect();
    fw.send("events", json!({"after": 0, "through": 0, "events": [], "missions": newer})).await;
    fw.barrier().await;
    let ids = indexed(&env, "a");
    assert_eq!(ids.len(), 10_000, "the cap holds after an upsert");
    assert!(ids.contains(&"c0".to_string()) && ids.contains(&"c2".to_string()));
    for gone in ["a00000", "a00001", "a00002"] {
        assert!(!ids.contains(&gone.to_string()), "{gone} was the oldest finished summary");
    }
    assert!(ids.contains(&"a00003".to_string()));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn enrolment_checks_the_name_first_and_never_passes_256_even_when_concurrent() {
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    let db = env.srv.state.db.clone();
    for i in 0..(MAX_ENROLLED_WORKERS - 2) {
        assert!(db.worker_add(&format!("e{i}"), &format!("hash{i}")).unwrap());
    }
    // 254 enrolled: the 255th and 256th are accepted, the 257th is refused.
    let (status, body) = api.post("/api/v1/workers", json!({"name": "last-but-one"})).await;
    assert_eq!(status, 201, "{body}");
    let (status, body) = api.post("/api/v1/workers", json!({"name": "last"})).await;
    assert_eq!(status, 201, "{body}");
    let (status, body) = api.post("/api/v1/workers", json!({"name": "one-too-many"})).await;
    assert_eq!((status, body["code"].as_str()), (409, Some("too-many-workers")), "{body}");
    // An existing name is reported as such even when the table is full.
    let (status, body) = api.post("/api/v1/workers", json!({"name": "last"})).await;
    assert_eq!((status, body["code"].as_str()), (409, Some("worker-exists")), "{body}");
    assert_eq!(db.workers_list().unwrap().len() as u64, MAX_ENROLLED_WORKERS);
    // Concurrent enrolments from 250 workers: exactly six fit.
    let fresh = boot_with(relaxed).await;
    let db = fresh.srv.state.db.clone();
    for i in 0..250 {
        assert!(db.worker_add(&format!("e{i}"), &format!("hash{i}")).unwrap());
    }
    assert_eq!(
        db.worker_add_capped("e0", "unused-hash", None, MAX_ENROLLED_WORKERS).unwrap(),
        EnrolOutcome::Exists
    );
    let handles: Vec<_> = (0..24)
        .map(|i| {
            let db = db.clone();
            std::thread::spawn(move || db.worker_add_capped(&format!("c{i}"), &format!("hc{i}"), None, MAX_ENROLLED_WORKERS).unwrap())
        })
        .collect();
    let added = handles.into_iter().map(|h| h.join().unwrap()).filter(|o| *o == EnrolOutcome::Added).count();
    assert_eq!(added, 6);
    assert_eq!(db.workers_list().unwrap().len() as u64, MAX_ENROLLED_WORKERS);
    fresh.stop().await;
    env.stop().await;
}

async fn expect_close_code(ws: &mut Ws) -> Option<u16> {
    use futures_util::StreamExt;
    use tokio_tungstenite::tungstenite::Message as WsMsg;
    loop {
        match tokio::time::timeout(Duration::from_secs(30), ws.next())
            .await
            .expect("timed out waiting for close")
        {
            Some(Ok(WsMsg::Close(f))) => return f.map(|f| u16::from(f.code)),
            None | Some(Err(_)) => return None,
            Some(Ok(_)) => {}
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_client_that_stops_reading_gets_1013_and_diagnostics_count_it() {
    let env = boot().await;
    let api = env.admin().await;
    let origin = env.origin();
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let mut stalled = env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.unwrap();
    let (_, diag) = api.get("/api/v1/diagnostics").await;
    assert_eq!(diag["resources"]["stream_clients"], 1);
    let filler = "z".repeat(3000);
    let hub = env.srv.state.hub.clone();
    // Nobody reads `stalled`: the kernel buffers fill, the session task blocks in send, and the 32-message
    // broadcast buffer overflows.
    for i in 0..40_000u64 {
        hub.publish(json!({"type": "event", "worker": "w", "cursor": i, "pad": filler}));
        if i % 64 == 0 {
            tokio::task::yield_now().await;
        }
    }
    assert_eq!(expect_close_code(&mut stalled).await, Some(1013));
    let mut gone = false;
    for _ in 0..50 {
        let (_, diag) = api.get("/api/v1/diagnostics").await;
        if diag["resources"]["stream_clients"] == 0 {
            gone = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(gone, "the closed client is no longer counted");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn diagnostics_expose_connection_and_queue_counters() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (_fw, _) = Fw::connect_settled(&env, &token, "wid1", 0).await;
    let (status, diag) = api.get("/api/v1/diagnostics").await;
    assert_eq!(status, 200);
    let resources = &diag["resources"];
    assert_eq!(resources["worker_connections"], 1, "{diag}");
    for key in [
        "outbound_queue_depth_max",
        "outbound_queue_depth_total",
        "stream_clients",
        "terminal_sessions",
        "terminal_input_frames_dropped",
    ] {
        assert!(resources[key].is_u64(), "{key} missing in {diag}");
    }
    assert_eq!((diag["enrolled_workers"].as_u64(), diag["missions_indexed"].as_u64()), (Some(1), Some(1)));
    env.stop().await;
}

async fn bearer_stream(env: &Env, secret: &str) -> Result<Ws, tokio_tungstenite::tungstenite::Error> {
    env.ws("/api/v1/stream", &[("authorization", &format!("Bearer {secret}"))]).await
}

fn refusal_text(result: Result<Ws, tokio_tungstenite::tungstenite::Error>) -> String {
    match result {
        Err(tokio_tungstenite::tungstenite::Error::Http(response)) => {
            assert_eq!(response.status().as_u16(), 503);
            assert!(response.headers().contains_key("retry-after"));
            let body = String::from_utf8_lossy(response.body().as_deref().unwrap_or_default()).to_string();
            assert!(body.contains("\"code\":\"busy\""), "{body}");
            body
        }
        other => panic!("the stream must be refused with 503: {:?}", other.map(|_| ())),
    }
}

async fn new_token(api: &Api, name: &str) -> String {
    let (status, body) = api.post("/api/v1/tokens", json!({"name": name, "scopes": ["read"]})).await;
    assert_eq!(status, 201, "{body}");
    body["secret"].as_str().unwrap().to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_sockets_are_capped_at_128_in_total_with_a_clear_error() {
    use bridge_coordinator::hub::{MAX_STREAM_CLIENTS, MAX_STREAM_CLIENTS_PER_SESSION};
    assert_eq!(MAX_STREAM_CLIENTS, 128);
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    let mut open = Vec::new();
    for t in 0..(MAX_STREAM_CLIENTS / MAX_STREAM_CLIENTS_PER_SESSION) {
        let secret = new_token(&api, &format!("t{t}")).await;
        for _ in 0..MAX_STREAM_CLIENTS_PER_SESSION {
            open.push(bearer_stream(&env, &secret).await.unwrap());
        }
    }
    let last = new_token(&api, "last").await;
    let text = refusal_text(bearer_stream(&env, &last).await);
    assert!(text.contains("128 browser streams"), "{text}");
    open.pop();
    let mut admitted = false;
    for _ in 0..50 {
        if bearer_stream(&env, &last).await.is_ok() {
            admitted = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(admitted, "a slot is free once one stream closes");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn one_session_may_hold_16_streams_and_the_17th_is_refused_without_blocking_others() {
    use bridge_coordinator::hub::MAX_STREAM_CLIENTS_PER_SESSION;
    assert_eq!(MAX_STREAM_CLIENTS_PER_SESSION, 16);
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    let origin = env.origin();
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let mut open = Vec::new();
    for _ in 0..MAX_STREAM_CLIENTS_PER_SESSION {
        open.push(env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.unwrap());
    }
    let text = refusal_text(env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await);
    assert!(text.contains("this session already has 16"), "{text}");
    let other = new_token(&api, "other").await;
    assert!(bearer_stream(&env, &other).await.is_ok(), "another session is not affected");
    open.pop();
    let mut admitted = false;
    for _ in 0..50 {
        if env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.is_ok() {
            admitted = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(admitted);
    env.stop().await;
}
