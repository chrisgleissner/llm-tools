//! Caps from the capacity plan (docs/distributed/capacity-plan.md, findings F1 to F10): each test fails when
//! the cap it names is raised or removed.
mod common;
use bridge_coordinator::db::{HelloOutcome, MAX_ENROLLED_WORKERS, MAX_TOTAL_MISSIONS};
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
async fn enrolment_is_capped_at_256_workers_with_a_clear_error() {
    assert_eq!(MAX_ENROLLED_WORKERS, 256);
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    for i in 0..MAX_ENROLLED_WORKERS {
        assert!(env.srv.state.db.worker_add(&format!("e{i}"), &format!("hash{i}")).unwrap());
    }
    let (status, body) = api.post("/api/v1/workers", json!({"name": "one-too-many"})).await;
    assert_eq!(status, 409, "{body}");
    assert_eq!(body["code"], "too-many-workers", "{body}");
    assert_eq!(env.srv.state.db.workers_list().unwrap().len() as u64, MAX_ENROLLED_WORKERS);
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

#[tokio::test(flavor = "multi_thread")]
async fn total_missions_are_capped_and_an_unpaged_list_is_one_page() {
    assert_eq!(MAX_TOTAL_MISSIONS, 10_000);
    let env = boot().await;
    let api = env.admin().await;
    let db = &env.srv.state.db;
    for i in 0..10 {
        let name = format!("f{i}");
        api.add_worker(&name).await;
        let hello = hello_payload(&name, 1000);
        let outcome = db
            .worker_hello(&name, &format!("wid-{name}"), hello.as_object().unwrap(), &caps(), "127.0.0.1")
            .unwrap();
        assert!(matches!(outcome, HelloOutcome::Ok { .. }));
    }
    let token = api.add_worker("late").await;
    let ws = Fw::raw(&env, &token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: "wid-late".into(),
        generation: 0,
        seq: 0,
    };
    fw.send("hello", hello_payload("late", 1)).await;
    let reply = fw.recv().await;
    assert_eq!(reply["payload"]["code"], "too-many-missions", "{reply}");
    assert_eq!(fw.expect_close().await, Some(1008));
    assert_eq!(db.diagnostics().unwrap()["missions_indexed"], 10_000);
    let (status, body) = api.get("/api/v1/missions").await;
    assert_eq!(status, 200);
    assert_eq!(
        body["missions"].as_array().unwrap().len(),
        100,
        "an unpaged list returns the default page, not every row"
    );
    assert_eq!(body["total"], 10_000);
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

#[tokio::test(flavor = "multi_thread")]
async fn stream_sockets_are_capped_at_128_with_a_clear_error() {
    use bridge_coordinator::hub::MAX_STREAM_CLIENTS;
    assert_eq!(MAX_STREAM_CLIENTS, 128);
    let env = boot_with(relaxed).await;
    let api = env.admin().await;
    let origin = env.origin();
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let mut open = Vec::new();
    for _ in 0..MAX_STREAM_CLIENTS {
        open.push(env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.unwrap());
    }
    match env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await {
        Err(tokio_tungstenite::tungstenite::Error::Http(response)) => {
            assert_eq!(response.status().as_u16(), 503);
            let body = String::from_utf8_lossy(response.body().as_deref().unwrap_or_default()).to_string();
            assert!(body.contains("\"code\":\"busy\""), "{body}");
            assert!(response.headers().contains_key("retry-after"));
        }
        other => panic!("the 129th stream must be refused with 503: {:?}", other.map(|_| ())),
    }
    open.pop();
    let mut admitted = false;
    for _ in 0..50 {
        if env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.is_ok() {
            admitted = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(admitted, "a slot is free once one stream closes");
    env.stop().await;
}
