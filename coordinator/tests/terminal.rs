//! Remote terminal relay: browser endpoint, worker attach rules and bounds (PROTO-009, SEC-006, WEB-008).
mod common;
use base64::Engine;
use common::*;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message as WsMsg;

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

async fn recv_json(ws: &mut Ws) -> Value {
    loop {
        match tokio::time::timeout(Duration::from_secs(10), ws.next()).await.expect("timed out") {
            Some(Ok(WsMsg::Text(t))) => return serde_json::from_str(t.as_str()).unwrap(),
            Some(Ok(WsMsg::Ping(_) | WsMsg::Pong(_))) => {}
            other => panic!("expected a text frame, got {other:?}"),
        }
    }
}

async fn send_json(ws: &mut Ws, v: Value) {
    ws.send(WsMsg::Text(v.to_string().into())).await.unwrap();
}

/// Next control message of `kind`, skipping others.
async fn control(fw: &mut Fw, kind: &str) -> Value {
    loop {
        let m = fw.recv().await;
        if m["type"] == kind {
            return m;
        }
    }
}

fn browser_headers(api: &Api) -> Vec<(String, String)> {
    vec![
        ("cookie".into(), format!("bridge_session={}", api.cookie.as_ref().unwrap())),
        ("origin".into(), api.base.clone()),
    ]
}

async fn browser(env: &Env, api: &Api, query: &str) -> Ws {
    let h = browser_headers(api);
    let refs: Vec<(&str, &str)> = h.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
    env.ws(&format!("/api/v1/terminal?{query}"), &refs).await.expect("browser upgrade")
}

async fn attach(env: &Env, token: &str, session: &str) -> Result<Ws, tokio_tungstenite::tungstenite::Error> {
    env.ws(&format!("/api/v1/worker/terminal/{session}"), &[("authorization", &format!("Bearer {token}"))])
        .await
}

struct Session {
    browser: Ws,
    worker: Ws,
    id: String,
}

async fn open_session(env: &Env, api: &Api, fw: &mut Fw, token: &str, mode: &str) -> Session {
    let browser = browser(env, api, &format!("worker=w1&mission=m1&mode={mode}&cols=100&rows=30")).await;
    let open = control(fw, "terminal.open").await;
    let p = &open["payload"];
    assert_eq!(
        (p["mission_id"].as_str(), p["mode"].as_str(), p["cols"].as_u64(), p["rows"].as_u64()),
        (Some("m1"), Some(mode), Some(100), Some(30))
    );
    let id = p["session_id"].as_str().unwrap().to_string();
    assert!(id.len() >= 16 && id.bytes().all(|b| b.is_ascii_alphanumeric()));
    let worker = attach(env, token, &id).await.expect("worker attaches");
    Session { browser, worker, id }
}

#[tokio::test(flavor = "multi_thread")]
async fn relay_both_directions_with_validation() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut s = open_session(&env, &api, &mut fw, &token, "write").await;

    // worker -> browser: s and o are relayed, unknown frames are dropped
    send_json(&mut s.worker, json!({"t": "s", "cols": 100, "rows": 30, "mode": "write", "reason": ""})).await;
    send_json(&mut s.worker, json!({"t": "zzz"})).await;
    s.worker.send(WsMsg::Text("not json".into())).await.unwrap();
    send_json(&mut s.worker, json!({"t": "o", "d": b64(b"hello")})).await;
    assert_eq!(
        recv_json(&mut s.browser).await,
        json!({"t": "s", "cols": 100, "rows": 30, "mode": "write", "reason": ""})
    );
    assert_eq!(recv_json(&mut s.browser).await, json!({"t": "o", "d": b64(b"hello")}));

    // browser -> worker: invalid frames are dropped, valid ones arrive in order
    send_json(&mut s.browser, json!({"t": "i", "d": b64(&vec![b'a'; 70_000])})).await; // over 64 KiB decoded
    send_json(&mut s.browser, json!({"t": "i", "d": "!!not base64"})).await;
    send_json(&mut s.browser, json!({"t": "i", "d": 5})).await;
    send_json(&mut s.browser, json!({"t": "r", "cols": 0, "rows": 10})).await;
    send_json(&mut s.browser, json!({"t": "r", "cols": 2000, "rows": 10})).await;
    send_json(&mut s.browser, json!({"t": "q"})).await;
    s.browser.send(WsMsg::Text("garbage".into())).await.unwrap();
    send_json(&mut s.browser, json!({"t": "i", "d": b64(b"ls\r")})).await;
    send_json(&mut s.browser, json!({"t": "r", "cols": 120, "rows": 40})).await;
    assert_eq!(recv_json(&mut s.worker).await, json!({"t": "i", "d": b64(b"ls\r")}));
    assert_eq!(recv_json(&mut s.worker).await, json!({"t": "r", "cols": 120, "rows": 40}));

    // the open is audited with who, worker, mission and mode, and no secret
    let audit = env.srv.state.db.audit_list(50).unwrap();
    let row = audit.iter().find(|r| r["action"] == "terminal.open").expect("audit row");
    assert_eq!(row["actor"], "admin");
    let detail = row["detail"].as_str().unwrap();
    assert!(
        detail.contains("worker=w1") && detail.contains("mission=m1") && detail.contains("mode=write") && detail.contains(&s.id),
        "{detail}"
    );
    assert!(!audit
        .iter()
        .any(|r| r.to_string().contains(&token) || r.to_string().contains(api.cookie.as_ref().unwrap())));

    // browser close: the worker gets terminal.close and its socket closes
    s.browser.close(None).await.unwrap();
    let close = control(&mut fw, "terminal.close").await;
    assert_eq!(close["payload"]["session_id"], s.id);
    expect_close(&mut s.worker).await;
    wait_sessions(&env, 0).await;
    env.stop().await;
}

async fn wait_sessions(env: &Env, n: usize) {
    for _ in 0..200 {
        if env.srv.state.hub.terminal_count() == n {
            return;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("terminal count did not reach {n}: {}", env.srv.state.hub.terminal_count());
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_end_closes_browser_with_reason() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut s = open_session(&env, &api, &mut fw, &token, "view").await;
    send_json(&mut s.worker, json!({"t": "x", "reason": "station ended"})).await;
    assert_eq!(recv_json(&mut s.browser).await, json!({"t": "x", "reason": "station ended"}));
    assert_eq!(expect_close(&mut s.browser).await, Some(1000));
    wait_sessions(&env, 0).await;

    // a worker socket that just drops ends the session too
    let mut s = open_session(&env, &api, &mut fw, &token, "view").await;
    drop(s.worker);
    let x = recv_json(&mut s.browser).await;
    assert_eq!(x["t"], "x");
    expect_close(&mut s.browser).await;
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn refused_status_is_relayed() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut browser = browser(&env, &api, "worker=w1&mission=nope").await;
    let open = control(&mut fw, "terminal.open").await;
    fw.send(
        "terminal.status",
        json!({"session_id": open["payload"]["session_id"], "state": "refused", "mode": "view", "reason": "unknown mission"}),
    )
    .await;
    assert_eq!(recv_json(&mut browser).await, json!({"t": "x", "reason": "unknown mission"}));
    expect_close(&mut browser).await;
    wait_sessions(&env, 0).await;
    // another worker cannot report on this session, and a malformed status is rejected
    fw.send("terminal.status", json!({"session_id": "x", "state": "bogus", "mode": "view", "reason": ""}))
        .await;
    assert_eq!(control(&mut fw, "error").await["payload"]["code"], "invalid-message");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn offline_or_unknown_worker_gets_error_frame() {
    let env = boot().await;
    let api = env.admin().await;
    let _ = api.add_worker("w1").await; // enrolled but never connected
    for worker in ["w1", "ghost"] {
        let mut b = browser(&env, &api, &format!("worker={worker}&mission=m1")).await;
        let x = recv_json(&mut b).await;
        assert_eq!(x, json!({"t": "x", "reason": "worker offline"}));
        expect_close(&mut b).await;
    }
    wait_sessions(&env, 0).await;
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn browser_upgrade_is_authenticated_and_origin_checked() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let cookie = format!("bridge_session={}", api.cookie.as_ref().unwrap());
    let q = "/api/v1/terminal?worker=w1&mission=m1";
    let refused = |e: tokio_tungstenite::tungstenite::Error, code: &str| assert!(format!("{e:?}").contains(code), "{e:?}");
    refused(env.ws(q, &[]).await.expect_err("no credentials"), "401");
    refused(
        env.ws(q, &[("cookie", "bridge_session=nope"), ("origin", &api.base)])
            .await
            .expect_err("bad cookie"),
        "401",
    );
    refused(env.ws(q, &[("authorization", "Bearer nope")]).await.expect_err("bad token"), "401");
    refused(env.ws(q, &[("cookie", &cookie)]).await.expect_err("no origin"), "403");
    refused(
        env.ws(q, &[("cookie", &cookie), ("origin", "https://evil.example")])
            .await
            .expect_err("wrong origin"),
        "403",
    );
    // a worker token is not an API credential
    refused(
        env.ws(q, &[("authorization", &format!("Bearer {token}"))]).await.expect_err("worker token"),
        "401",
    );
    // bad parameters are refused before the upgrade
    let h = [("cookie", cookie.as_str()), ("origin", api.base.as_str())];
    for bad in [
        "worker=W1&mission=m1",
        "worker=w1",
        "worker=w1&mission=m1&mode=admin",
        "worker=w1&mission=m1&cols=0",
        "worker=w1&mission=m1&rows=1001",
        "worker=w1&mission=a%20b",
    ] {
        refused(env.ws(&format!("/api/v1/terminal?{bad}"), &h).await.expect_err(bad), "400");
    }
    // nothing was sent to the worker by any refused attempt
    fw.barrier().await;
    assert_eq!(env.srv.state.hub.terminal_count(), 0);

    // an API token works without an Origin header
    let bearer = "api-token-for-terminal-test";
    env.srv.state.db.token_create("ci", &bridge_coordinator::auth::sha256_hex(bearer)).unwrap();
    let mut b = env.ws(q, &[("authorization", &format!("Bearer {bearer}"))]).await.expect("token upgrade");
    let open = control(&mut fw, "terminal.open").await;
    assert_eq!(open["payload"]["mode"], "view");
    let audit = env.srv.state.db.audit_list(10).unwrap();
    assert!(audit.iter().any(|r| r["action"] == "terminal.open" && r["actor"] == "token:ci"));
    b.close(None).await.unwrap();
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_attach_rules() {
    let env = boot().await;
    let api = env.admin().await;
    let t1 = api.add_worker("w1").await;
    let t2 = api.add_worker("w2").await;
    let (mut fw1, _) = Fw::connect(&env, &t1, "wid1", 0).await;
    let browser = browser(&env, &api, "worker=w1&mission=m1&mode=view").await;
    let open = control(&mut fw1, "terminal.open").await;
    let id = open["payload"]["session_id"].as_str().unwrap().to_string();
    let refused = |e: tokio_tungstenite::tungstenite::Error, code: &str| assert!(format!("{e:?}").contains(code), "{e:?}");

    refused(env.ws(&format!("/api/v1/worker/terminal/{id}"), &[]).await.expect_err("no token"), "401");
    refused(attach(&env, "nope", &id).await.expect_err("bad token"), "401");
    refused(attach(&env, &t2, &id).await.expect_err("other worker"), "404");
    refused(attach(&env, &t1, "unknown-session").await.expect_err("unknown id"), "404");
    refused(attach(&env, &t1, "bad%20id").await.expect_err("malformed id"), "404");
    // the browser's cookie is not a worker credential
    let h = browser_headers(&api);
    refused(
        env.ws(&format!("/api/v1/worker/terminal/{id}"), &[(h[0].0.as_str(), h[0].1.as_str())])
            .await
            .expect_err("cookie"),
        "401",
    );
    // the session is still attachable by its worker, once
    let _first = attach(&env, &t1, &id).await.expect("first attach");
    refused(attach(&env, &t1, &id).await.expect_err("duplicate"), "404");
    // once the session ends its id is gone
    drop(browser);
    wait_sessions(&env, 0).await;
    refused(attach(&env, &t1, &id).await.expect_err("ended"), "404");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn lagging_viewer_is_closed_with_1013() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut s = open_session(&env, &api, &mut fw, &token, "view").await;
    // the browser never reads while the worker floods output
    let chunk = json!({"t": "o", "d": b64(&vec![b'x'; 48 * 1024])}).to_string();
    let mut sent = 0usize;
    loop {
        if s.worker.send(WsMsg::Text(chunk.clone().into())).await.is_err() {
            break;
        }
        sent += chunk.len();
        assert!(sent < 400 * 1024 * 1024, "the coordinator never closed the lagging viewer");
        if sent % (4 * 1024 * 1024) < chunk.len() {
            // let the coordinator react; stop once it has torn the session down
            tokio::time::sleep(Duration::from_millis(20)).await;
            if env.srv.state.hub.terminal_count() == 0 {
                break;
            }
        }
    }
    wait_sessions(&env, 0).await;
    // now read: buffered output first, then the close with 1013
    let mut code = None;
    while let Ok(Some(m)) = tokio::time::timeout(Duration::from_secs(20), s.browser.next()).await {
        match m {
            Ok(WsMsg::Close(f)) => {
                code = f.map(|f| u16::from(f.code));
                break;
            }
            Ok(_) => {}
            Err(_) => break,
        }
    }
    assert_eq!(code, Some(1013));
    // the worker is told to stop
    let close = control(&mut fw, "terminal.close").await;
    assert_eq!(close["payload"]["session_id"], s.id);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn session_caps() {
    let env = boot().await;
    let api = env.admin().await;
    let mut fws = Vec::new();
    for n in 1..=5 {
        let name = format!("w{n}");
        let token = api.add_worker(&name).await;
        let ws = Fw::raw(&env, &token).await.unwrap();
        let mut fw = Fw {
            ws,
            worker_id: format!("wid{n}"),
            generation: 0,
            seq: 0,
        };
        fw.send(
            "hello",
            json!({"worker_id": fw.worker_id.clone(), "name": name, "software_version": "t", "protocol_versions": [1], "platform": {}, "capabilities": [], "missions": [], "event_cursor": 0}),
        )
        .await;
        assert_eq!(fw.recv().await["type"], "welcome");
        fws.push(fw);
    }
    let mut open = Vec::new();
    // 16 per worker
    for _ in 0..16 {
        open.push(browser(&env, &api, "worker=w1&mission=m1").await);
    }
    let mut over = browser(&env, &api, "worker=w1&mission=m1").await;
    let x = recv_json(&mut over).await;
    assert_eq!(x["t"], "x");
    assert!(x["reason"].as_str().unwrap().contains("worker"), "{x}");
    assert_eq!(expect_close(&mut over).await, Some(1013));
    // 64 in total
    for w in ["w2", "w3", "w4"] {
        for _ in 0..16 {
            open.push(browser(&env, &api, &format!("worker={w}&mission=m1")).await);
        }
    }
    wait_sessions(&env, 64).await;
    let mut over = browser(&env, &api, "worker=w5&mission=m1").await;
    let x = recv_json(&mut over).await;
    assert!(x["reason"].as_str().unwrap().contains("coordinator"), "{x}");
    assert_eq!(expect_close(&mut over).await, Some(1013));
    drop(open);
    wait_sessions(&env, 0).await;
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn silent_browser_terminal_is_closed_after_idle_timeout() {
    // NET-010: the server pings; a browser that never answers is closed with 1001.
    let env = boot_with(|t| {
        t.replace("ws_ping_interval_secs = 30", "ws_ping_interval_secs = 1")
            .replace("ws_idle_timeout_secs = 90", "ws_idle_timeout_secs = 2")
    })
    .await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect(&env, &token, "wid1", 0).await;
    let mut s = open_session(&env, &api, &mut fw, &token, "view").await;
    tokio::time::sleep(Duration::from_millis(4500)).await;
    // A client that never read its queued pings may see a reset instead of the close frame.
    let code = expect_close(&mut s.browser).await;
    assert!(matches!(code, None | Some(1001)), "{code:?}");
    env.stop().await;
}
