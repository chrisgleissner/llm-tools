mod common;
use common::*;
use futures_util::{SinkExt, StreamExt};
use reqwest::Method;
use rustls::pki_types::pem::PemObject;
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
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
async fn worker_client_certificate_is_issued_once_and_token_rotation_is_staged() {
    let env = boot_with(|t| t.replace("worker_mtls = false", "worker_mtls = true")).await;
    let api = env.admin().await;
    let (status, enrollment) = api.post("/api/v1/workers", json!({"name":"mtls-1"})).await;
    assert_eq!(status, 201, "{enrollment}");
    assert_eq!(enrollment["mutual_tls"], true);
    assert!(enrollment["client_certificate"].as_str().unwrap().contains("BEGIN CERTIFICATE"));
    assert!(enrollment["client_private_key"].as_str().unwrap().contains("PRIVATE KEY"));
    let (_, list) = api.get("/api/v1/workers").await;
    assert_eq!(list["workers"][0]["mutual_tls"], true);
    assert!(!list.to_string().contains("PRIVATE KEY"));
    assert!(!list.to_string().contains(enrollment["token"].as_str().unwrap()));

    let old = enrollment["token"].as_str().unwrap();
    let new = "new-staged-token-which-is-long-enough-0123456789";
    let hash = bridge_coordinator::auth::sha256_hex(new);
    let db = &env.srv.state.db;
    let cert = enrollment["client_certificate"].as_str().unwrap();
    let der = rustls::pki_types::CertificateDer::pem_slice_iter(cert.as_bytes()).next().unwrap().unwrap();
    use sha2::Digest;
    let fingerprint = hex::encode(sha2::Sha256::digest(der.as_ref()));
    assert!(db.worker_client_certificate_matches("mtls-1", &fingerprint).unwrap());
    assert!(!db.worker_client_certificate_matches("mtls-1", "00-wrong-certificate").unwrap());
    assert!(!db.worker_client_certificate_matches("different-worker", &fingerprint).unwrap());
    assert!(db.worker_stage_token_rotation("mtls-1", &hash, bridge_coordinator::now() + 60).unwrap());
    assert!(db
        .worker_by_connection_token(&bridge_coordinator::auth::sha256_hex(old), bridge_coordinator::now())
        .unwrap()
        .is_some());
    assert!(db.worker_by_connection_token(&hash, bridge_coordinator::now()).unwrap().unwrap().1);
    assert!(db.worker_by_connection_token(&hash, bridge_coordinator::now() + 61).unwrap().is_none());
    assert!(!db.worker_promote_token_rotation("different-worker", &hash, bridge_coordinator::now()).unwrap());
    let superseding = bridge_coordinator::auth::sha256_hex("superseding-token-which-is-long-enough-012345");
    assert!(db.worker_stage_token_rotation("mtls-1", &superseding, bridge_coordinator::now() + 60).unwrap());
    assert!(db.worker_by_connection_token(&hash, bridge_coordinator::now()).unwrap().is_none());
    assert!(db.worker_by_connection_token(&superseding, bridge_coordinator::now()).unwrap().unwrap().1);
    assert!(!db.worker_promote_token_rotation("mtls-1", &hash, bridge_coordinator::now()).unwrap());
    assert!(db.worker_promote_token_rotation("mtls-1", &superseding, bridge_coordinator::now()).unwrap());
    assert!(db
        .worker_by_connection_token(&bridge_coordinator::auth::sha256_hex(old), bridge_coordinator::now())
        .unwrap()
        .is_none());
    assert!(!db.worker_by_connection_token(&superseding, bridge_coordinator::now()).unwrap().unwrap().1);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_only_listener_requires_a_matching_ca_issued_client_certificate() {
    let env = boot_with(|t| {
        t.replace("worker_mtls = false", "worker_mtls = true")
            .replace("# [listener_groups]", "listener_groups = { worker_channel = \"127.0.0.1:0\" }")
    })
    .await;
    let addr = env
        .srv
        .listeners
        .iter()
        .find(|(group, _)| *group == bridge_coordinator::config::ListenerGroup::WorkerChannel)
        .unwrap()
        .1;
    let good = bridge_coordinator::certs::issue_worker_client_identity(&env.dir, "w1").unwrap();
    let wrong = bridge_coordinator::certs::issue_worker_client_identity(&env.dir, "w2").unwrap();
    let token = "worker-token-long-enough-for-test-0123456789";
    env.srv
        .state
        .db
        .worker_add_with_client_certificate("w1", &bridge_coordinator::auth::sha256_hex(token), &good.fingerprint_sha256)
        .unwrap();

    async fn connect(
        env: &Env,
        addr: std::net::SocketAddr,
        identity: Option<&bridge_coordinator::certs::WorkerClientIdentity>,
        token: &str,
    ) -> Result<tokio_tungstenite::WebSocketStream<tokio_rustls::client::TlsStream<tokio::net::TcpStream>>, tokio_tungstenite::tungstenite::Error> {
        use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};
        let mut roots = rustls::RootCertStore::empty();
        for cert in CertificateDer::pem_slice_iter(&std::fs::read(env.dir.join("ca.pem")).unwrap()) {
            roots.add(cert.unwrap()).unwrap();
        }
        let builder = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_root_certificates(roots);
        let tls_cfg = if let Some(identity) = identity {
            let certs = CertificateDer::pem_slice_iter(identity.cert_pem.as_bytes()).map(Result::unwrap).collect();
            let key = PrivateKeyDer::from_pem_slice(identity.key_pem.as_bytes()).unwrap();
            builder.with_client_auth_cert(certs, key).unwrap()
        } else {
            builder.with_no_client_auth()
        };
        let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
        let tls = tokio_rustls::TlsConnector::from(std::sync::Arc::new(tls_cfg))
            .connect(ServerName::try_from("127.0.0.1").unwrap(), tcp)
            .await?;
        let request = format!("wss://127.0.0.1:{}/api/v1/worker/connect", addr.port()).into_client_request().unwrap();
        let mut request = request;
        request.headers_mut().insert("authorization", format!("Bearer {token}").parse().unwrap());
        Ok(tokio_tungstenite::client_async(request, tls).await?.0)
    }

    assert!(
        connect(&env, addr, None, token).await.is_err(),
        "no client certificate must fail the TLS handshake"
    );
    assert!(
        connect(&env, addr, Some(&wrong), token).await.is_err(),
        "a different worker certificate must fail route authentication"
    );
    let socket = connect(&env, addr, Some(&good), token).await.expect("matching certificate and token accepted");
    drop(socket);
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
async fn heartbeat_events_do_not_make_a_caught_up_worker_stale() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let stale = |list: &Value| -> Vec<bool> { list["missions"].as_array().unwrap().iter().map(|m| m["stale"].as_bool().unwrap()).collect() };

    // A worker that predates mission_cursor sends only the events. Heartbeats and capacity
    // refreshes cannot change a mission summary, so they leave the stored copy current.
    fw.send(
        "events",
        json!({"after": 0, "through": 2, "events": [
            {"id": 1, "mission_id": "m1", "kind": "controller-heartbeat", "data": {}},
            {"id": 2, "mission_id": "m1", "kind": "capacity-refresh", "data": {}}
        ]}),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    assert_eq!(stale(&list), vec![false], "{list}");

    // A mixed batch includes an event that can change a mission, so the copy is stale again.
    fw.send(
        "events",
        json!({"after": 2, "through": 4, "events": [
            {"id": 3, "mission_id": "m1", "kind": "controller-heartbeat", "data": {}},
            {"id": 4, "mission_id": "m1", "kind": "task-status", "data": {}}
        ]}),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    assert_eq!(stale(&list), vec![true], "{list}");

    // Heartbeats after that do not hide the missing refresh, and a batch that carries its
    // own mission_cursor makes the copy current again.
    fw.send(
        "events",
        json!({"after": 4, "through": 5, "events": [{"id": 5, "mission_id": "m1", "kind": "controller-heartbeat", "data": {}}]}),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    assert_eq!(stale(&list), vec![true], "{list}");
    fw.send(
        "events",
        json!({"after": 5, "through": 6, "mission_cursor": 6, "events": [{"id": 6, "kind": "task-status"}], "missions": []}),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    assert_eq!(stale(&list), vec![false], "{list}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn mission_list_serves_attention_and_task_counts_when_the_worker_reports_them() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;

    // The hello of an older worker (see Fw::connect) has neither field: the list omits them.
    let (_, list) = api.get("/api/v1/missions").await;
    let legacy = &list["missions"][0]["mission"];
    assert!(legacy.get("attention").is_none() && legacy.get("task_counts").is_none(), "{list}");

    let attention = json!({"kind": "approval", "message": "task 2 awaits your review", "tasks": [2]});
    fw.send(
        "events",
        json!({"after": 0, "through": 1, "mission_cursor": 1,
        "events": [{"id": 1, "mission_id": "m1", "kind": "task-status", "data": {}}],
        "missions": [
            {"id": "m1", "name": "mission", "status": "RUNNING", "folder": "proj", "updated_at": null,
             "task_counts": {"VERIFYING": 1, "QUEUED": 2}, "attention": attention},
            {"id": "m2", "name": "quiet", "status": "RUNNING", "folder": "proj", "updated_at": null,
             "task_counts": {"RUNNING": 1}, "attention": null}
        ]}),
    )
    .await;
    fw.barrier().await;
    let (_, list) = api.get("/api/v1/missions").await;
    let by_id = |id: &str| list["missions"].as_array().unwrap().iter().find(|m| m["mission"]["id"] == id).unwrap().clone();
    assert_eq!(by_id("m1")["mission"]["attention"], attention);
    assert_eq!(by_id("m1")["mission"]["task_counts"], json!({"VERIFYING": 1, "QUEUED": 2}));
    assert!(by_id("m2")["mission"]["attention"].is_null());
    assert_eq!(by_id("m1")["stale"], false);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn command_round_trip() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect_settled(&env, &token, "wid1", 0).await;

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
    // The coordinator notices the close asynchronously. A command submitted before it has unregistered the
    // connection is queued on the dying connection and journaled as `sent`, not `pending`.
    for _ in 0..400 {
        if !env.srv.state.hub.online("w1") {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    assert!(!env.srv.state.hub.online("w1"), "the coordinator never noticed the closed connection");
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

/// The worker keeps one events batch in flight and treats the `pong` of a `ping` sent right after the
/// batch as proof that the coordinator processed it. That holds only while a connection's messages are
/// handled and answered in order, and while an accepted batch draws no reply at all.
#[tokio::test(flavor = "multi_thread")]
async fn a_pong_after_an_events_batch_confirms_the_batch_and_follows_its_nack() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let ev = |ids: &[u64]| ids.iter().map(|i| json!({"id": i, "kind": "test", "mission_id": "m1"})).collect::<Vec<_>>();

    // accepted: the next message from the coordinator is the pong
    fw.send("events", json!({"after": 0, "through": 3, "events": ev(&[1, 2, 3])})).await;
    fw.send("ping", json!({})).await;
    assert_eq!(fw.recv().await["type"], "pong");

    // a batch that starts elsewhere is refused before the pong of the ping behind it, and only once
    fw.send("events", json!({"after": 1, "through": 5, "events": ev(&[4, 5])})).await;
    fw.send("ping", json!({})).await;
    let nack = fw.recv().await;
    assert_eq!(
        (nack["type"].as_str(), nack["payload"]["expected_after"].as_u64()),
        (Some("events.nack"), Some(3))
    );
    assert_eq!(fw.recv().await["type"], "pong");

    // an empty batch that only moves the cursor past skipped ids is accepted
    fw.send("events", json!({"after": 3, "through": 40, "events": []})).await;
    fw.send("ping", json!({})).await;
    assert_eq!(fw.recv().await["type"], "pong");
    fw.send("events", json!({"after": 40, "through": 41, "events": ev(&[41])})).await;
    fw.send("ping", json!({})).await;
    assert_eq!(fw.recv().await["type"], "pong");
    env.stop().await;
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
    let (mut fw, _) = Fw::connect_settled(&env, &token, "wid1", 0).await;
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
        .post("/api/v1/commands", json!({"target": "w1", "request": request("u1", "mission.teleport")}))
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
    fw.send(
        "events",
        json!({"after": 0, "through": 1, "events": [{"id": 1, "mission_id": "m1", "task_id": 2, "kind": "task-paused"}]}),
    )
    .await;
    let m = next(&mut stream).await;
    assert_eq!(m["type"], "event");
    assert_eq!((m["cursor"].as_u64(), m["count"].as_u64()), (Some(1), Some(1)));
    assert_eq!(m["missions"][0]["id"], "m1");
    assert_eq!(m["task_missions"], json!(["m1"]));
    assert!(m.get("events").is_none(), "browser notifications must not retain or fan out event bodies");
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
async fn oversized_command_request_is_refused_with_documented_problem() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (_worker, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut command = request("oversized", "mission.list");
    // Keep each JSON string within the global 64 KiB limit while making the
    // complete command request exceed its independent 64 KiB budget.
    command["params"] = json!({"first": "x".repeat(40 * 1024), "second": "y".repeat(40 * 1024)});
    let (status, problem) = api.post("/api/v1/commands", json!({"target": "w1", "request": command})).await;
    assert_eq!(status, 413, "{problem}");
    assert_eq!(problem["code"], "request-too-large");
    assert_eq!(problem["type"], "https://llm-tools.dev/problems/request-too-large");
    let (_, spec) = env.api().get("/api/v1/openapi.json").await;
    assert!(spec["paths"]["/api/v1/commands"]["post"]["responses"]["413"].is_object());
    env.stop().await;
}

#[test]
fn command_journal_refuses_excess_pending_bytes() {
    use bridge_coordinator::db::{Db, InsertOutcome, MAX_OPEN_COMMAND_BYTES_PER_WORKER};
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let db = Db::open(&bridge_coordinator::db_path(&dir)).unwrap();
    assert!(db.worker_add("w1", "hash").unwrap());
    let padding = "x".repeat(60 * 1024);
    let mut accepted = 0;
    for i in 0..20 {
        let mut row = request(&format!("queued-{i}"), "mission.list");
        row["params"] = json!({"padding": padding});
        if matches!(db.command_insert("w1", "mission.list", false, &row).unwrap(), InsertOutcome::New(_)) {
            accepted += row.to_string().len();
        } else {
            break;
        }
    }
    assert!(accepted <= MAX_OPEN_COMMAND_BYTES_PER_WORKER as usize);
    assert!(accepted >= 400 * 1024, "the test should exercise the byte budget, accepted {accepted} bytes");
    let mut too_large = request("single-oversized", "mission.list");
    too_large["params"] = json!({"padding": "x".repeat(65 * 1024)});
    assert!(matches!(
        db.command_insert("w1", "mission.list", false, &too_large).unwrap(),
        InsertOutcome::RequestTooLarge
    ));

    assert!(db.worker_add("w2", "hash-2").unwrap());
    let multibyte = "🛑".repeat(10 * 1024);
    let mut accepted = 0;
    for i in 0..20 {
        let mut row = request(&format!("unicode-{i}"), "mission.list");
        row["params"] = json!({"padding": multibyte});
        if matches!(db.command_insert("w2", "mission.list", false, &row).unwrap(), InsertOutcome::New(_)) {
            accepted += row.to_string().len();
        } else {
            break;
        }
    }
    assert!(
        accepted <= MAX_OPEN_COMMAND_BYTES_PER_WORKER as usize,
        "UTF-8 request bytes were undercounted: {accepted}"
    );
    assert!(accepted >= 400 * 1024);
}

#[test]
fn event_batches_have_an_independent_byte_limit() {
    use bridge_coordinator::protocol::{self, MAX_EVENT_BATCH_BYTES};
    let raw = json!({
        "v": 1, "type": "events", "id": "e1", "worker_id": "wid", "generation": 1,
        "payload": {"after": 0, "through": 1, "events": [{"id": 1, "body": "x".repeat(MAX_EVENT_BATCH_BYTES)}]}
    })
    .to_string();
    let error = protocol::decode(raw.as_bytes()).expect_err("oversized batch is refused before storage");
    assert_eq!(error.code, "message-too-large");
}

#[test]
fn hub_fanout_keeps_only_small_change_notifications_for_multiple_browsers() {
    use bridge_coordinator::hub::{Hub, MAX_STREAM_MESSAGE_BYTES};
    let hub = Hub::new();
    let mut browser_one = hub.stream.subscribe();
    let mut browser_two = hub.stream.subscribe();
    hub.publish(json!({"type": "event", "worker": "w1", "cursor": 7, "events": [{"data": "x".repeat(200_000)}]}));
    for browser in [&mut browser_one, &mut browser_two] {
        let message = browser.try_recv().unwrap();
        assert!(message.len() <= MAX_STREAM_MESSAGE_BYTES);
        let value: Value = serde_json::from_str(&message).unwrap();
        assert!(value.get("events").is_none());
        assert_eq!(value["cursor"], 7);
    }
    hub.publish(json!({"type": "command", "command": {"request": {"params": "x".repeat(200_000)}, "result": {"result": "x".repeat(200_000)}}}));
    let message = browser_one.try_recv().unwrap();
    assert!(message.len() <= MAX_STREAM_MESSAGE_BYTES);
    let value: Value = serde_json::from_str(&message).unwrap();
    assert!(value["command"].get("request").is_none());
    assert!(value["command"].get("result").is_none());
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

/// Wait until the worker is listed offline.
async fn wait_offline(api: &Api) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while api.get("/api/v1/workers/w1").await.1["state"] != "offline" {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("worker goes offline");
}

#[tokio::test(flavor = "multi_thread")]
async fn reads_for_an_offline_worker_are_refused_and_writes_stay_queued() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (fw, _) = Fw::connect(&env, &token, "wid1", 0).await; // the hello advertises the operations
    drop(fw);
    wait_offline(&api).await;

    // A read is refused at once, through /commands and through the resource routes, and leaves no journal entry.
    for (id, op) in [("read-1", "mission.list"), ("read-2", "mission.status")] {
        let (st, body) = api.post("/api/v1/commands", json!({"target": "w1", "request": request(id, op)})).await;
        assert_eq!((st, body["error"]["code"].as_str()), (503, Some("worker-offline")), "{body}");
        assert_eq!(api.get(&format!("/api/v1/commands/{id}")).await.0, 404, "a refused read must not be journaled");
    }
    let (st, body) = api.get("/api/v1/missions/m1?worker=w1").await;
    assert_eq!((st, body["error"]["code"].as_str()), (503, Some("worker-offline")), "{body}");
    let (st, body) = api.get("/api/v1/missions/m1/tasks?worker=w1").await;
    assert_eq!((st, body["error"]["code"].as_str()), (503, Some("worker-offline")), "{body}");

    // A write is queued as before and is delivered once after the reconnect, with no read before it.
    assert_eq!(api.post("/api/v1/commands", submit("write-1")).await.0, 202);
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let delivered = fw.recv().await;
    assert_eq!(delivered["payload"]["request_id"], "write-1");
    fw.barrier().await; // anything else queued would have arrived before the pong
    assert_eq!(api.get("/api/v1/commands/read-1").await.0, 404);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn unanswered_reads_fail_when_the_worker_disconnects_and_are_not_replayed() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let (st, _) = api
        .post("/api/v1/commands", json!({"target": "w1", "request": request("read-open", "mission.list")}))
        .await;
    assert_eq!(st, 202);
    assert_eq!(fw.recv().await["payload"]["request_id"], "read-open");
    assert_eq!(api.post("/api/v1/commands", submit("write-open")).await.0, 202);
    assert_eq!(fw.recv().await["payload"]["request_id"], "write-open");
    drop(fw);
    wait_offline(&api).await;

    // The read is finished with the failure a client can read; the write stays open.
    let (_, read) = api.get("/api/v1/commands/read-open").await;
    assert_eq!(read["state"], "done", "{read}");
    assert_eq!(
        (read["result"]["outcome"].as_str(), read["result"]["error"]["code"].as_str()),
        (Some("failed"), Some("worker-offline"))
    );
    assert_eq!(api.get("/api/v1/commands/write-open").await.1["state"], "sent");

    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(fw.recv().await["payload"]["request_id"], "write-open");
    fw.barrier().await;
    assert_eq!(api.get("/api/v1/diagnostics").await.1["command_queue_depths"]["w1"], json!({"sent": 1}));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn browser_stream_gets_change_notices_not_event_bodies_or_command_results() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let origin = env.origin();
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let mut stream = env.ws("/api/v1/stream", &[("cookie", &cookie), ("origin", &origin)]).await.unwrap();
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    assert_eq!(next(&mut stream).await["state"], "online");

    let events: Vec<Value> = (1..=25)
        .map(|id| json!({"id": id, "mission_id": if id % 5 == 0 { "m2" } else { "m1" }, "kind": "task-note", "data": {"text": "x".repeat(600)}}))
        .collect();
    fw.send(
        "events",
        json!({"after": 0, "through": 25, "events": events, "missions": [{"id": "m3", "name": "n", "status": "RUNNING", "folder": "f", "updated_at": null}]}),
    )
    .await;
    let notice = next(&mut stream).await;
    let text = notice.to_string();
    assert!(text.len() < 600, "a 25-event batch of 15 KB produced a {} byte notice", text.len());
    assert!(notice.get("events").is_none() && !text.contains("xxxx"), "{notice}");
    assert_eq!((notice["cursor"].as_u64(), notice["count"].as_u64()), (Some(25), Some(25)));
    assert_eq!((notice["first_event_id"].as_u64(), notice["last_event_id"].as_u64()), (Some(1), Some(25)));
    let missions = notice["missions"].as_array().unwrap();
    let by_id = |id: &str| missions.iter().find(|m| m["id"] == id).unwrap_or_else(|| panic!("no mission {id} in {notice}"));
    assert_eq!((by_id("m1")["events"].as_u64(), by_id("m1")["last_event_id"].as_u64()), (Some(20), Some(24)));
    assert_eq!((by_id("m2")["events"].as_u64(), by_id("m2")["summary"].as_bool()), (Some(5), Some(false)));
    assert_eq!((by_id("m3")["events"].as_u64(), by_id("m3")["summary"].as_bool()), (Some(0), Some(true)));

    // A summary-only update (a newly pending provider question) is announced as well.
    fw.send(
        "events",
        json!({"after": 25, "through": 25, "events": [], "missions": [{"id": "m3", "name": "n", "status": "RUNNING", "folder": "f", "updated_at": null}]}),
    )
    .await;
    let notice = next(&mut stream).await;
    assert_eq!(
        (notice["type"].as_str(), notice["count"].as_u64(), notice["missions"][0]["summary"].as_bool()),
        (Some("event"), Some(0), Some(true))
    );

    // A command notice names the command and its state but carries neither the request parameters nor the result.
    assert_eq!(api.post("/api/v1/commands", submit("cmd-1")).await.0, 202);
    assert_eq!(fw.recv().await["payload"]["request_id"], "cmd-1");
    let big = json!({"schema_version": 1, "request_id": "cmd-1", "operation": "task.add", "outcome": "applied", "result": {"blob": "y".repeat(50_000)}, "error": null});
    fw.send("command.result", big).await;
    let mut done = None;
    while done.is_none() {
        let m = next(&mut stream).await;
        if m["type"] == "command" && m["command"]["state"] == "done" {
            done = Some(m);
        }
    }
    let done = done.unwrap();
    assert!(done.to_string().len() < 600, "{done}");
    assert_eq!(
        (done["command"]["outcome"].as_str(), done["command"]["request_id"].as_str()),
        (Some("applied"), Some("cmd-1"))
    );
    assert!(done["command"].get("result").is_none() && done["command"].get("request").is_none());
    // The full entry stays available from the journal.
    assert!(api.get("/api/v1/commands/cmd-1").await.1["result"]["result"]["blob"].as_str().unwrap().len() == 50_000);

    // The diagnostics count messages and bytes by direction and type.
    let (_, diagnostics) = api.get("/api/v1/diagnostics").await;
    let link = &diagnostics["link_bytes"];
    assert_eq!(link["worker_in"]["events"]["messages"], 2);
    assert!(link["worker_in"]["command.result"]["bytes"].as_u64().unwrap() > 50_000);
    assert!(link["browser_out"]["event"]["messages"].as_u64().unwrap() >= 2);
    assert!(link["browser_out"]["event"]["bytes"].as_u64().unwrap() < 2_000, "{link}");
    assert!(link["worker_out"]["welcome"]["messages"].as_u64().unwrap() >= 1);
    env.stop().await;
}
