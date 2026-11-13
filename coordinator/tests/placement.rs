//! ORCH-008 and ORCH-009: remote mission creation options and worker placement.
mod common;
use bridge_coordinator::auth::sha256_hex;
use common::*;
use serde_json::{json, Value};

struct Fleet {
    env: Env,
    api: Api,
    _connections: Vec<tokio::sync::mpsc::Receiver<bridge_coordinator::hub::Outbound>>,
}

struct Spec<'a> {
    name: &'a str,
    online: bool,
    control: bool,
    /// Providers that the worker's latest usage snapshot reports as ready, or `None` for no snapshot.
    ready: Option<&'a [&'a str]>,
    running: usize,
    roots: &'a [&'a str],
    /// Extra keys of the stored usage snapshot, such as `routes`.
    extra: Value,
}

fn spec<'a>(name: &'a str, ready: Option<&'a [&'a str]>, running: usize) -> Spec<'a> {
    Spec {
        name,
        online: true,
        control: true,
        ready,
        running,
        roots: &["/srv/work"],
        extra: json!({}),
    }
}

async fn fleet(specs: Vec<Spec<'_>>) -> Fleet {
    // Placement waits at most this long for a usage snapshot that it had to fetch.
    static SHORT_WAIT: std::sync::Once = std::sync::Once::new();
    SHORT_WAIT.call_once(|| std::env::set_var("BRIDGE_PLACEMENT_USAGE_WAIT_MS", "2500"));
    let env = boot().await;
    let api = env.admin().await;
    let mut connections = Vec::new();
    for s in specs {
        env.srv.state.db.worker_add(s.name, &sha256_hex(&format!("secret-{}", s.name))).unwrap();
        let missions: Vec<Value> = (0..s.running)
            .map(|n| json!({"id": format!("{}-m{n}", s.name), "name": "m", "status": "RUNNING"}))
            .chain([json!({"id": format!("{}-done", s.name), "name": "old", "status": "COMPLETED"})])
            .collect();
        let mut hello = serde_json::Map::new();
        hello.insert("missions".into(), json!(missions));
        hello.insert(
            "worker_policy".into(),
            json!({"allow_control": s.control, "allow_terminal_view": false, "allow_create": true,
                   "allow_root": s.roots, "allow_power": false, "allow_restore": false}),
        );
        let capabilities = json!([
            {"operation":"mission.create","mutating":true},
            {"operation":"usage.snapshot","mutating":false}
        ]);
        env.srv
            .state
            .db
            .worker_hello(s.name, &format!("id-{}", s.name), &hello, &capabilities, "203.0.113.9")
            .unwrap();
        if let Some(ready) = s.ready {
            let providers: Vec<Value> = ["codex", "claude"]
                .iter()
                .map(|p| json!({"provider": p, "available": true, "ready": ready.contains(p), "scopes": []}))
                .collect();
            let id = format!("snapshot-{}", s.name);
            let request = json!({"schema_version": 1, "operation": "usage.snapshot", "params": {}, "mission_id": null,
                                 "target": s.name, "request_id": id});
            env.srv.state.db.command_insert(s.name, "usage.snapshot", false, &request).unwrap();
            env.srv
                .state
                .db
                .command_done(
                    s.name,
                    &id,
                    &json!({"outcome": "applied", "result": {"providers": providers, "routes": s.extra["routes"]}}),
                )
                .unwrap();
        }
        if s.online {
            connections.push(mark_online(&env, s.name, &format!("id-{}", s.name)));
        }
    }
    Fleet {
        env,
        api,
        _connections: connections,
    }
}

fn create_body(extra: Value) -> Value {
    let mut body = json!({"prompt": "new", "cwd": "/srv/work", "wait_ms": 0});
    for (key, value) in extra.as_object().unwrap() {
        body[key] = value.clone();
    }
    body
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_picks_a_worker_with_a_ready_requested_provider() {
    let f = fleet(vec![spec("alpha", Some(&["claude"]), 0), spec("beta", Some(&["codex"]), 3)]).await;
    let (status, created) = f.api.post("/api/v1/missions", create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["target"], "beta");
    assert_eq!(created["placement"]["worker"], "beta");
    let reason = created["placement"]["reason"].as_str().unwrap();
    assert!(reason.contains("codex ready") && reason.contains("3 active missions"), "{reason}");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_prefers_the_fewest_active_missions_and_breaks_ties_by_name() {
    let f = fleet(vec![
        spec("alpha", Some(&["codex"]), 2),
        spec("beta", Some(&["codex"]), 1),
        spec("gamma", Some(&["codex"]), 1),
    ])
    .await;
    let (status, created) = f.api.post("/api/v1/missions", create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(
        created["placement"]["worker"], "beta",
        "a finished mission does not count and the tie goes to the lower name"
    );
    assert!(created["placement"]["reason"].as_str().unwrap().contains("fewest of 3 eligible workers"));
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_named_worker_always_wins_over_placement() {
    let f = fleet(vec![spec("alpha", Some(&["claude"]), 5), spec("beta", Some(&["codex"]), 0)]).await;
    let (status, created) = f
        .api
        .post("/api/v1/missions", create_body(json!({"worker": "alpha", "providers": "codex"})))
        .await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["target"], "alpha");
    assert!(created.get("placement").is_none(), "{created}");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_without_an_eligible_worker_returns_409_with_a_reason_per_worker() {
    let mut offline = spec("delta", Some(&["codex"]), 0);
    offline.online = false;
    let mut no_control = spec("gamma", Some(&["codex"]), 0);
    no_control.control = false;
    let f = fleet(vec![spec("alpha", Some(&["claude"]), 0), spec("beta", None, 0), no_control, offline]).await;
    let (status, problem) = f.api.post("/api/v1/missions", create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 409, "{problem}");
    assert_eq!(problem["type"], "https://llm-tools.dev/problems/no-eligible-worker");
    let reasons = &problem["error"]["details"]["workers"];
    assert_eq!(reasons["alpha"], "none of codex is ready");
    assert_eq!(reasons["beta"], "no usage snapshot available");
    assert_eq!(reasons["gamma"], "does not allow control");
    assert_eq!(reasons["delta"], "offline");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_remote_verifier_command_or_file_is_refused_and_a_verifier_name_is_forwarded() {
    let f = fleet(vec![spec("alpha", Some(&["codex"]), 0)]).await;
    for option in [
        "command_template",
        "verifier",
        "verifier_command",
        "verifier_argv",
        "verifier_arg",
        "verifier_file",
        "verifier_files",
    ] {
        let (status, problem) = f.api.post("/api/v1/missions", create_body(json!({"worker": "alpha", option: "x"}))).await;
        assert_eq!(status, 422, "{option}: {problem}");
        assert_eq!(problem["type"], "https://llm-tools.dev/problems/unsupported-remote-option");
    }
    let (status, created) = f
        .api
        .post(
            "/api/v1/missions",
            create_body(
                json!({"worker": "alpha", "verifier_name": "unit", "verifier_timeout": 60, "auto_continue": true,
                               "include_config": true, "log_max_bytes": 2048, "log_rotations": 1, "log_retained_runs": 5,
                               "capacity_refresh_seconds": 30}),
            ),
        )
        .await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["request"]["params"]["verifier_name"], "unit");
    assert_eq!(created["request"]["params"]["auto_continue"], true);
    f.env.stop().await;
}

async fn post_with_key(f: &Fleet, key: &str, body: Value) -> (u16, Value) {
    f.api
        .send(f.api.rb(reqwest::Method::POST, "/api/v1/missions").header("idempotency-key", key).json(&body))
        .await
}

#[tokio::test(flavor = "multi_thread")]
async fn a_retry_with_the_same_idempotency_key_repeats_the_original_placement() {
    let f = fleet(vec![spec("alpha", Some(&["codex"]), 0), spec("beta", Some(&["codex"]), 4)]).await;
    let body = create_body(json!({"providers": "codex"}));
    let (status, first) = post_with_key(&f, "create-1", body.clone()).await;
    assert_eq!(status, 202, "{first}");
    assert_eq!(first["placement"]["worker"], "alpha");
    // Alpha now forbids control, so a fresh placement would choose beta.
    let mut hello = serde_json::Map::new();
    hello.insert("missions".into(), json!([]));
    hello.insert(
        "worker_policy".into(),
        json!({"allow_control": false, "allow_terminal_view": false, "allow_create": true,
               "allow_root": ["/srv/work"], "allow_power": false, "allow_restore": false}),
    );
    let capabilities = json!([{"operation":"mission.create","mutating":true}]);
    f.env
        .srv
        .state
        .db
        .worker_hello("alpha", "id-alpha", &hello, &capabilities, "203.0.113.9")
        .unwrap();
    let (status, fresh) = f.api.post("/api/v1/missions", body.clone()).await;
    assert_eq!(status, 202, "{fresh}");
    assert_eq!(fresh["placement"]["worker"], "beta");
    let (status, retry) = post_with_key(&f, "create-1", body).await;
    assert_eq!(status, 202, "{retry}");
    assert_eq!(retry["placement"], first["placement"]);
    assert_eq!(retry["request_id"], first["request_id"]);
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_matches_the_folder_against_allowed_roots_by_path_component() {
    let mut inside = spec("alpha", Some(&["codex"]), 5);
    inside.roots = &["/srv/work"];
    let mut prefix_only = spec("beta", Some(&["codex"]), 0);
    prefix_only.roots = &["/srv/wor"];
    let mut elsewhere = spec("gamma", Some(&["codex"]), 0);
    elsewhere.roots = &["/home/other"];
    let f = fleet(vec![inside, prefix_only, elsewhere]).await;
    let (status, created) = f
        .api
        .post("/api/v1/missions", create_body(json!({"providers": "codex", "cwd": "/srv/work/project"})))
        .await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["placement"]["worker"], "alpha");
    let (status, escaped) = f
        .api
        .post("/api/v1/missions", create_body(json!({"providers": "codex", "cwd": "/srv/work/../secret"})))
        .await;
    assert_eq!(status, 409, "{escaped}");
    let reasons = &escaped["error"]["details"]["workers"];
    assert_eq!(reasons["alpha"], "folder-not-allowed");
    assert_eq!(reasons["beta"], "folder-not-allowed");
    assert_eq!(reasons["gamma"], "folder-not-allowed");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_fetches_missing_snapshots_together_and_reuses_a_pending_fetch() {
    let f = fleet(vec![spec("alpha", None, 0), spec("beta", None, 0)]).await;
    let db = f.env.srv.state.db.clone();
    let request = json!({"schema_version": 1, "operation": "usage.snapshot", "params": {}, "mission_id": null,
                         "target": "alpha", "request_id": "pre-alpha"});
    db.command_insert("alpha", "usage.snapshot", false, &request).unwrap();
    let answered = std::sync::Arc::new(std::sync::Mutex::new(Vec::<(String, String)>::new()));
    let log = answered.clone();
    let responder = tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        for _ in 0..400 {
            for (name, ready) in [("alpha", "codex"), ("beta", "claude")] {
                if let Some(open) = db.open_command(name, "usage.snapshot", 0).unwrap() {
                    let providers: Vec<Value> = ["codex", "claude"]
                        .iter()
                        .map(|p| json!({"provider": p, "available": true, "ready": *p == ready, "scopes": []}))
                        .collect();
                    db.command_done(name, &open.request_id, &json!({"outcome": "applied", "result": {"providers": providers}}))
                        .unwrap();
                    log.lock().unwrap().push((name.to_string(), open.request_id));
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    });
    let started = std::time::Instant::now();
    let (status, created) = f.api.post("/api/v1/missions", create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["placement"]["worker"], "alpha");
    assert!(
        started.elapsed() < std::time::Duration::from_secs(2),
        "the fetches ran together and finished early"
    );
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    responder.abort();
    let answered = answered.lock().unwrap().clone();
    let alpha: Vec<&String> = answered.iter().filter(|(name, _)| name == "alpha").map(|(_, id)| id).collect();
    let beta: Vec<&String> = answered.iter().filter(|(name, _)| name == "beta").map(|(_, id)| id).collect();
    assert_eq!(alpha, vec!["pre-alpha"], "the pending fetch of alpha is reused, not repeated");
    assert_eq!(beta.len(), 1);
    assert!(beta[0].starts_with("placement-"));
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn placement_with_routes_uses_route_rows_and_treats_missing_data_as_unknown() {
    let mut alpha = spec("alpha", Some(&["codex"]), 0);
    alpha.extra = json!({"routes": [{"route": "fast", "provider": "codex", "available": false}]});
    let mut beta = spec("beta", Some(&["codex"]), 2);
    beta.extra = json!({"routes": [{"route": "fast", "provider": "codex", "available": true}]});
    let f = fleet(vec![alpha, beta]).await;
    let (status, created) = f.api.post("/api/v1/missions", create_body(json!({"routes": "fast"}))).await;
    assert_eq!(status, 202, "{created}");
    assert_eq!(created["placement"]["worker"], "beta");
    assert!(created["placement"]["reason"].as_str().unwrap().contains("route fast available"));
    let (status, unknown) = f.api.post("/api/v1/missions", create_body(json!({"routes": "mystery"}))).await;
    assert_eq!(status, 202, "{unknown}");
    assert_eq!(unknown["placement"]["worker"], "alpha");
    assert!(unknown["placement"]["reason"].as_str().unwrap().contains("route readiness unknown"));
    f.env.stop().await;

    let mut only = spec("alpha", Some(&["codex"]), 0);
    only.extra = json!({"routes": [{"route": "fast", "provider": "codex", "available": false}]});
    let f = fleet(vec![only]).await;
    let (status, refused) = f.api.post("/api/v1/missions", create_body(json!({"routes": "fast"}))).await;
    assert_eq!(status, 409, "{refused}");
    assert_eq!(refused["error"]["details"]["workers"]["alpha"], "no requested route is available (fast)");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_token_restricted_to_some_workers_only_sees_those_workers_in_placement() {
    let f = fleet(vec![
        spec("alpha", Some(&["claude"]), 0),
        spec("beta", Some(&["codex"]), 0),
        spec("gamma", Some(&["codex"]), 0),
    ])
    .await;
    let (status, token) = f
        .api
        .post(
            "/api/v1/tokens",
            json!({"name": "alpha-only", "scopes": ["admin"], "allowed_workers": ["alpha"]}),
        )
        .await;
    assert_eq!(status, 201, "{token}");
    let secret = token["secret"].as_str().unwrap().to_string();
    let post = |body: Value| {
        let rb = f.env.api().bare(reqwest::Method::POST, "/api/v1/missions").bearer_auth(&secret).json(&body);
        async move {
            let r = rb.send().await.unwrap();
            (r.status().as_u16(), r.json::<Value>().await.unwrap())
        }
    };
    let (status, refused) = post(create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 409, "{refused}");
    let reasons = refused["error"]["details"]["workers"].as_object().unwrap();
    assert_eq!(reasons.keys().collect::<Vec<_>>(), vec!["alpha"]);
    let (status, placed) = post(create_body(json!({"providers": "claude"}))).await;
    assert_eq!(status, 202, "{placed}");
    assert_eq!(placed["placement"]["worker"], "alpha");
    f.env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stored_placements_are_pruned_with_the_command_they_belong_to() {
    let f = fleet(vec![spec("alpha", Some(&["codex"]), 0), spec("beta", Some(&["claude"]), 0)]).await;
    let (status, kept) = post_with_key(&f, "keep-1", create_body(json!({"providers": "codex"}))).await;
    assert_eq!(status, 202, "{kept}");
    let (status, gone) = post_with_key(&f, "gone-1", create_body(json!({"providers": "claude"}))).await;
    assert_eq!(status, 202, "{gone}");
    assert_eq!(gone["placement"]["worker"], "beta");
    let db = &f.env.srv.state.db;
    db.maintenance().unwrap();
    assert!(db.placement_get("keep-1").unwrap().is_some() && db.placement_get("gone-1").unwrap().is_some());
    // Removing a worker deletes its commands, as retention does for an old or surplus finished command.
    assert!(db.worker_revoke("beta").unwrap());
    db.maintenance().unwrap();
    assert!(db.placement_get("keep-1").unwrap().is_some(), "the placement of a retained command stays");
    assert!(db.placement_get("gone-1").unwrap().is_none(), "the placement of a removed command is pruned");
    f.env.stop().await;
}
