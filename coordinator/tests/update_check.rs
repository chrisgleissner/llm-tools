//! PRD-011 opt-in update availability in the diagnostics API.
mod common;
use common::*;
use serde_json::json;

#[tokio::test(flavor = "multi_thread")]
async fn default_config_makes_no_update_request_and_reports_off() {
    let env = boot().await;
    let api = env.admin().await;
    let (st, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(st, 200);
    assert_eq!(d["checks"]["update_check"]["value"], "off");
    assert_eq!(d["checks"]["update_check"]["status"], "ok");
    assert!(!env.dir.join("update-check.json").exists());
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn enabled_check_shows_a_cached_newer_release_without_network() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    std::fs::write(
        dir.join("update-check.json"),
        json!({"checked_at": bridge_coordinator::now(), "latest": "99.0.0", "error": null}).to_string(),
    )
    .unwrap();
    edit_config(&dir, |t| t.replace("8443", "0").replace("update_check = false", "update_check = true"));
    let srv = bridge_coordinator::server::start(&dir).await.unwrap();
    let ca = std::fs::read(dir.join("ca.pem")).unwrap();
    let tls = client_tls(&ca);
    let client = reqwest::Client::builder().use_preconfigured_tls((*tls).clone()).build().unwrap();
    let env = Env {
        addr: srv.addr,
        dir,
        srv,
        tls,
        client,
    };
    let api = env.admin().await;
    let (st, d) = api.get("/api/v1/diagnostics").await;
    assert_eq!(st, 200);
    assert_eq!(d["checks"]["update_check"]["value"], "update available: 99.0.0");
    assert_eq!(d["checks"]["update_check"]["status"], "warn");
    assert!(matches!(d["status"].as_str(), Some("warn" | "fail")));
    env.stop().await;
}
