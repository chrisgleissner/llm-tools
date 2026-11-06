mod common;
use common::*;
use reqwest::Method;
use serde_json::json;

/// Unix permission bits. Windows has no permission bits; the data directory's ACL applies there.
#[cfg(unix)]
fn mode(p: &std::path::Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).unwrap().permissions().mode() & 0o777
}

#[test]
fn init_is_idempotent_and_private() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    let files = ["ca.pem", "ca.key", "server.pem", "server.key", "config.toml", "setup-token"];
    let before: Vec<Vec<u8>> = files.iter().map(|f| std::fs::read(dir.join(f)).unwrap()).collect();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    let after: Vec<Vec<u8>> = files.iter().map(|f| std::fs::read(dir.join(f)).unwrap()).collect();
    assert_eq!(before, after, "init must never overwrite existing files");
    #[cfg(unix)]
    {
        assert_eq!(mode(&dir), 0o700);
        for f in ["ca.key", "server.key", "setup-token", "coordinator.sqlite3"] {
            assert_eq!(mode(&dir.join(f)), 0o600, "{f}");
        }
    }
    assert!(std::fs::read_to_string(dir.join("config.toml")).unwrap().contains("127.0.0.1:8443"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn non_loopback_bind_needs_lan_enabled() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    edit_config(&dir, |t| t.replace("127.0.0.1:8443", "0.0.0.0:0"));
    let err = bridge_coordinator::server::start(&dir).await.err().expect("must refuse");
    assert!(err.contains("lan_enabled"), "{err}");
    edit_config(&dir, |t| t.replace("lan_enabled = false", "lan_enabled = true"));
    let srv = bridge_coordinator::server::start(&dir).await.expect("allowed with lan_enabled");
    srv.stop().await;
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn setup_token_is_single_use() {
    let env = boot().await;
    let api = env.api();
    let (_, st) = api.send(api.bare(Method::GET, "/api/v1/auth/state")).await;
    assert_eq!(st["setup_required"], true);
    assert_eq!(st["authenticated"], false);
    assert!(st["password_policy"]["min_length"].is_u64(), "the public state publishes the password policy");
    let token = std::fs::read_to_string(env.dir.join("setup-token")).unwrap();
    let body = |t: &str, pw: &str| json!({"setup_token": t, "username": "admin", "password": pw});
    let post = |b| api.send(api.bare(Method::POST, "/api/v1/auth/setup").json(&b));
    assert_eq!(post(body("wrong", PASSWORD)).await.0, 403);
    assert_eq!(post(body(token.trim(), "short")).await.0, 400, "bad password does not consume the token");
    assert!(env.dir.join("setup-token").exists());
    assert_eq!(post(body(token.trim(), PASSWORD)).await.0, 201);
    assert!(!env.dir.join("setup-token").exists(), "token file consumed");
    assert_eq!(post(body(token.trim(), PASSWORD)).await.0, 409);
    let (_, st) = api.send(api.bare(Method::GET, "/api/v1/auth/state")).await;
    assert_eq!(st["setup_required"], false);
    let audit = env.srv.state.db.audit_list(50).unwrap();
    assert!(audit.iter().any(|r| r["action"] == "setup"));
    assert!(audit.iter().any(|r| r["action"] == "setup.failure"));
    assert!(!audit.iter().any(|r| r.to_string().contains(token.trim())), "tokens never reach the audit log");
    env.stop().await;
}

const PROTECTED: &[(&str, &str)] = &[
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/password"),
    ("GET", "/api/v1/workers"),
    ("POST", "/api/v1/workers"),
    ("DELETE", "/api/v1/workers/w1"),
    ("GET", "/api/v1/missions"),
    ("POST", "/api/v1/commands"),
    ("GET", "/api/v1/commands/x"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/stream"),
    ("GET", "/api/v1/diagnostics"),
];

#[tokio::test(flavor = "multi_thread")]
async fn login_session_lifecycle() {
    let env = boot().await;
    let mut api = env.admin().await;
    // every protected route needs authentication
    let anon = env.api();
    for (m, p) in PROTECTED {
        let (st, body) = anon.send(anon.bare(m.parse().unwrap(), p)).await;
        assert_eq!(st, 401, "{m} {p}: {body}");
    }
    let (st, _) = anon
        .send(anon.rb(Method::GET, "/api/v1/workers").header("cookie", "__Host-bridge_session=garbage"))
        .await;
    assert_eq!(st, 401);
    assert_eq!(anon.send(anon.bare(Method::GET, "/healthz")).await.1, json!({"status": "ok"}));

    // identical error for unknown user and wrong password
    let unknown = anon
        .send(
            anon.bare(Method::POST, "/api/v1/auth/login")
                .json(&json!({"username": "nobody", "password": PASSWORD})),
        )
        .await;
    let wrong = anon
        .send(
            anon.bare(Method::POST, "/api/v1/auth/login")
                .json(&json!({"username": "admin", "password": "wrong password!"})),
        )
        .await;
    assert_eq!(unknown, wrong);
    assert_eq!(unknown.0, 401);

    // cookie attributes
    let r = anon
        .bare(Method::POST, "/api/v1/auth/login")
        .json(&json!({"username": "admin", "password": PASSWORD}))
        .send()
        .await
        .unwrap();
    let sc = r.headers().get("set-cookie").unwrap().to_str().unwrap().to_string();
    for attr in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/"] {
        assert!(sc.contains(attr), "{sc}");
    }
    assert_eq!(api.get("/api/v1/workers").await.0, 200);
    assert_eq!(api.get("/api/v1/auth/state").await.1["authenticated"], true);

    // logout deletes the session
    assert_eq!(api.post("/api/v1/auth/logout", json!({})).await.0, 200);
    assert_eq!(api.get("/api/v1/workers").await.0, 401);

    // idle and absolute expiry
    let now = bridge_coordinator::now();
    for (last_seen, expires) in [(now - 8 * 3600 - 5, now + 1000), (now, now - 1)] {
        assert_eq!(api.login("admin", PASSWORD).await, 200);
        assert_eq!(api.get("/api/v1/workers").await.0, 200);
        let hash = bridge_coordinator::auth::sha256_hex(api.cookie.as_ref().unwrap());
        env.srv.state.db.session_age(&hash, last_seen, expires).unwrap();
        assert_eq!(api.get("/api/v1/workers").await.0, 401, "expired session ({last_seen},{expires})");
    }
    let audit = env.srv.state.db.audit_list(100).unwrap();
    assert!(audit.iter().any(|r| r["action"] == "login.failure"));
    assert!(audit.iter().any(|r| r["action"] == "login.success"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn password_change_deletes_all_sessions() {
    let env = boot().await;
    let mut a = env.admin().await;
    let mut b = env.api();
    assert_eq!(b.login("admin", PASSWORD).await, 200);
    assert_eq!(
        a.post("/api/v1/auth/password", json!({"current": "not the password", "new": "another long password"}))
            .await
            .0,
        403
    );
    assert_eq!(a.post("/api/v1/auth/password", json!({"current": PASSWORD, "new": "short"})).await.0, 400);
    assert_eq!(
        a.post("/api/v1/auth/password", json!({"current": PASSWORD, "new": "another long password"}))
            .await
            .0,
        200
    );
    assert_eq!(a.get("/api/v1/workers").await.0, 401);
    assert_eq!(b.get("/api/v1/workers").await.0, 401);
    assert_eq!(env.srv.state.db.session_count().unwrap(), 0);
    assert_eq!(a.login("admin", PASSWORD).await, 401);
    assert_eq!(a.login("admin", "another long password").await, 200);
    assert!(env.srv.state.db.audit_list(50).unwrap().iter().any(|r| r["action"] == "password.change"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn login_rate_limit_per_username() {
    let env = boot().await;
    let mut api = env.admin().await;
    for _ in 0..5 {
        assert_eq!(api.login("admin", "wrong password!").await, 401);
    }
    assert_eq!(api.login("admin", PASSWORD).await, 429, "limited even with the right password");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn login_rate_limit_per_address() {
    let env = boot().await;
    let mut api = env.admin().await;
    for i in 0..5 {
        assert_eq!(api.login(&format!("user{i}"), "wrong password!").await, 401);
    }
    assert_eq!(api.login("admin", PASSWORD).await, 429, "the client address is limited across usernames");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn csrf_origin_and_bearer_tokens() {
    let env = boot().await;
    let api = env.admin().await;
    let create = |rb: reqwest::RequestBuilder| api.send(rb.json(&json!({"name": "w-csrf"})));
    let path = "/api/v1/workers";
    // cookie only: no CSRF header, no Origin, wrong values
    let cookie = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    let base = || api.bare(Method::POST, path).header("cookie", cookie.clone());
    assert_eq!(create(base().header("origin", env.origin())).await.0, 403, "missing CSRF token");
    assert_eq!(
        create(base().header("origin", env.origin()).header("x-csrf-token", "nope")).await.0,
        403,
        "wrong CSRF token"
    );
    let csrf = api.csrf.clone().unwrap();
    assert_eq!(create(base().header("x-csrf-token", csrf.clone())).await.0, 403, "missing Origin");
    assert_eq!(
        create(base().header("x-csrf-token", csrf.clone()).header("origin", "https://evil.example"))
            .await
            .0,
        403,
        "foreign Origin"
    );
    assert_eq!(
        create(base().header("x-csrf-token", csrf.clone()).header("origin", format!("{}/", env.origin())))
            .await
            .0,
        403,
        "Origin must match exactly"
    );
    assert_eq!(create(base().header("x-csrf-token", csrf).header("origin", env.origin())).await.0, 201);

    // bearer API token: no CSRF or Origin needed, and revocable
    let tok = bridge_coordinator::auth::random_token();
    env.srv
        .state
        .db
        .token_create_scoped("cli", &bridge_coordinator::auth::sha256_hex(&tok), None, &["admin".into()])
        .unwrap();
    let mut bearer = env.api();
    bearer.bearer = Some(tok.clone());
    assert_eq!(bearer.post("/api/v1/workers", json!({"name": "w-bearer"})).await.0, 201);
    assert_eq!(bearer.get("/api/v1/diagnostics").await.0, 200);
    env.srv.state.db.token_revoke("cli").unwrap();
    assert_eq!(bearer.get("/api/v1/workers").await.0, 401);
    let audit = env.srv.state.db.audit_list(50).unwrap();
    assert!(audit.iter().any(|r| r["action"] == "worker.add" && r["actor"] == "token:cli"));

    // the stream upgrade checks Origin for cookie sessions
    let c = format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap());
    assert!(env.ws("/api/v1/stream", &[("cookie", &c)]).await.is_err(), "no Origin");
    assert!(
        env.ws("/api/v1/stream", &[("cookie", &c), ("origin", "https://evil.example")]).await.is_err(),
        "foreign Origin"
    );
    assert!(env.ws("/api/v1/stream", &[("cookie", &c), ("origin", &env.origin())]).await.is_ok());
    assert!(env.ws("/api/v1/stream", &[]).await.is_err(), "unauthenticated");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn body_limit_and_placeholder() {
    let env = boot().await;
    let api = env.admin().await;
    let big = "x".repeat(1024 * 1024 + 10);
    let (st, _) = api.send(api.rb(Method::POST, "/api/v1/workers").body(big)).await;
    assert_eq!(st, 413);
    let r = api.bare(Method::GET, "/").send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.text().await.unwrap().contains("Bridge coordinator"));
    let (st, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(st, 200);
    assert!(d["db_bytes"].as_i64().unwrap() > 0 && d["uptime_seconds"].is_number());
    env.stop().await;
}
