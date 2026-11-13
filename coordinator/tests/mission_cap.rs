//! Review follow-up for the capacity caps: updates at the cap, the refused count, the bounded hello on the wire and
//! enrolment with client certificates.
mod common;
use bridge_coordinator::db::{Db, MAX_ENROLLED_WORKERS, MAX_MISSIONS_PER_WORKER};
use common::*;
use serde_json::{json, Value};

fn summary(id: &str, status: &str) -> Value {
    json!({"id": id, "name": "mission", "status": status, "folder": "project-dir", "updated_at": "2026-01-01T00:00:00+00:00",
           "task_counts": {"COMPLETED": 36, "QUEUED": 4}, "current_task": {"ordinal": 37, "title": "Add the retry path to the scheduler"},
           "station": "claude", "model": "claude-sonnet-5", "needs_kind": null, "needs_tasks": 0})
}

fn hello(name: &str, missions: Vec<Value>) -> Value {
    json!({"worker_id": format!("wid-{name}"), "name": name, "software_version": "test", "protocol_versions": [1],
           "platform": {"system": "Linux"}, "capabilities": caps(), "missions": missions, "event_cursor": 0})
}

async fn connect(env: &Env, token: &str, name: &str, missions: Vec<Value>) -> Fw {
    let ws = Fw::raw(env, token).await.unwrap();
    let mut fw = Fw {
        ws,
        worker_id: format!("wid-{name}"),
        generation: 0,
        seq: 0,
    };
    let frame = hello(name, missions).to_string();
    assert!(frame.len() < 1024 * 1024, "the hello frame is {} bytes", frame.len());
    fw.send("hello", serde_json::from_str(&frame).unwrap()).await;
    let welcome = fw.recv().await;
    assert_eq!(welcome["type"], "welcome", "{welcome}");
    fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
    fw
}

/// Ping twice and collect every message up to the second pong. The coordinator's outbound queue and its reply to a ping
/// are separate paths, so one round trip could return before a queued notice.
async fn drain(fw: &mut Fw) -> Vec<Value> {
    let mut seen = Vec::new();
    for _ in 0..2 {
        fw.send("ping", json!({})).await;
        loop {
            let m = fw.recv().await;
            if m["type"] == "pong" {
                break;
            }
            seen.push(m);
        }
    }
    seen
}

/// Ping until a `notice` shows up (at most 20 round trips); the queued notice and the pong take separate paths.
async fn drain_for_notice(fw: &mut Fw) -> Vec<Value> {
    let mut seen = Vec::new();
    for _ in 0..20 {
        seen.extend(drain(fw).await);
        if seen.iter().any(|m| m["type"] == "notice") {
            break;
        }
    }
    seen
}

fn mission_ids(env: &Env, worker: &str) -> std::collections::BTreeSet<String> {
    env.srv
        .state
        .db
        .missions_list()
        .unwrap()
        .into_iter()
        .filter(|(w, _)| w == worker)
        .map(|(_, info)| info["id"].as_str().unwrap().to_string())
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn updating_a_stored_active_mission_at_the_cap_evicts_nothing_and_a_new_one_is_refused_with_a_notice() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    // 3000 + 3000 + 3000 + 1000 active missions in four frames: exactly the cap.
    let mut fw = connect(&env, &token, "a", (0..3000).map(|i| summary(&format!("a{i:05}"), "RUNNING")).collect()).await;
    for (prefix, count) in [("b", 3000), ("c", 3000), ("d", 1000)] {
        let batch: Vec<Value> = (0..count).map(|i| summary(&format!("{prefix}{i:05}"), "RUNNING")).collect();
        fw.send("events", json!({"after": 0, "through": 0, "events": [], "missions": batch})).await;
        assert!(drain(&mut fw).await.iter().all(|m| m["type"] != "error"));
    }
    assert_eq!(mission_ids(&env, "a").len() as u64, MAX_MISSIONS_PER_WORKER);
    // Update an existing active mission, twice: it stays and nothing else goes.
    let mut changed = summary("a00000", "RUNNING");
    changed["name"] = json!("renamed");
    for _ in 0..2 {
        fw.send("events", json!({"after": 0, "through": 0, "events": [], "missions": [changed.clone()]}))
            .await;
        let seen = drain(&mut fw).await;
        assert!(seen.iter().all(|m| m["type"] != "notice" && m["type"] != "error"), "{seen:?}");
        assert_eq!(mission_ids(&env, "a").len() as u64, MAX_MISSIONS_PER_WORKER);
    }
    let stored: Value = env
        .srv
        .state
        .db
        .missions_list()
        .unwrap()
        .into_iter()
        .find(|(_, i)| i["id"] == "a00000")
        .unwrap()
        .1;
    assert_eq!(stored["name"], "renamed");
    // A new active mission over the cap is refused and the worker is told.
    // The new id comes first and the update second: a refusal that counted updates as inserts would drop a00000.
    let mut renamed_again = summary("a00000", "RUNNING");
    renamed_again["name"] = json!("renamed again");
    fw.send(
        "events",
        json!({"after": 0, "through": 0, "events": [], "missions": [summary("new-one", "RUNNING"), renamed_again]}),
    )
    .await;
    let seen = drain_for_notice(&mut fw).await;
    let notice = seen.iter().find(|m| m["type"] == "notice").unwrap_or_else(|| panic!("no notice in {seen:?}"));
    assert_eq!(
        (notice["payload"]["code"].as_str(), notice["payload"]["refused"].as_u64()),
        (Some("missions-over-cap"), Some(1))
    );
    assert!(!mission_ids(&env, "a").contains("new-one") && mission_ids(&env, "a").contains("a00000"));
    let stored: Value = env
        .srv
        .state
        .db
        .missions_list()
        .unwrap()
        .into_iter()
        .find(|(_, i)| i["id"] == "a00000")
        .unwrap()
        .1;
    assert_eq!(stored["name"], "renamed again");
    assert_eq!(mission_ids(&env, "a").len() as u64, MAX_MISSIONS_PER_WORKER);
    // The refused count is stored: a second connection to the database sees it (a restart reads the same row).
    let reopened = Db::open(&env.dir.join(bridge_coordinator::DB_FILE)).unwrap();
    let warning = reopened.diagnostics().unwrap()["warnings"][0].clone();
    assert_eq!(
        (warning["code"].as_str(), warning["worker"].as_str(), warning["missions_refused"].as_u64()),
        (Some("missions-over-cap"), Some("a"), Some(1))
    );
    assert!(warning["last_refused_at"].as_u64().unwrap() > 0);
    // A hello that fits clears the warning and tells the worker.
    let mut again = connect(&env, &token, "a", vec![summary("only", "RUNNING")]).await;
    let seen = drain_for_notice(&mut again).await;
    assert_eq!(
        seen.iter().find(|m| m["type"] == "notice").map(|m| m["payload"]["refused"].as_u64()),
        Some(Some(0)),
        "{seen:?}"
    );
    assert_eq!(reopened.diagnostics().unwrap()["warnings"], json!([]));
    env.stop().await;
}

/// The worker sends a bounded hello and the rest as events batches: 4500 realistic summaries end up indexed with no
/// error frame and no frame near the 1 MiB limit.
#[tokio::test(flavor = "multi_thread")]
async fn a_bounded_hello_followed_by_events_batches_indexes_all_4500_missions() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("a").await;
    let every: Vec<Value> = (0..4500)
        .map(|i| summary(&format!("m{i:05}"), if i < 300 { "RUNNING" } else { "COMPLETED" }))
        .collect();
    let mut fw = connect(&env, &token, "a", every[..2000].to_vec()).await;
    for chunk in every[2000..].chunks(1000) {
        fw.send(
            "events",
            json!({"after": 0, "through": 0, "events": [], "mission_cursor": 0, "missions": chunk}),
        )
        .await;
        let seen = drain(&mut fw).await;
        assert!(seen.iter().all(|m| m["type"] != "error"), "{seen:?}");
    }
    assert_eq!(mission_ids(&env, "a").len(), 4500);
    let (status, body) = api.get("/api/v1/missions?limit=1").await;
    assert_eq!((status, body["total"].as_u64()), (200, Some(4500)), "{body}");
    env.stop().await;
}

fn fingerprint(env: &Env, name: &str) -> Option<String> {
    env.srv.state.db.worker_get(name).unwrap().and_then(|w| w.client_cert_fingerprint)
}

fn listing(dir: &std::path::Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
        .collect();
    names.sort();
    names
}

#[tokio::test(flavor = "multi_thread")]
async fn a_refused_enrolment_with_client_certificates_leaves_no_files_and_keeps_the_existing_identity() {
    let env = boot_with(|t| {
        t.replace("worker_mtls = false", "worker_mtls = true")
            .replace("mutation_limit_per_token = 30", "mutation_limit_per_token = 10000")
            .replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 10000")
    })
    .await;
    let api = env.admin().await;
    let (status, first) = api.post("/api/v1/workers", json!({"name": "keep"})).await;
    assert_eq!(status, 201, "{first}");
    let keep = fingerprint(&env, "keep").expect("the first enrolment stored a fingerprint");
    let before = listing(&env.dir);
    // An existing name: refused, no new identity, the stored one is untouched.
    let (status, body) = api.post("/api/v1/workers", json!({"name": "keep"})).await;
    assert_eq!((status, body["code"].as_str()), (409, Some("worker-exists")), "{body}");
    assert!(body.get("client_private_key").is_none());
    assert_eq!(fingerprint(&env, "keep"), Some(keep.clone()));
    // A full table: refused before a certificate is issued, and nothing new on disk.
    for i in 0..MAX_ENROLLED_WORKERS - 1 {
        assert!(env.srv.state.db.worker_add(&format!("e{i}"), &format!("hash{i}")).unwrap());
    }
    let (status, body) = api.post("/api/v1/workers", json!({"name": "late"})).await;
    assert_eq!((status, body["code"].as_str()), (409, Some("too-many-workers")), "{body}");
    assert!(body.get("client_private_key").is_none() && body.get("client_certificate").is_none());
    assert!(env.srv.state.db.worker_get("late").unwrap().is_none());
    assert_eq!(listing(&env.dir), before, "no file was created or removed");
    assert_eq!(fingerprint(&env, "keep"), Some(keep));
    env.stop().await;
}
