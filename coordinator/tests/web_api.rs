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
async fn malformed_numeric_path_parameters_use_problem_json() {
    let env = boot().await;
    let api = env.admin().await;
    let response = api.rb(Method::GET, "/api/v1/missions/m1/tasks/not-a-number?worker=w1").send().await.unwrap();
    assert_eq!(response.status().as_u16(), 400);
    assert_eq!(response.headers()["content-type"], "application/problem+json");
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["code"], "invalid-request");
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
    assert_eq!(body["passkey_sign_in_available"], false);
    assert!(body["passkey_canonical_origin"].is_null());
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
async fn passkey_sign_in_is_advertised_only_on_the_bound_canonical_origin() {
    let env = boot_with(|t| {
        t.replace(
            "allowed_origins = []",
            "allowed_origins = [\"https://bridge.example.test\"]\npublic_origin = \"https://bridge.example.test\"",
        )
    })
    .await;
    let _ = env.admin().await;
    let db = bridge_coordinator::db::Db::open(&bridge_coordinator::db_path(&env.dir)).unwrap();
    db.passkey_put_at_origin(
        &bridge_coordinator::db::PasskeyCredentialRow {
            credential_id: "credential".into(),
            principal_id: "admin".into(),
            credential: "{}".into(),
            user_name: "Phone".into(),
            created_at: bridge_coordinator::now(),
            last_used: None,
            aaguid: None,
            transports: "[]".into(),
            backup_eligible: false,
            backed_up: false,
            sign_count: 0,
        },
        "https://bridge.example.test",
    )
    .unwrap();
    drop(db);

    let api = env.api();
    let (status, body) = api.get("/api/v1/auth/state").await;
    assert_eq!(status, 200);
    assert_eq!(body["passkey_sign_in_available"], false, "IP origin must not advertise passkeys");
    let response = api
        .rb(Method::GET, "/api/v1/auth/state")
        .header("origin", "https://bridge.example.test")
        .send()
        .await
        .unwrap();
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["passkey_sign_in_available"], true);
    assert_eq!(body["passkey_canonical_origin"], "https://bridge.example.test");
    let response = api
        .rb(Method::GET, "/api/v1/auth/state")
        .header("origin", "https://other.example.test")
        .send()
        .await
        .unwrap();
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["passkey_sign_in_available"], false);
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
    let mut token_parts = secret.split('_');
    assert_eq!(token_parts.next(), Some("lbr"));
    assert_eq!(token_parts.next(), Some("pat"));
    assert_eq!(
        token_parts.next().map(str::len),
        Some(16),
        "token id is visible for scanning and identification"
    );
    assert_eq!(token_parts.next().map(str::len), Some(64), "token secret encodes 256 random bits");
    assert!(token_parts.next().is_none());
    assert_eq!(created["token"]["scopes"], json!(["read", "operate"]));
    assert!(created["token"]["created_at"].as_str().unwrap().ends_with('Z'));
    assert!(created["token"]["expires_at"].as_str().unwrap().ends_with('Z'));
    let mut bearer = env.api();
    bearer.bearer = Some(secret.to_string());
    assert_eq!(bearer.get("/api/v1/missions").await.0, 200, "prefixed bearer token authenticates");
    let mut wrong_secret = secret.to_string();
    let last_secret_char = wrong_secret.chars().last().unwrap();
    wrong_secret.pop();
    wrong_secret.push(if last_secret_char == '0' { '1' } else { '0' });
    let mut rejected = env.api();
    rejected.bearer = Some(wrong_secret);
    assert_eq!(rejected.get("/api/v1/missions").await.0, 401, "wrong token secret is rejected");
    let legacy_secret = bridge_coordinator::auth::random_token();
    assert!(env
        .srv
        .state
        .db
        .token_create_scoped(
            "legacy-token",
            &bridge_coordinator::auth::sha256_hex(&legacy_secret),
            None,
            &["read".to_string()],
        )
        .unwrap());
    let mut legacy = env.api();
    legacy.bearer = Some(legacy_secret);
    assert_eq!(legacy.get("/api/v1/missions").await.0, 200, "old unprefixed bearer tokens remain accepted");

    let (status, listed) = api.get("/api/v1/tokens").await;
    assert_eq!(status, 200);
    let token = listed["tokens"].as_array().unwrap().iter().find(|t| t["name"] == "automation").unwrap();
    assert_eq!(token["id"], created["token"]["id"]);
    assert_eq!(token["scopes"], json!(["read", "operate"]));
    assert!(token["last_used"].as_str().is_some_and(|value| value.ends_with('Z')));
    assert!(token.get("secret").is_none() && token.get("token_hash").is_none());
    let legacy = listed["tokens"].as_array().unwrap().iter().find(|t| t["name"] == "legacy-token").unwrap();
    assert!(legacy["id"].is_null(), "legacy token has no public token ID");

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
async fn token_expiry_respects_the_configured_maximum_but_allows_lan_nonexpiry() {
    let env = boot_with(|t| t.replace("max_token_lifetime_days = 3650", "max_token_lifetime_days = 1")).await;
    let api = env.admin().await;
    let (status, body) = api.post("/api/v1/tokens", json!({"name":"too-long","scopes":["read"],"expires_days":2})).await;
    assert_eq!(status, 400, "{body}");
    assert_eq!(body["code"], "invalid-request");

    let (status, body) = api.post("/api/v1/tokens", json!({"name":"one-day","scopes":["read"],"expires_days":1})).await;
    assert_eq!(status, 201, "{body}");
    assert!(body["token"]["expires_at"].as_str().is_some());

    let (status, body) = api
        .post("/api/v1/tokens", json!({"name":"local-forever","scopes":["read"],"expires_days":0}))
        .await;
    assert_eq!(status, 201, "{body}");
    assert!(body["token"]["expires_at"].is_null());
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn passkey_registration_start_is_bound_to_https_named_origin_and_session() {
    let origin = "https://bridge.example.test";
    let env = boot_with(|t| t.replace("allowed_origins = []", "allowed_origins = [\"https://bridge.example.test\"]")).await;
    let api = env.admin().await;
    let (status, options) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/passkeys/registration/start")
                .header("cookie", format!("__Host-bridge_session={}", api.cookie.as_deref().unwrap()))
                .header("x-csrf-token", api.csrf.as_deref().unwrap())
                .header("origin", origin)
                .json(&json!({"name":"laptop"})),
        )
        .await;
    assert_eq!(status, 200, "{options}");
    assert_eq!(options["options"]["publicKey"]["rp"]["id"], "bridge.example.test");
    assert_eq!(options["options"]["publicKey"]["attestation"], "none");
    assert_eq!(options["options"]["publicKey"]["authenticatorSelection"]["userVerification"], "required");
    assert_eq!(options["options"]["publicKey"]["authenticatorSelection"]["residentKey"], "required");
    let challenge_id = options["challenge_id"].as_str().unwrap();
    assert_eq!(challenge_id.len(), 64);

    // A malformed but structurally valid assertion reaches verification and consumes the
    // single-use ceremony. Replaying the same challenge is rejected as gone.
    let invalid_credential = json!({"id":"AQ","rawId":"AQ","type":"public-key","response":{"attestationObject":"AQ","clientDataJSON":"AQ"}});
    let finish = || {
        api.bare(Method::POST, "/api/v1/auth/passkeys/registration/finish")
            .header("cookie", format!("__Host-bridge_session={}", api.cookie.as_deref().unwrap()))
            .header("x-csrf-token", api.csrf.as_deref().unwrap())
            .header("origin", origin)
            .json(&json!({"challenge_id":challenge_id,"credential":invalid_credential}))
    };
    let (status, _) = api.send(finish()).await;
    assert_eq!(status, 400);
    let (status, body) = api.send(finish()).await;
    assert_eq!(status, 410, "challenge replay must fail: {body}");

    let (status, _) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/passkeys/registration/start")
                .header("cookie", format!("__Host-bridge_session={}", api.cookie.as_deref().unwrap()))
                .header("x-csrf-token", api.csrf.as_deref().unwrap())
                .header("origin", "https://attacker.example.test")
                .json(&json!({"name":"attacker"})),
        )
        .await;
    assert_eq!(status, 403, "an unapproved origin cannot start registration");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn passkey_challenge_creation_uses_the_per_address_mutation_budget() {
    let origin = "https://bridge.example.test";
    let env = boot_with(|t| {
        t.replace("allowed_origins = []", "allowed_origins = [\"https://bridge.example.test\"]")
            .replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 1")
    })
    .await;
    let api = env.admin().await;
    let registration_start = || {
        api.bare(Method::POST, "/api/v1/auth/passkeys/registration/start")
            .header("cookie", format!("__Host-bridge_session={}", api.cookie.as_deref().unwrap()))
            .header("x-csrf-token", api.csrf.as_deref().unwrap())
            .header("origin", origin)
            .json(&json!({"name":"laptop"}))
    };

    let first = registration_start().send().await.unwrap();
    assert_eq!(first.status().as_u16(), 200);
    let limited = registration_start().send().await.unwrap();
    assert_eq!(limited.status().as_u16(), 429);
    assert_eq!(limited.headers()["content-type"], "application/problem+json");
    assert!((1..=60).contains(&limited.headers()["retry-after"].to_str().unwrap().parse::<u64>().unwrap()));
    let body: Value = limited.json().await.unwrap();
    assert_eq!(body["type"], "https://llm-tools.dev/problems/mutation-rate-limit");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn browser_session_mutations_share_the_per_address_budget() {
    let env = boot_with(|t| t.replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 1")).await;
    let api = env.admin().await;
    let request = || api.rb(Method::POST, "/api/v1/commands").json(&json!({}));

    let first = request().send().await.unwrap();
    assert_eq!(first.status().as_u16(), 400, "invalid command must reach handler validation");
    let limited = request().send().await.unwrap();
    assert_eq!(limited.status().as_u16(), 429);
    assert_eq!(limited.headers()["content-type"], "application/problem+json");
    assert!((1..=60).contains(&limited.headers()["retry-after"].to_str().unwrap().parse::<u64>().unwrap()));
    let body: Value = limited.json().await.unwrap();
    assert_eq!(body["code"], "mutation-rate-limit");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn passkey_routes_stay_unavailable_on_ip_origins_and_unknown_credentials_fail_closed() {
    let env = boot().await;
    let api = env.admin().await;
    let (status, body) = api.post("/api/v1/auth/passkeys/registration/start", json!({"name":"phone"})).await;
    assert_eq!(status, 501, "IP origins cannot be WebAuthn RP IDs: {body}");

    let origin = "https://bridge.example.test";
    let env = {
        env.stop().await;
        boot_with(|t| t.replace("allowed_origins = []", "allowed_origins = [\"https://bridge.example.test\"]")).await
    };
    let api = env.admin().await;
    let (status, body) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/passkeys/assertion/start")
                .header("origin", origin)
                .json(&json!({})),
        )
        .await;
    assert_eq!(status, 404, "an account without credentials cannot start assertion: {body}");
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
    let retry_after = response.headers().get("retry-after").unwrap().to_str().unwrap().parse::<u64>().unwrap();
    assert!(
        (1..=60).contains(&retry_after),
        "Retry-After must be positive and no longer than the configured 60-second window: {retry_after}"
    );
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["type"], "https://llm-tools.dev/problems/mutation-rate-limit");

    let (status, _) = token_api.get("/api/v1/workers").await;
    assert_eq!(status, 200, "read requests still succeed after the mutation budget is exhausted");

    let audit = env.srv.state.db.audit_list(100).unwrap();
    let token_uses: Vec<_> = audit.iter().filter(|row| row["action"] == "token.use").collect();
    assert_eq!(token_uses.iter().filter(|row| row["actor"] == "token:mutator").count(), 32);
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
async fn api016_create_suspend_restore_resources_and_worker_policy_are_exposed() {
    let env = boot().await;
    let api = env.admin().await;
    env.srv
        .state
        .db
        .worker_add("build-1", &bridge_coordinator::auth::sha256_hex("worker-secret-api016"))
        .unwrap();
    let policy = json!({"allow_control":true,"allow_terminal_view":true,"allow_create":true,
        "allow_root":["/srv/work"],"allow_power":false,"allow_restore":true});
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([{"id":"mission-1","name":"Example"}]));
    hello.insert("worker_policy".into(), policy.clone());
    let capabilities = json!([
        {"operation":"mission.create","mutating":true},
        {"operation":"mission.suspend_until_credits","mutating":true},
        {"operation":"worker.restore","mutating":true}
    ]);
    env.srv
        .state
        .db
        .worker_hello("build-1", "worker-id-api016", &hello, &capabilities, "203.0.113.8")
        .unwrap();

    let (status, workers) = api.get("/api/v1/workers").await;
    assert_eq!(status, 200);
    assert_eq!(workers["workers"][0]["policy"]["allow_root_count"], 1);
    assert_eq!(workers["workers"][0]["policy"]["allow_create"], true);
    assert!(!serde_json::to_string(&workers).unwrap().contains("/srv/work"));
    let (status, worker) = api.get("/api/v1/workers/build-1").await;
    assert_eq!(status, 200);
    assert_eq!(worker["policy"]["allow_root_count"], 1);
    assert_eq!(worker["policy"]["allow_create"], true);
    assert_eq!(worker["policy"]["allow_restore"], true);
    assert!(!serde_json::to_string(&worker).unwrap().contains("/srv/work"));

    let (status, created) = api
        .post("/api/v1/missions", json!({"worker":"build-1","prompt":"new","cwd":"/srv/work","wait_ms":0}))
        .await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["operation"], "mission.create");
    assert_eq!(created["request"]["mission_id"], Value::Null);

    for option in ["include_config", "command_template", "verifier_argv", "verifier_file"] {
        let mut body = json!({"worker":"build-1","prompt":"new","cwd":"/srv/work"});
        body[option] = json!([]);
        let (status, problem) = api.post("/api/v1/missions", body).await;
        assert_eq!(status, 422, "{option}: {problem}");
        assert_eq!(problem["type"], "https://llm-tools.dev/problems/unsupported-remote-option");
    }

    let (status, suspended) = api
        .post("/api/v1/missions/mission-1/suspend-until-credits?worker=build-1", json!({"wait_ms":0}))
        .await;
    assert_eq!(status, 202, "{suspended}");
    assert_eq!(suspended["operation"], "mission.suspend_until_credits");

    let (status, restored) = api
        .post("/api/v1/workers/build-1/restore", json!({"input":"/srv/work/backup.db","wait_ms":0}))
        .await;
    assert_eq!(status, 202, "{restored}");
    assert_eq!(restored["operation"], "worker.restore");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn cli_parity_resource_adapters_queue_the_expected_typed_operations() {
    let env = boot().await;
    let api = env.admin().await;
    env.srv
        .state
        .db
        .worker_add("build-1", &bridge_coordinator::auth::sha256_hex("worker-secret-parity"))
        .unwrap();
    let operations = [
        "mission.settings.get",
        "mission.settings.patch",
        "mission.rotation.get",
        "mission.rotation.put",
        "mission.steer",
        "mission.finish",
        "mission.agents",
        "proposal.list",
        "proposal.create",
        "proposal.accept",
        "proposal.reject",
        "worker.doctor",
        "worker.audit",
        "worker.gc",
    ];
    let capabilities = operations.iter().map(|operation| json!({"operation":operation,"mutating":matches!(*operation,"mission.settings.patch"|"mission.rotation.put"|"mission.steer"|"mission.finish"|"proposal.create"|"proposal.accept"|"proposal.reject"|"worker.gc")})).collect::<Vec<_>>();
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([{"id":"mission-1","name":"Example"}]));
    env.srv
        .state
        .db
        .worker_hello("build-1", "worker-id-parity", &hello, &json!(capabilities), "203.0.113.8")
        .unwrap();
    let routes = [
        (
            Method::GET,
            "/api/v1/missions/mission-1/settings?worker=build-1",
            json!({}),
            "mission.settings.get",
        ),
        (
            Method::PATCH,
            "/api/v1/missions/mission-1/settings?worker=build-1",
            json!({"auto_continue":true}),
            "mission.settings.patch",
        ),
        (
            Method::PATCH,
            "/api/v1/missions/mission-1/settings?worker=build-1",
            json!({"colour":"auto"}),
            "mission.settings.patch",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/steer?worker=build-1",
            json!({"kind":"pause-after-iteration"}),
            "mission.steer",
        ),
        (Method::POST, "/api/v1/missions/mission-1/finish?worker=build-1", json!({}), "mission.finish"),
        (
            Method::GET,
            "/api/v1/missions/mission-1/rotation?worker=build-1",
            json!({}),
            "mission.rotation.get",
        ),
        (
            Method::PUT,
            "/api/v1/missions/mission-1/rotation?worker=build-1",
            json!({"providers":"codex"}),
            "mission.rotation.put",
        ),
        (Method::GET, "/api/v1/workers/build-1/agents", json!({}), "mission.agents"),
        (Method::GET, "/api/v1/missions/mission-1/proposals?worker=build-1", json!({}), "proposal.list"),
        (
            Method::POST,
            "/api/v1/missions/mission-1/proposals?worker=build-1",
            json!({"source_operation_id":"op-1","prompt":"task"}),
            "proposal.create",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/proposals/3/accept?worker=build-1",
            json!({}),
            "proposal.accept",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/proposals/3/reject?worker=build-1",
            json!({"reason":"no"}),
            "proposal.reject",
        ),
        (Method::GET, "/api/v1/workers/build-1/doctor", json!({}), "worker.doctor"),
        (Method::GET, "/api/v1/workers/build-1/audit", json!({}), "worker.audit"),
        (Method::POST, "/api/v1/workers/build-1/gc", json!({}), "worker.gc"),
    ];
    for (method, path, body, operation) in routes {
        let request = api.rb(method.clone(), path);
        let request = if matches!(method, Method::GET) { request } else { request.json(&body) };
        let (status, response) = api.send(request).await;
        assert_eq!(status, 202, "{method} {path}: {response}");
        assert_eq!(response["operation"], operation, "{method} {path}: {response}");
    }
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

    for (method, path, body, expected_operation) in [
        (
            Method::POST,
            "/api/v1/missions/mission-1/start?worker=build-1",
            json!({"headless":true,"providers":"codex"}),
            "mission.start",
        ),
        (Method::POST, "/api/v1/missions/mission-1/resume?worker=build-1", json!({}), "mission.resume"),
        (Method::POST, "/api/v1/missions/mission-1/pause?worker=build-1", json!({}), "mission.pause"),
        (
            Method::POST,
            "/api/v1/missions/mission-1/stop?worker=build-1",
            json!({"force":true}),
            "mission.stop",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/tasks?worker=build-1",
            json!({"title":"A task","prompt":"Do the work","priority":3,"depends_on":[1,3],"acceptance":[]}),
            "task.add",
        ),
        (
            Method::PATCH,
            "/api/v1/missions/mission-1/tasks/10?worker=build-1",
            json!({"depends_on":2}),
            "task.dependency",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/tasks/7/verify?worker=build-1",
            json!({"approved":true,"evidence":"Reviewed output","expected_revision":3}),
            "task.verify",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/tasks/8/verify?worker=build-1",
            json!({"approved":false,"reason":"Missing evidence","expected_revision":1}),
            "task.reject",
        ),
        (
            Method::POST,
            "/api/v1/missions/mission-1/tasks/9/retry?worker=build-1",
            json!({"expected_revision":2}),
            "task.retry",
        ),
    ] {
        let (status, command) = api.send(api.rb(method, path).json(&body)).await;
        assert_eq!(status, 202, "{path}: {command}");
        assert_eq!(command["operation"], expected_operation, "{path}: {command}");
        assert_eq!(command["target"], "build-1");
        assert_eq!(command["request"]["mission_id"], "mission-1");
        if path.ends_with("/stop?worker=build-1") {
            assert_eq!(command["request"]["params"], json!({"force":true}));
        }
        if path == "/api/v1/missions/mission-1/tasks?worker=build-1" {
            assert_eq!(command["request"]["params"]["depends_on_numbers"], json!([1, 3]));
        }
        if path == "/api/v1/missions/mission-1/tasks/10?worker=build-1" {
            assert_eq!(command["request"]["params"]["depends_on_number"], 2);
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
    worker.barrier().await;
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
    let (invalid_worker_status, invalid_worker) = api
        .get("/api/v1/missions?worker=0000000000000000000000000000000000000000000000000000000000000000")
        .await;
    assert_eq!(invalid_worker_status, 400);
    assert_eq!(invalid_worker["code"], "invalid-request");
    let (invalid_limit_status, invalid_limit) = api.get("/api/v1/missions?limit=0").await;
    assert_eq!(invalid_limit_status, 400);
    assert_eq!(invalid_limit["code"], "invalid-request");
    let (invalid_large_limit_status, invalid_large_limit) = api.get("/api/v1/missions?limit=501").await;
    assert_eq!(invalid_large_limit_status, 400);
    assert_eq!(invalid_large_limit["code"], "invalid-request");

    let (_, page_one) = api.get("/api/v1/missions?limit=2").await;
    assert_eq!(ids(&page_one), ["m1", "m2"] as [&str; 2]);
    let next = page_one["next_cursor"].as_str().expect("next page cursor").to_string();
    let (_, page_two) = api.get(&format!("/api/v1/missions?limit=2&cursor={next}")).await;
    assert_eq!(ids(&page_two), ["m3", "m4"] as [&str; 2]);
    assert!(page_two["next_cursor"].is_null());
    let (invalid_status, invalid) = api.get(&format!("/api/v1/missions?limit=2&sort=name&cursor={next}")).await;
    assert_eq!(invalid_status, 400);
    assert_eq!(invalid["code"], "invalid-cursor");
    let mut forged = next.clone().into_bytes();
    forged[0] = if forged[0] == b'A' { b'B' } else { b'A' };
    let forged = String::from_utf8(forged).unwrap();
    let (forged_status, forged_problem) = api.get(&format!("/api/v1/missions?limit=2&cursor={forged}")).await;
    assert_eq!(forged_status, 400);
    assert_eq!(forged_problem["code"], "invalid-cursor");
    let (_, combined_page) = api.get(&format!("/api/v1/missions?limit=2&cursor={next}&offset=0")).await;
    assert_eq!(
        ids(&combined_page),
        ["m3", "m4"] as [&str; 2],
        "opaque cursor takes precedence over the deprecated offset"
    );

    for bad in ["sort=colour", "order=sideways", "limit=-1", "offset=x"] {
        let (st, e) = api.get(&format!("/api/v1/missions?{bad}")).await;
        assert_eq!(st, 400, "{bad}: {e}");
    }
    let overlong_status = "x".repeat(257);
    let (st, e) = api.get(&format!("/api/v1/missions?status={overlong_status}")).await;
    assert_eq!(st, 400, "overlong status filter: {e}");
    env.stop().await;
}
