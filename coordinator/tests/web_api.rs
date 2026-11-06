//! API features the browser UI depends on: password policy and session details in `auth/state`,
//! the "remember this device" session policy, and filtering, sorting and paging of `GET /missions`.
mod common;
use common::*;
use reqwest::Method;
use serde_json::{json, Value};

async fn login_with(env: &Env, remember: Option<Value>) -> (Api, String) {
    let mut api = env.api();
    let mut body = json!({"username": "admin", "password": PASSWORD});
    if let Some(r) = remember {
        body["remember"] = r;
    }
    let r = api.bare(Method::POST, "/api/v1/auth/login").json(&body).send().await.unwrap();
    assert_eq!(r.status().as_u16(), 200);
    let sc = r.headers().get("set-cookie").unwrap().to_str().unwrap().to_string();
    api.cookie = Some(sc.split(';').next().unwrap().strip_prefix("__Host-bridge_session=").unwrap().to_string());
    api.csrf = Some(r.json::<Value>().await.unwrap()["csrf_token"].as_str().unwrap().to_string());
    (api, sc)
}

#[tokio::test(flavor = "multi_thread")]
async fn auth_state_publishes_the_password_policy_and_session_details() {
    let env = boot().await;
    let anon = env.api();
    let (st, body) = anon.get("/api/v1/auth/state").await;
    assert_eq!(st, 200);
    assert_eq!(body["password_policy"]["min_length"], bridge_coordinator::auth::MIN_PASSWORD_LEN);
    assert_eq!(body["password_policy"]["max_length"], bridge_coordinator::auth::MAX_PASSWORD_LEN);
    assert!(
        body.get("username").is_none() && body.get("session").is_none(),
        "nothing about the session before sign-in: {body}"
    );
    assert_eq!(body["session_policy"]["remembered"]["idle_secs"], bridge_coordinator::db::SESSION_IDLE_SECS);
    assert_eq!(
        body["session_policy"]["default"]["absolute_secs"],
        bridge_coordinator::db::SESSION_ABSOLUTE_SECS_SHORT
    );
    // The advertised minimum is the one the server enforces.
    let min = bridge_coordinator::auth::MIN_PASSWORD_LEN;
    assert!(bridge_coordinator::auth::valid_password(&"x".repeat(min)).is_ok());
    assert!(bridge_coordinator::auth::valid_password(&"x".repeat(min - 1)).is_err());

    let api = env.admin().await;
    let (_, body) = api.get("/api/v1/auth/state").await;
    assert_eq!(body["authenticated"], true);
    assert_eq!(body["username"], "admin");
    assert!(body["session"]["expires_at"].as_i64().unwrap() > bridge_coordinator::now());
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn remember_device_selects_a_real_server_side_session_lifetime() {
    use bridge_coordinator::db::{SESSION_ABSOLUTE_SECS, SESSION_ABSOLUTE_SECS_SHORT, SESSION_IDLE_SECS, SESSION_IDLE_SECS_SHORT};
    let env = boot().await;
    let _ = env.admin().await;
    let now = bridge_coordinator::now();

    // Default and remember=false: browser-session cookie, short server lifetime.
    for flag in [None, Some(json!(false))] {
        let (api, cookie) = login_with(&env, flag).await;
        assert!(!cookie.contains("Max-Age"), "not remembered => session cookie: {cookie}");
        let (_, s) = api.get("/api/v1/auth/state").await;
        assert_eq!(s["session"]["remember"], false);
        assert_eq!(s["session"]["idle_timeout_secs"], SESSION_IDLE_SECS_SHORT);
        let expires = s["session"]["expires_at"].as_i64().unwrap();
        assert!((expires - now - SESSION_ABSOLUTE_SECS_SHORT).abs() < 30, "{expires}");
        // Idle for a little over the short idle limit: expired.
        let hash = bridge_coordinator::auth::sha256_hex(api.cookie.as_ref().unwrap());
        env.srv.state.db.session_age(&hash, now - SESSION_IDLE_SECS_SHORT - 5, now + 1000).unwrap();
        assert_eq!(api.get("/api/v1/workers").await.0, 401, "short idle limit");
    }

    // remember=true: persistent cookie, long lifetime, survives the idle time that ends a short session.
    let (api, cookie) = login_with(&env, Some(json!(true))).await;
    assert!(cookie.contains(&format!("Max-Age={SESSION_ABSOLUTE_SECS}")), "{cookie}");
    let (_, s) = api.get("/api/v1/auth/state").await;
    assert_eq!(s["session"]["remember"], true);
    assert_eq!(s["session"]["idle_timeout_secs"], SESSION_IDLE_SECS);
    let hash = bridge_coordinator::auth::sha256_hex(api.cookie.as_ref().unwrap());
    env.srv.state.db.session_age(&hash, now - SESSION_IDLE_SECS_SHORT - 5, now + 1000).unwrap();
    assert_eq!(api.get("/api/v1/workers").await.0, 200, "remembered device is still signed in");
    env.srv.state.db.session_age(&hash, now - SESSION_IDLE_SECS - 5, now + 1000).unwrap();
    assert_eq!(api.get("/api/v1/workers").await.0, 401, "long idle limit");

    // The absolute limit also differs.
    let (api, _) = login_with(&env, None).await;
    let hash = bridge_coordinator::auth::sha256_hex(api.cookie.as_ref().unwrap());
    env.srv.state.db.session_age(&hash, now, now - 1).unwrap();
    assert_eq!(api.get("/api/v1/workers").await.0, 401, "absolute limit");

    // A non-boolean flag is refused before any credential is checked.
    let anon = env.api();
    let (st, _) = anon
        .send(
            anon.bare(Method::POST, "/api/v1/auth/login")
                .json(&json!({"username": "admin", "password": PASSWORD, "remember": "yes"})),
        )
        .await;
    assert_eq!(st, 400);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn token_management_lists_metadata_and_shows_secret_only_at_creation() {
    let env = boot().await;
    let api = env.admin().await;
    let (status, created) = api
        .post("/api/v1/tokens", json!({"name":"automation","scopes":["read","operate"],"expires_days":30}))
        .await;
    assert_eq!(status, 201, "{created}");
    let secret = created["secret"].as_str().expect("one-time token secret");
    assert!(!secret.is_empty());
    assert_eq!(created["token"]["scopes"], json!(["read", "operate"]));
    assert!(created["token"]["created_at"].as_str().unwrap().ends_with('Z'));
    assert!(created["token"]["expires_at"].as_str().unwrap().ends_with('Z'));

    let (status, listed) = api.get("/api/v1/tokens").await;
    assert_eq!(status, 200);
    let token = listed["tokens"].as_array().unwrap().iter().find(|t| t["name"] == "automation").unwrap();
    assert_eq!(token["scopes"], json!(["read", "operate"]));
    assert!(token["last_used"].is_null());
    assert!(token.get("secret").is_none() && token.get("token_hash").is_none());

    let (status, revoked) = api.send(api.rb(Method::DELETE, "/api/v1/tokens/automation")).await;
    assert_eq!(status, 200, "{revoked}");
    assert!(!api.get("/api/v1/tokens").await.1["tokens"]
        .as_array()
        .unwrap()
        .iter()
        .any(|t| t["name"] == "automation"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn mission_task_resource_adapts_to_the_typed_worker_command() {
    let env = boot().await;
    let api = env.admin().await;
    let worker_secret = "worker-secret-for-resource-test";
    env.srv
        .state
        .db
        .worker_add("build-1", &bridge_coordinator::auth::sha256_hex(worker_secret))
        .unwrap();
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([{"id":"mission-1","name":"Example"}]));
    env.srv
        .state
        .db
        .worker_hello(
            "build-1",
            "worker-id-resource-test",
            &hello,
            &json!([{"operation":"task.list","mutating":false}]),
        )
        .unwrap();

    let (status, command) = api.get("/api/v1/missions/mission-1/tasks?worker=build-1&limit=25&offset=5").await;
    assert_eq!(status, 202, "{command}");
    assert_eq!(command["operation"], "task.list");
    assert_eq!(command["request"]["params"], json!({"limit":25,"offset":5}));
    assert_eq!(command["request"]["mission_id"], "mission-1");
    assert_eq!(command["target"], "build-1");
    assert!(command["request_id"].as_str().unwrap().starts_with("resource-"));

    let (status, problem) = api.get("/api/v1/missions/mission-1/tasks?worker=build-1&admin=true").await;
    assert_eq!(status, 400, "{problem}");
    assert_eq!(problem["type"], "https://llm-tools.dev/problems/invalid-request");
    env.stop().await;
}

#[test]
fn migration_to_version_3_keeps_existing_sessions_on_the_original_policy() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("v2.sqlite3");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch(
            "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, created_at INTEGER NOT NULL,
               last_seen INTEGER NOT NULL, expires_at INTEGER NOT NULL);
             PRAGMA user_version = 2;",
        )
        .unwrap();
        let now = bridge_coordinator::now();
        c.execute(
            "INSERT INTO sessions VALUES('old', 'csrf', ?1, ?2, ?3)",
            rusqlite::params![now, now - 3 * 3600, now + 86_400],
        )
        .unwrap();
    }
    let db = bridge_coordinator::db::Db::open(&path).unwrap();
    let s = db
        .session_get("old")
        .unwrap()
        .expect("a session idle for 3 h is valid under the original 8 h limit");
    assert!(s.remember);
    assert_eq!(s.idle_secs, bridge_coordinator::db::SESSION_IDLE_SECS);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn missions_can_be_searched_filtered_sorted_and_paged() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let ws = Fw::raw(&env, &token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: "wid".into(),
        generation: 0,
        seq: 0,
    };
    let mission = |id: &str, name: &str, status: &str, folder: &str, updated: &str| json!({"id": id, "name": name, "status": status, "folder": folder, "updated_at": updated});
    fw.send(
        "hello",
        json!({"worker_id": "wid", "name": "w1", "software_version": "test", "protocol_versions": [1],
               "platform": {"system": "Linux"}, "capabilities": caps(), "event_cursor": 0, "missions": [
            mission("m1", "Refactor parser", "RUNNING", "parser", "2026-01-03T10:00:00Z"),
            mission("m2", "Fix login bug", "FAILED", "web", "2026-01-01T10:00:00Z"),
            mission("m3", "Write docs", "COMPLETED", "docs", "2026-01-02T10:00:00Z"),
            mission("m4", "refactor docs", "RUNNING", "docs", "2026-01-04T10:00:00Z"),
        ]}),
    )
    .await;
    assert_eq!(fw.recv().await["type"], "welcome");
    let ids = |b: &Value| {
        b["missions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["mission"]["id"].as_str().unwrap().to_string())
            .collect::<Vec<_>>()
    };

    let (st, all) = api.get("/api/v1/missions").await;
    assert_eq!(st, 200);
    assert_eq!(all["total"], 4, "no limit returns every mission");
    assert_eq!(ids(&all).len(), 4);
    assert_eq!(all["statuses"], json!({"COMPLETED": 1, "FAILED": 1, "RUNNING": 2}));

    let (_, b) = api.get("/api/v1/missions?q=REFACTOR&sort=name").await;
    assert_eq!(ids(&b), ["m4", "m1"] as [&str; 2], "case-insensitive search over name, sorted by name");
    assert_eq!(b["statuses"], json!({"RUNNING": 2}), "facets ignore the status filter but honour the search");
    let (_, b) = api.get("/api/v1/missions?q=docs").await;
    assert_eq!(b["total"], 2, "the folder is searched too");
    let (_, b) = api.get("/api/v1/missions?status=failed,completed&sort=updated&order=desc").await;
    assert_eq!(ids(&b), ["m3", "m2"] as [&str; 2]);
    let (_, b) = api.get("/api/v1/missions?sort=updated&order=desc&limit=2&offset=1").await;
    assert_eq!(ids(&b), ["m1", "m3"] as [&str; 2]);
    assert_eq!(b["total"], 4, "total counts matches before paging");
    assert_eq!(b["offset"], 1);
    let (_, b) = api.get("/api/v1/missions?worker=nobody").await;
    assert_eq!(b["total"], 0);
    let (_, b) = api.get("/api/v1/missions?limit=0").await;
    assert_eq!(ids(&b).len(), 1, "limit is clamped to at least 1");

    for bad in ["sort=colour", "order=sideways", "limit=-1", "offset=x"] {
        let (st, e) = api.get(&format!("/api/v1/missions?{bad}")).await;
        assert_eq!(st, 400, "{bad}: {e}");
    }
    env.stop().await;
}
