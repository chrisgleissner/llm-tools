//! Security hardening: login rate limiting, slow and oversized bodies, connection caps,
//! worker pre-hello limits, generation handling and API token lifetime.
mod common;
use bridge_coordinator::auth::{addr_key, RateLimiter};
use common::*;
use reqwest::Method;
use serde_json::{json, Value};
use std::net::IpAddr;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;

type Tls = tokio_rustls::client::TlsStream<TcpStream>;

async fn tls_connect(env: &Env) -> Tls {
    let tcp = TcpStream::connect(env.addr).await.unwrap();
    TlsConnector::from(env.tls.clone())
        .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
        .await
        .unwrap()
}

/// Read until the peer closes or `limit` passes; returns what arrived.
async fn read_to_close(s: &mut Tls, limit: Duration) -> (String, bool) {
    let mut out = Vec::new();
    let mut buf = [0u8; 4096];
    let end = Instant::now() + limit;
    loop {
        match tokio::time::timeout(end.saturating_duration_since(Instant::now()), s.read(&mut buf)).await {
            Err(_) => return (String::from_utf8_lossy(&out).into_owned(), false),
            Ok(Ok(0) | Err(_)) => return (String::from_utf8_lossy(&out).into_owned(), true),
            Ok(Ok(n)) => out.extend_from_slice(&buf[..n]),
        }
    }
}

fn set(key: &str, value: &str) -> impl FnOnce(String) -> String {
    let (k, v) = (key.to_string(), value.to_string());
    move |t| {
        let mut out = String::new();
        for line in t.lines() {
            if line.starts_with(&format!("{k} = ")) {
                out += &format!("{k} = {v}\n");
            } else {
                out += line;
                out.push('\n');
            }
        }
        out
    }
}

// ------------------------------------------------------------ H1 / L1: login rate limit

#[tokio::test(flavor = "multi_thread")]
async fn concurrent_wrong_logins_from_one_address_are_limited_before_argon2() {
    let env = boot_with(set("max_connections_per_ip", "200")).await;
    let _admin = env.admin().await; // creates the administrator; the successful login makes 127.0.0.1 a known address
    let api = env.api();
    let mut tasks = Vec::new();
    for _ in 0..60 {
        let (c, url) = (api.c.clone(), format!("{}/api/v1/auth/login", api.base));
        tasks.push(tokio::spawn(async move {
            c.post(url)
                .json(&json!({"username": "admin", "password": "not the password"}))
                .send()
                .await
                .unwrap()
                .status()
                .as_u16()
        }));
    }
    let mut codes = Vec::new();
    for t in tasks {
        codes.push(t.await.unwrap());
    }
    // A request that got a 401 had its password verified (Argon2); 429 was refused before.
    let verified = codes.iter().filter(|c| **c == 401).count();
    let refused = codes.iter().filter(|c| **c == 429).count();
    assert_eq!(verified, 5, "only the limit reaches Argon2: {codes:?}");
    assert_eq!(refused, 55, "{codes:?}");
    env.stop().await;
}

#[test]
fn ipv6_clients_are_keyed_by_their_64() {
    let a: IpAddr = "2001:db8:1:2:aaaa::1".parse().unwrap();
    let b: IpAddr = "2001:db8:1:2:bbbb:cccc:dddd:eeee".parse().unwrap();
    let c: IpAddr = "2001:db8:1:3::1".parse().unwrap();
    assert_eq!(addr_key(&a), addr_key(&b));
    assert_ne!(addr_key(&a), addr_key(&c));
    let mapped: IpAddr = "::ffff:192.0.2.1".parse().unwrap();
    assert_eq!(addr_key(&mapped), "192.0.2.1");
}

#[test]
fn full_key_table_refuses_new_keys_instead_of_ignoring_them() {
    let l = RateLimiter::new(5, Duration::from_secs(60), 4);
    let held: Vec<_> = (0..4).map(|i| l.acquire(&[format!("k{i}")], false).unwrap()).collect();
    assert!(l.acquire(&["new".to_string()], false).is_err(), "table full of live keys: fail closed");
    assert!(l.acquire(&["new".to_string()], true).is_ok(), "privileged callers may use the reserve");
    assert!(l.acquire(&["k0".to_string()], false).is_ok(), "an existing key is not a new entry");
    drop(held);
}

#[test]
fn in_flight_attempts_count_against_the_limit() {
    let l = RateLimiter::new(2, Duration::from_secs(60), 100);
    let k = vec!["k".to_string()];
    let a = l.acquire(&k, false).unwrap();
    let b = l.acquire(&k, false).unwrap();
    assert!(l.acquire(&k, false).is_err());
    a.succeed();
    let c = l.acquire(&k, false).expect("a finished successful attempt frees its slot");
    b.fail();
    c.fail();
    assert!(l.acquire(&k, false).is_err(), "two recorded failures");
}

#[tokio::test(flavor = "multi_thread")]
async fn user_limit_cannot_lock_the_administrator_out_of_a_known_address() {
    let env = boot().await;
    let _admin = env.admin().await; // successful login from 127.0.0.1
                                    // Bad passwords for "admin" from many other addresses fill the per-user limit.
    for _ in 0..5 {
        env.srv.state.limiter.acquire(&["user:admin".to_string()], false).unwrap().fail();
    }
    let mut api = env.api();
    assert_eq!(api.login("admin", PASSWORD).await, 200, "known address is not refused by the per-user limit");
    // The same address once it is no longer known (no successful login for more than 7 days) is refused.
    env.srv.state.db.known_addr_age("127.0.0.1", 0).unwrap();
    assert_eq!(api.login("admin", PASSWORD).await, 429);
    env.stop().await;
}

// ------------------------------------------------------------ M2: bodies on unauthenticated routes

#[tokio::test(flavor = "multi_thread")]
async fn auth_bodies_are_limited_to_4_kib() {
    let env = boot().await;
    let api = env.api();
    let big = "x".repeat(5000);
    let (st, body) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/login")
                .json(&json!({"username": "admin", "password": big})),
        )
        .await;
    assert_eq!(st, 413, "{body}");
    let (st, _) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/setup")
                .json(&json!({"setup_token": big, "username": "a", "password": "b"})),
        )
        .await;
    assert_eq!(st, 413);
    // an oversized body is not a failed login
    let (st, _) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/login")
                .json(&json!({"username": "admin", "password": "short"})),
        )
        .await;
    assert_eq!(st, 401);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn login_rate_limit_is_checked_before_the_body_is_read() {
    let env = boot().await;
    let _admin = env.admin().await;
    let api = env.api();
    for _ in 0..5 {
        let (st, _) = api
            .send(
                api.bare(Method::POST, "/api/v1/auth/login")
                    .json(&json!({"username": "admin", "password": "wrong password!"})),
            )
            .await;
        assert_eq!(st, 401);
    }
    // Headers announce a body that never arrives: a limited address is answered without waiting for it.
    let mut s = tls_connect(&env).await;
    let req = format!(
        "POST /api/v1/auth/login HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n",
        env.addr.port()
    );
    s.write_all(req.as_bytes()).await.unwrap();
    let mut buf = [0u8; 1024];
    let n = tokio::time::timeout(Duration::from_secs(3), s.read(&mut buf))
        .await
        .expect("answered before the body arrived")
        .unwrap();
    assert!(
        String::from_utf8_lossy(&buf[..n]).starts_with("HTTP/1.1 429"),
        "{}",
        String::from_utf8_lossy(&buf[..n])
    );
    env.stop().await;
}

fn authed_post_head(env: &Env, api: &Api, path: &str, length: usize) -> String {
    format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nCookie: __Host-bridge_session={}\r\nOrigin: {}\r\nX-CSRF-Token: {}\r\n\
         Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n",
        env.addr.port(),
        api.cookie.as_ref().unwrap(),
        api.base,
        api.csrf.as_ref().unwrap()
    )
}

#[tokio::test(flavor = "multi_thread")]
async fn buffered_large_bodies_are_bounded() {
    let env = boot_with(set("request_body_timeout_secs", "3")).await;
    let api = env.admin().await;
    let slots = env.srv.state.big_bodies.clone();
    assert_eq!(slots.available_permits(), 8);
    // Eight 200 kB bodies that stall after one byte take all the permits.
    let mut held = Vec::new();
    for _ in 0..8 {
        let mut s = tls_connect(&env).await;
        s.write_all(authed_post_head(&env, &api, "/api/v1/commands", 200_000).as_bytes()).await.unwrap();
        s.write_all(b"{").await.unwrap();
        held.push(s);
    }
    let end = Instant::now() + Duration::from_secs(2);
    while slots.available_permits() > 0 && Instant::now() < end {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(slots.available_permits(), 0);
    // A ninth is refused at once instead of buffering more memory.
    let mut s = tls_connect(&env).await;
    s.write_all(authed_post_head(&env, &api, "/api/v1/commands", 200_000).as_bytes()).await.unwrap();
    s.write_all(b"{").await.unwrap();
    let (text, _) = read_to_close(&mut s, Duration::from_secs(2)).await;
    assert!(text.starts_with("HTTP/1.1 503"), "{text}");
    // Small requests are unaffected.
    assert_eq!(api.get("/api/v1/workers").await.0, 200);
    // The stalled bodies time out and release their permits.
    for mut s in held {
        let (text, closed) = read_to_close(&mut s, Duration::from_secs(8)).await;
        assert!(closed && text.starts_with("HTTP/1.1 408"), "{text}");
    }
    assert_eq!(slots.available_permits(), 8);
    env.stop().await;
}

// ------------------------------------------------------------ M1: slow bodies and connection caps

#[tokio::test(flavor = "multi_thread")]
async fn slow_bodies_do_not_hold_connection_slots() {
    let env = boot_with(|t| {
        set("http_idle_timeout_secs", "1")(set("worker_reserve_connections", "0")(set("request_body_timeout_secs", "2")(set(
            "max_connections",
            "4",
        )(t))))
    })
    .await;
    let api = env.admin().await;
    // Let the keep-alive connections of the setup requests expire so all four slots are free.
    tokio::time::sleep(Duration::from_millis(1600)).await;
    let mut held = Vec::new();
    for _ in 0..4 {
        let mut s = tls_connect(&env).await;
        s.write_all(authed_post_head(&env, &api, "/api/v1/workers", 1000).as_bytes()).await.unwrap();
        s.write_all(b"{").await.unwrap();
        held.push(s);
    }
    // Every slot is taken: a new connection is dropped.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut c = TcpStream::connect(env.addr).await.unwrap();
    let mut b = [0u8; 8];
    assert!(matches!(tokio::time::timeout(Duration::from_secs(2), c.read(&mut b)).await, Ok(Ok(0) | Err(_))));
    // The stalled requests are answered with 408 after the body timeout and the slots come back.
    for mut s in held {
        let (text, closed) = read_to_close(&mut s, Duration::from_secs(6)).await;
        assert!(closed && text.starts_with("HTTP/1.1 408"), "{text}");
    }
    let mut ok = false;
    for _ in 0..20 {
        if let Ok(r) = env.client.get(format!("{}/healthz", env.base())).send().await {
            ok = r.status() == 200;
            if ok {
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok, "slots released after the body timeout");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn connections_per_address_are_capped() {
    let env = boot_with(set("max_connections_per_ip", "3")).await;
    let held: Vec<_> = futures_util::future::join_all((0..3).map(|_| TcpStream::connect(env.addr))).await;
    let held: Vec<_> = held.into_iter().map(Result::unwrap).collect();
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut c = TcpStream::connect(env.addr).await.unwrap();
    let mut b = [0u8; 8];
    let r = tokio::time::timeout(Duration::from_secs(2), c.read(&mut b))
        .await
        .expect("refused without hanging");
    assert!(matches!(r, Ok(0) | Err(_)), "{r:?}");
    drop(held);
    let mut ok = false;
    for _ in 0..30 {
        if let Ok(r) = env.client.get(format!("{}/healthz", env.base())).send().await {
            if r.status() == 200 {
                ok = true;
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok, "accepts again once the address is under its cap");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_reserve_admits_workers_when_general_slots_are_full() {
    let env = boot_with(|t| set("worker_reserve_connections", "2")(set("max_connections", "8")(set("max_connections_per_ip", "100")(t)))).await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    // Six idle sockets (no TLS handshake yet) take every general slot.
    let fill: Vec<_> = futures_util::future::join_all((0..6).map(|_| TcpStream::connect(env.addr))).await;
    let _fill: Vec<_> = fill.into_iter().map(Result::unwrap).collect();
    tokio::time::sleep(Duration::from_millis(300)).await;
    // A browser-style request lands in the reserve and is refused.
    let mut s = tls_connect(&env).await;
    s.write_all(format!("GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n", env.addr.port()).as_bytes())
        .await
        .unwrap();
    let (text, _) = read_to_close(&mut s, Duration::from_secs(3)).await;
    assert!(text.starts_with("HTTP/1.1 503"), "{text}");
    // A worker with a valid token still gets in.
    let (mut fw, welcome) = Fw::connect(&env, &token, "wid-1", 0).await;
    assert_eq!(welcome["type"], "welcome");
    fw.barrier().await;
    // A worker token that is not valid is refused in the reserve like any other request.
    let r = Fw::raw(&env, "not-a-token").await;
    assert!(r.is_err());
    env.stop().await;
}

// ------------------------------------------------------------ L3: pre-hello

fn hello_payload(worker_id: &str) -> Value {
    json!({"worker_id": worker_id, "name": "w1", "software_version": "test", "protocol_versions": [1],
        "platform": {"system": "Linux"}, "capabilities": caps(), "missions": [], "event_cursor": 0})
}

#[tokio::test(flavor = "multi_thread")]
async fn one_pending_connection_per_worker_token() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let first = Fw::raw(&env, &token).await.unwrap();
    // The first connection has not sent hello: a second one with the same token is refused.
    assert!(Fw::raw(&env, &token).await.is_err(), "second pre-hello connection must be refused");
    // Another worker token is independent.
    let other = api.add_worker("w2").await;
    let _second_worker = Fw::raw(&env, &other).await.unwrap();
    // After its hello the first connection no longer holds the slot.
    let mut fw = Fw {
        ws: first,
        worker_id: "wid-1".into(),
        generation: 0,
        seq: 0,
    };
    fw.send("hello", hello_payload("wid-1")).await;
    assert_eq!(fw.recv().await["type"], "welcome");
    let again = Fw::raw(&env, &token).await.expect("slot is free after hello");
    drop(again);
    // A connection that closes without hello frees the slot too.
    drop(_second_worker);
    let mut freed = false;
    for _ in 0..40 {
        if Fw::raw(&env, &other).await.is_ok() {
            freed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(freed, "slot freed when the silent connection closed");
    drop(fw);
    env.stop().await;
}

// ------------------------------------------------------------ M3: errors and generations

fn error_message(worker_id: &str, generation: u64, in_reply_to: &str) -> String {
    json!({"v": 1, "type": "error", "id": "err-1", "worker_id": worker_id, "generation": generation,
           "payload": {"code": "stale-generation", "message": "test", "in_reply_to": in_reply_to}})
    .to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn error_messages_are_never_answered_with_errors() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _welcome) = Fw::connect(&env, &token, "wid-1", 0).await;
    // An error carrying the wrong generation and the wrong worker id: no reply of any kind.
    fw.send_raw(error_message("wid-1", fw.generation + 5, "some-command")).await;
    fw.send_raw(error_message("someone-else", fw.generation, "some-command")).await;
    fw.send("ping", json!({})).await;
    let next = fw.recv().await;
    assert_eq!(next["type"], "pong", "the first message back must answer the ping, got {next}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_generation_reply_to_welcome_closes_the_connection() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, welcome) = Fw::connect(&env, &token, "wid-1", 0).await;
    // A stale-generation error about some other message does not close the connection.
    fw.send_raw(error_message("wid-1", fw.generation, "unrelated")).await;
    fw.barrier().await;
    // The worker refusing the welcome generation does.
    fw.send_raw(error_message("wid-1", fw.generation, welcome["id"].as_str().unwrap())).await;
    assert_eq!(fw.expect_close().await, Some(1008));
    let audit = env.srv.state.db.audit_list(50).unwrap();
    let row = audit.iter().find(|r| r["action"] == "worker.stale-generation").expect("audit entry");
    assert_eq!(row["actor"], "worker:w1");
    assert!(row["detail"].as_str().unwrap().contains("closed"), "{row}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn re_added_worker_gets_a_generation_above_every_earlier_one() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut a, _) = Fw::connect(&env, &token, "wid-1", 0).await;
    a.barrier().await;
    drop(a);
    let (mut b, _) = Fw::connect(&env, &token, "wid-1", 0).await;
    b.barrier().await;
    let issued = b.generation;
    assert!(issued >= 2);
    drop(b);
    let (st, _) = api.send(api.rb(Method::DELETE, "/api/v1/workers/w1")).await;
    assert_eq!(st, 200);
    let token2 = api.add_worker("w1").await;
    let (c, welcome) = Fw::connect(&env, &token2, "wid-1", 0).await;
    assert!(c.generation > issued, "re-added worker got {} after {issued}: {welcome}", c.generation);
    env.stop().await;
}

// ------------------------------------------------------------ M5: API tokens

fn bearer(env: &Env, token: &str) -> Api {
    let mut a = env.api();
    a.bearer = Some(token.to_string());
    a
}

#[tokio::test(flavor = "multi_thread")]
async fn expired_api_tokens_are_refused() {
    let env = boot().await;
    let _admin = env.admin().await;
    let db = &env.srv.state.db;
    let now = bridge_coordinator::now();
    for (name, expires) in [("expired", Some(now - 1)), ("future", Some(now + 3600)), ("forever", None)] {
        let hash = bridge_coordinator::auth::sha256_hex(name);
        assert!(db.token_create_scoped(name, &hash, expires, &["admin".into()]).unwrap());
    }
    assert_eq!(bearer(&env, "expired").get("/api/v1/workers").await.0, 401);
    assert_eq!(bearer(&env, "future").get("/api/v1/workers").await.0, 200);
    assert_eq!(bearer(&env, "forever").get("/api/v1/workers").await.0, 200);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn password_change_revokes_api_tokens_unless_kept() {
    let env = boot().await;
    let mut api = env.admin().await;
    let db = &env.srv.state.db;
    db.token_create_scoped("ci", &bridge_coordinator::auth::sha256_hex("ci-secret"), None, &["admin".into()])
        .unwrap();
    assert_eq!(bearer(&env, "ci-secret").get("/api/v1/workers").await.0, 200);
    let new1 = "another long password 1";
    let (st, body) = api
        .post("/api/v1/auth/password", json!({"current": PASSWORD, "new": new1, "keep_api_tokens": true}))
        .await;
    assert_eq!((st, body["revoked_api_tokens"].clone()), (200, json!(0)), "{body}");
    assert_eq!(bearer(&env, "ci-secret").get("/api/v1/workers").await.0, 200, "kept on request");
    assert_eq!(api.login("admin", new1).await, 200);
    let (st, body) = api
        .post("/api/v1/auth/password", json!({"current": new1, "new": "another long password 2"}))
        .await;
    assert_eq!((st, body["revoked_api_tokens"].clone()), (200, json!(1)), "{body}");
    assert_eq!(bearer(&env, "ci-secret").get("/api/v1/workers").await.0, 401, "revoked by default");
    let audit = db.audit_list(50).unwrap();
    assert!(audit
        .iter()
        .any(|r| r["action"] == "password.change" && r["detail"].as_str().unwrap().contains("1 API token")));
    env.stop().await;
}

// ------------------------------------------------------------ L7: cookie

#[tokio::test(flavor = "multi_thread")]
async fn session_cookie_uses_the_host_prefix() {
    let env = boot().await;
    let api = env.admin().await;
    let anon = env.api();
    let r = anon
        .bare(Method::POST, "/api/v1/auth/login")
        .json(&json!({"username": "admin", "password": PASSWORD}))
        .send()
        .await
        .unwrap();
    let sc = r.headers().get("set-cookie").unwrap().to_str().unwrap().to_string();
    assert!(sc.starts_with("__Host-bridge_session="), "{sc}");
    assert!(sc.contains("Secure") && sc.contains("Path=/") && !sc.contains("Domain"), "{sc}");
    // The old unprefixed name no longer authenticates.
    let legacy = format!("bridge_session={}", api.cookie.as_ref().unwrap());
    let (st, _) = anon.send(anon.bare(Method::GET, "/api/v1/workers").header("cookie", legacy)).await;
    assert_eq!(st, 401);
    assert_eq!(api.get("/api/v1/workers").await.0, 200);
    env.stop().await;
}

// ------------------------------------------------------------ schema migration 1 -> 2

#[test]
fn version_1_database_is_migrated_in_place() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("v1.sqlite3");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch(
            "CREATE TABLE workers(name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, worker_id TEXT,
               generation INTEGER NOT NULL DEFAULT 0, event_cursor INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
               last_seen INTEGER, hello_at INTEGER, last_event_at INTEGER, software_version TEXT, protocol_versions TEXT,
               platform TEXT, capabilities TEXT);
             CREATE TABLE missions(worker TEXT NOT NULL, mission_id TEXT NOT NULL, info TEXT NOT NULL, PRIMARY KEY(worker, mission_id));
             CREATE TABLE api_tokens(name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, last_used INTEGER);
             INSERT INTO workers(name, token_hash, worker_id, generation, created_at) VALUES('w1', 'h1', 'wid', 7, 0);
             INSERT INTO api_tokens(name, token_hash, created_at) VALUES('old', 'h2', 0);
             PRAGMA user_version = 1;",
        )
        .unwrap();
    }
    let db = bridge_coordinator::db::Db::open(&path).unwrap();
    let hello = serde_json::Map::new();
    match db.worker_hello("w1", "wid", &hello, &json!([]), "192.0.2.10").unwrap() {
        bridge_coordinator::db::HelloOutcome::Ok { generation, .. } => assert_eq!(generation, 8),
        _ => panic!("hello refused"),
    }
    assert_eq!(
        db.tokens_list().unwrap(),
        vec![("old".to_string(), None)],
        "existing tokens keep working and do not expire"
    );
    assert_eq!(db.token_lookup("h2").unwrap().unwrap().scopes, vec!["admin"]);
    let v: i64 = rusqlite::Connection::open(&path)
        .unwrap()
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(v, bridge_coordinator::db::SCHEMA_VERSION);
    let _ = std::fs::remove_dir_all(dir);
}
