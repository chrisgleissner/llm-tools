//! FIND-004: a failed audit write is never silent.
//!
//! The failure is injected with a trigger on the `audit` table that aborts every insert, created through a
//! second connection to the data directory's database file. A new mutating command is stored together with its
//! audit entry, so a failed audit write fails the submission with a 5xx and stores no command. An action that
//! cannot be undone (an API token that was created) makes `/healthz` report a fault until an audit write succeeds.
mod common;
use common::*;
use reqwest::Method;
use rusqlite::Connection;
use serde_json::{json, Value};

fn db_file(env: &Env) -> std::path::PathBuf {
    bridge_coordinator::db_path(&env.dir)
}

fn fail_audit_inserts(env: &Env) {
    let c = Connection::open(db_file(env)).unwrap();
    c.busy_timeout(std::time::Duration::from_secs(5)).unwrap();
    c.execute_batch("CREATE TRIGGER audit_fail BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END;")
        .unwrap();
}

fn allow_audit_inserts(env: &Env) {
    let c = Connection::open(db_file(env)).unwrap();
    c.execute_batch("DROP TRIGGER audit_fail").unwrap();
}

fn audit_rows(env: &Env, action: &str) -> i64 {
    Connection::open(db_file(env))
        .unwrap()
        .query_row("SELECT COUNT(*) FROM audit WHERE action=?", [action], |r| r.get(0))
        .unwrap()
}

fn submit(id: &str) -> Value {
    json!({"target": "w1", "request": request(id, "task.add")})
}

async fn health(env: &Env) -> (u16, Value) {
    let r = env.client.get(format!("{}/healthz", env.base())).send().await.unwrap();
    (r.status().as_u16(), r.json().await.unwrap_or(Value::Null))
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_audit_write_fails_a_command_submission_and_stores_nothing() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (_fw, _) = Fw::connect_settled(&env, &token, "wid1", 0).await;

    assert_eq!(api.post("/api/v1/commands", submit("before")).await.0, 202);
    assert_eq!(audit_rows(&env, "command.submit"), 1);

    fail_audit_inserts(&env);
    let (status, body) = api.post("/api/v1/commands", submit("during")).await;
    assert!(
        (500..600).contains(&status),
        "a submission whose audit write failed was answered {status}: {body}"
    );
    assert_eq!(
        api.get("/api/v1/commands/during").await.0,
        404,
        "the command of the failed submission was stored"
    );
    assert_eq!(audit_rows(&env, "command.submit"), 1);
    assert_eq!(health(&env).await.0, 503, "a failed audit write is not reported by /healthz");

    allow_audit_inserts(&env);
    let (status, body) = api.post("/api/v1/commands", submit("during")).await;
    assert_eq!(status, 202, "the same request id is accepted once audit writes work again: {body}");
    assert_eq!(audit_rows(&env, "command.submit"), 2);
    assert_eq!(health(&env).await, (200, json!({"status": "ok"})));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_audit_write_after_an_irreversible_action_is_a_health_fault() {
    let env = boot().await;
    let api = env.admin().await;
    assert_eq!(health(&env).await.0, 200);

    fail_audit_inserts(&env);
    let (status, body) = api.post("/api/v1/tokens", json!({"name": "t1", "scopes": ["read"]})).await;
    assert_eq!(status, 201, "{body}");
    let (code, report) = health(&env).await;
    assert_eq!(code, 503, "{report}");
    assert_eq!(report["status"], "fault");
    let fault = report["faults"][0].as_str().unwrap();
    assert!(fault.contains("audit-write-failed") && fault.contains("injected audit failure"), "{fault}");
    assert_eq!(audit_rows(&env, "token.create"), 0);

    // While the failure persists, health stays a fault however often it is asked.
    assert_eq!(health(&env).await.0, 503);
    allow_audit_inserts(&env);
    // Nothing else is audited: health retries the write itself and recovers.
    let mut code = 0;
    for _ in 0..20 {
        code = health(&env).await.0;
        if code == 200 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    assert_eq!(code, 200, "health did not recover after audit writes worked again");
    assert_eq!(audit_rows(&env, "audit.recovered"), 1);
    let (status, _) = api.send(api.rb(Method::DELETE, "/api/v1/tokens/t1")).await;
    assert_eq!(status, 200);
    assert_eq!(audit_rows(&env, "token.revoke"), 1);
    env.stop().await;
}
