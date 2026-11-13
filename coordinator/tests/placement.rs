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
}

fn spec<'a>(name: &'a str, ready: Option<&'a [&'a str]>, running: usize) -> Spec<'a> {
    Spec {
        name,
        online: true,
        control: true,
        ready,
        running,
    }
}

async fn fleet(specs: Vec<Spec<'_>>) -> Fleet {
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
                   "allow_root": ["/srv/work"], "allow_power": false, "allow_restore": false}),
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
                .command_done(s.name, &id, &json!({"outcome": "applied", "result": {"providers": providers}}))
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
