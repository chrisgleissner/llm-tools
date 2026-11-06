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
async fn mutation_bodies_require_json_content_type() {
    let env = boot().await;
    let api = env.admin().await;
    let (status, body) = api
        .send(
            api.rb(Method::POST, "/api/v1/tokens")
                .header("content-type", "text/plain")
                .body(r#"{"name":"automation","scopes":["read"]}"#),
        )
        .await;
    assert_eq!(status, 415, "{body}");
    assert_eq!(body["type"], "https://llm-tools.dev/problems/unsupported-media-type");
    env.stop().await;
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
async fn bearer_mutations_are_rate_limited_and_audited_without_logging_the_secret() {
    let env = boot().await;
    let admin = env.admin().await;
    let (status, created) = admin
        .post("/api/v1/tokens", json!({"name":"mutator","scopes":["admin"],"expires_days":30}))
        .await;
    assert_eq!(status, 201, "{created}");
    let secret = created["secret"].as_str().unwrap().to_string();
    let mut token_api = env.api();
    token_api.bearer = Some(secret.clone());

    for _ in 0..30 {
        let (status, _) = token_api.post("/api/v1/commands", json!({})).await;
        assert_eq!(status, 400, "malformed request should reach command validation");
    }
    let response = token_api.rb(Method::POST, "/api/v1/commands").json(&json!({})).send().await.unwrap();
    assert_eq!(response.status().as_u16(), 429);
    assert_eq!(response.headers().get("retry-after").unwrap(), "60");
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["type"], "https://llm-tools.dev/problems/mutation-rate-limit");

    let audit = env.srv.state.db.audit_list(100).unwrap();
    let token_uses: Vec<_> = audit.iter().filter(|row| row["action"] == "token.use").collect();
    assert_eq!(token_uses.len(), 31);
    assert!(token_uses.iter().all(|row| row["actor"] == "token:mutator"));
    assert!(!serde_json::to_string(&token_uses).unwrap().contains(&secret));
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
    hello.insert("host_name".into(), json!("api-test-host"));
    env.srv
        .state
        .db
        .worker_hello(
            "build-1",
            "worker-id-resource-test",
            &hello,
            &json!([{"operation":"task.list","mutating":false},{"operation":"mission.status","mutating":false}]),
            "203.0.113.7",
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
    let (status, status_command) = api.get("/api/v1/missions/mission-1?worker=build-1").await;
    assert_eq!(status, 202, "{status_command}");
    assert_eq!(status_command["operation"], "mission.status");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn mission_and_task_mutations_adapt_to_typed_worker_commands() {
    let env = boot().await;
    let api = env.admin().await;
    let worker_secret = "worker-secret-for-mutation-test";
    env.srv
        .state
        .db
        .worker_add("build-1", &bridge_coordinator::auth::sha256_hex(worker_secret))
        .unwrap();
    let operations = [
        "mission.start",
        "mission.resume",
        "mission.pause",
        "mission.stop",
        "mission.cancel",
        "mission.continue",
        "task.add",
        "task.amend",
        "task.priority",
        "task.reorder",
        "task.dependency",
        "task.verify",
        "task.reject",
        "task.pause",
        "task.resume",
        "task.cancel",
        "task.retry",
    ];
    let capabilities: Vec<Value> = operations.iter().map(|operation| json!({"operation":operation,"mutating":true})).collect();
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([{"id":"mission-1","name":"Example"}]));
    env.srv
        .state
        .db
        .worker_hello("build-1", "worker-id-mutation-test", &hello, &json!(capabilities), "203.0.113.7")
        .unwrap();

    for (path, body, expected_operation) in [
        (
            "/api/v1/missions/mission-1/start?worker=build-1",
            json!({"headless":true,"providers":"codex"}),
            "mission.start",
        ),
        ("/api/v1/missions/mission-1/resume?worker=build-1", json!({}), "mission.resume"),
        ("/api/v1/missions/mission-1/pause?worker=build-1", json!({}), "mission.pause"),
        ("/api/v1/missions/mission-1/stop?worker=build-1", json!({"force":true}), "mission.stop"),
        (
            "/api/v1/missions/mission-1/tasks?worker=build-1",
            json!({"title":"A task","prompt":"Do the work","priority":3,"depends_on":[],"acceptance":[]}),
            "task.add",
        ),
        (
            "/api/v1/missions/mission-1/tasks/7/verify?worker=build-1",
            json!({"approved":true,"evidence":"Reviewed output","expected_revision":3}),
            "task.verify",
        ),
        (
            "/api/v1/missions/mission-1/tasks/8/verify?worker=build-1",
            json!({"approved":false,"reason":"Missing evidence","expected_revision":1}),
            "task.reject",
        ),
        (
            "/api/v1/missions/mission-1/tasks/9/retry?worker=build-1",
            json!({"expected_revision":2}),
            "task.retry",
        ),
    ] {
        let (status, command) = api.post(path, body).await;
        assert_eq!(status, 202, "{path}: {command}");
        assert_eq!(command["operation"], expected_operation, "{path}: {command}");
        assert_eq!(command["target"], "build-1");
        assert_eq!(command["request"]["mission_id"], "mission-1");
        if path.ends_with("/stop?worker=build-1") {
            assert_eq!(command["request"]["params"], json!({"force":true}));
        }
    }
    let (status, command) = api
        .send(
            api.rb(Method::PATCH, "/api/v1/missions/mission-1/tasks/7?worker=build-1")
                .header("if-match", "\"2\"")
                .json(&json!({"prompt":"Revised work","expected_revision":2})),
        )
        .await;
    assert_eq!(status, 202, "{command}");
    assert_eq!(command["operation"], "task.amend");
    assert_eq!(command["request"]["params"]["task_number"], 7);
    assert_eq!(command["request"]["params"]["expected_revision"], 2);
    let (status, mismatch) = api
        .send(
            api.rb(Method::PATCH, "/api/v1/missions/mission-1/tasks/7?worker=build-1")
                .header("if-match", "\"3\"")
                .json(&json!({"prompt":"Revised work","expected_revision":2})),
        )
        .await;
    assert_eq!(status, 400, "{mismatch}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn task_revision_etag_and_if_match_enforce_preconditions() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut worker, _) = Fw::connect(&env, &token, "etag-worker", 0).await;
    let read_api = Api {
        c: api.c.clone(),
        base: api.base.clone(),
        cookie: api.cookie.clone(),
        csrf: api.csrf.clone(),
        bearer: None,
    };
    let get = tokio::spawn(async move {
        read_api
            .rb(Method::GET, "/api/v1/missions/m1/tasks/2?worker=w1&wait_ms=5000")
            .send()
            .await
            .unwrap()
    });
    let command = worker.recv().await;
    let request_id = command["payload"]["request_id"].as_str().unwrap();
    assert_eq!(command["payload"]["operation"], "task.show");
    worker
        .send(
            "command.result",
            json!({"schema_version":1,"request_id":request_id,"operation":"task.show","outcome":"ok","result":{"id":12,"ordinal":2,"revision":7},"error":null}),
        )
        .await;
    let response = get.await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    assert_eq!(response.headers().get("etag").unwrap(), "\"7\"");
    let patch_api = Api {
        c: api.c.clone(),
        base: api.base.clone(),
        cookie: api.cookie.clone(),
        csrf: api.csrf.clone(),
        bearer: None,
    };
    let patch = tokio::spawn(async move {
        patch_api
            .rb(Method::PATCH, "/api/v1/missions/m1/tasks/2?worker=w1")
            .header("if-match", "\"6\"")
            .json(&json!({"prompt":"new prompt","wait_ms":5000}))
            .send()
            .await
            .unwrap()
    });
    let pending = worker.recv().await;
    worker
        .send(
            "command.result",
            json!({"schema_version":1,"request_id":pending["payload"]["request_id"],"operation":"task.amend","outcome":"rejected","result":null,"error":{"code":"conflict","message":"stale revision"}}),
        )
        .await;
    let response = patch.await.unwrap();
    assert_eq!(response.status().as_u16(), 412);
    let problem: Value = response.json().await.unwrap();
    assert_eq!(problem["type"], "https://llm-tools.dev/problems/stale-revision");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn mission_task_cursor_is_bound_and_returns_an_opaque_next_page() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut worker, _) = Fw::connect(&env, &token, "cursor-worker", 0).await;

    let filters = "worker=w1;mission=m1;resource=tasks;limit=1";
    let cursor = bridge_coordinator::cursor::issue(&env.srv.state.cursor_key, "missions/tasks", filters, 3);
    let read_api = Api {
        c: api.c.clone(),
        base: api.base.clone(),
        cookie: api.cookie.clone(),
        csrf: api.csrf.clone(),
        bearer: None,
    };
    let get = tokio::spawn(async move {
        read_api
            .rb(
                Method::GET,
                &format!("/api/v1/missions/m1/tasks?worker=w1&limit=1&cursor={cursor}&wait_ms=5000"),
            )
            .send()
            .await
            .unwrap()
    });
    let command = worker.recv().await;
    assert_eq!(command["payload"]["operation"], "task.list");
    assert_eq!(command["payload"]["params"]["offset"], 3);
    assert_eq!(command["payload"]["params"]["limit"], 1);
    let request_id = command["payload"]["request_id"].as_str().unwrap();
    worker
        .send(
            "command.result",
            json!({"schema_version":1,"request_id":request_id,"operation":"task.list","outcome":"ok","result":{"tasks":[{"ordinal":4}],"total":5,"offset":3},"error":null}),
        )
        .await;
    let response = get.await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    let body: Value = response.json().await.unwrap();
    let next = body["result"]["result"]["next_cursor"].as_str().unwrap();
    assert_eq!(
        bridge_coordinator::cursor::read(&env.srv.state.cursor_key, "missions/tasks", filters, next),
        Ok(4),
        "continuation advances by the number of returned rows"
    );

    let (bad_status, bad) = api.get("/api/v1/missions/m1/tasks?worker=w1&limit=1&cursor=not-a-cursor").await;
    assert_eq!(bad_status, 400, "{bad}");
    assert_eq!(bad["code"], "invalid-cursor");
    let (mismatch_status, mismatch) = api.get(&format!("/api/v1/missions/m1/tasks?worker=w1&limit=2&cursor={next}")).await;
    assert_eq!(mismatch_status, 400, "{mismatch}");
    assert_eq!(mismatch["code"], "invalid-cursor");

    let operation_filters = "worker=w1;mission=m1;resource=operations;limit=1";
    let operation_cursor = bridge_coordinator::cursor::issue(&env.srv.state.cursor_key, "missions/operations", operation_filters, 0);
    let operation_api = Api {
        c: api.c.clone(),
        base: api.base.clone(),
        cookie: api.cookie.clone(),
        csrf: api.csrf.clone(),
        bearer: None,
    };
    let operations = tokio::spawn(async move {
        operation_api
            .rb(
                Method::GET,
                &format!("/api/v1/missions/m1/operations?worker=w1&limit=1&cursor={operation_cursor}&wait_ms=5000"),
            )
            .send()
            .await
            .unwrap()
    });
    let command = worker.recv().await;
    assert_eq!(command["payload"]["operation"], "session.list");
    assert_eq!(command["payload"]["params"]["mission"], "m1");
    assert_eq!(command["payload"]["params"]["offset"], 0);
    worker
        .send(
            "command.result",
            json!({"schema_version":1,"request_id":command["payload"]["request_id"],"operation":"session.list","outcome":"ok","result":{"sessions":[{"operation_id":"op-1"}],"total":2,"offset":0},"error":null}),
        )
        .await;
    let response = operations.await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    let body: Value = response.json().await.unwrap();
    let next = body["result"]["result"]["next_cursor"].as_str().unwrap();
    assert_eq!(
        bridge_coordinator::cursor::read(&env.srv.state.cursor_key, "missions/operations", operation_filters, next),
        Ok(1)
    );

    let event_filters = "worker=w1;mission=m1;resource=events;limit=2";
    let event_cursor = bridge_coordinator::cursor::issue(&env.srv.state.cursor_key, "missions/events", event_filters, 4);
    let event_api = Api {
        c: api.c.clone(),
        base: api.base.clone(),
        cookie: api.cookie.clone(),
        csrf: api.csrf.clone(),
        bearer: None,
    };
    let events = tokio::spawn(async move {
        event_api
            .rb(
                Method::GET,
                &format!("/api/v1/missions/m1/events?worker=w1&limit=2&cursor={event_cursor}&wait_ms=5000"),
            )
            .send()
            .await
            .unwrap()
    });
    let command = worker.recv().await;
    assert_eq!(command["payload"]["operation"], "mission.events");
    assert_eq!(command["payload"]["params"]["after"], 4);
    worker
        .send(
            "command.result",
            json!({"schema_version":1,"request_id":command["payload"]["request_id"],"operation":"mission.events","outcome":"ok","result":{"events":[{"id":5},{"id":6}]},"error":null}),
        )
        .await;
    let response = events.await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    let body: Value = response.json().await.unwrap();
    let next = body["result"]["result"]["next_cursor"].as_str().unwrap();
    assert_eq!(
        bridge_coordinator::cursor::read(&env.srv.state.cursor_key, "missions/events", event_filters, next),
        Ok(6)
    );
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
    let mut first_mission = mission("m1", "Refactor parser", "RUNNING", "parser", "2026-01-03T10:00:00Z");
    first_mission["needs_you"] =
        json!({"kind":"provider","message":"Codex asks: Reconnect? Choices: 1. Yes; 2. No","command":"llm-bridge attach --mission m1"});
    fw.send(
        "hello",
        json!({"worker_id": "wid", "name": "w1", "software_version": "test", "protocol_versions": [1],
               "platform": {"system": "Linux"}, "capabilities": caps(), "event_cursor": 0, "missions": [
            first_mission,
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
    assert_eq!(all["total"], 4, "default page includes every matching fixture mission");
    assert_eq!(ids(&all).len(), 4);
    assert_eq!(
        all["missions"][0]["mission"]["needs_you"]["message"],
        "Codex asks: Reconnect? Choices: 1. Yes; 2. No"
    );
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

    let (_, page_one) = api.get("/api/v1/missions?limit=2").await;
    assert_eq!(ids(&page_one), ["m1", "m2"] as [&str; 2]);
    let next = page_one["next_cursor"].as_str().expect("next page cursor").to_string();
    let (_, page_two) = api.get(&format!("/api/v1/missions?limit=2&cursor={next}")).await;
    assert_eq!(ids(&page_two), ["m3", "m4"] as [&str; 2]);
    assert!(page_two["next_cursor"].is_null());
    let (invalid_status, invalid) = api.get(&format!("/api/v1/missions?limit=2&sort=name&cursor={next}")).await;
    assert_eq!(invalid_status, 400);
    assert_eq!(invalid["code"], "invalid-cursor");
    let mut forged = next.into_bytes();
    forged[0] = if forged[0] == b'A' { b'B' } else { b'A' };
    let forged = String::from_utf8(forged).unwrap();
    let (forged_status, forged_problem) = api.get(&format!("/api/v1/missions?limit=2&cursor={forged}")).await;
    assert_eq!(forged_status, 400);
    assert_eq!(forged_problem["code"], "invalid-cursor");
    let (conflict_status, conflict) = api.get("/api/v1/missions?cursor=bogus&offset=0").await;
    assert_eq!(conflict_status, 400);
    assert_eq!(conflict["code"], "invalid-request");

    for bad in ["sort=colour", "order=sideways", "limit=-1", "offset=x"] {
        let (st, e) = api.get(&format!("/api/v1/missions?{bad}")).await;
        assert_eq!(st, 400, "{bad}: {e}");
    }
    env.stop().await;
}
