//! The mission list parses only the fields it filters and sorts on. For a varied set of summaries (missing fields,
//! non-string values, mixed case, non-ASCII) the order, total and status counts must equal those of a full parse
//! of every summary.
mod common;
use bridge_coordinator::db::HelloOutcome;
use common::*;
use serde_json::{json, Value};

fn hello_with(name: &str, missions: Vec<Value>) -> serde_json::Map<String, Value> {
    json!({"worker_id": format!("wid-{name}"), "name": name, "software_version": "test", "protocol_versions": [1],
           "platform": {"system": "Linux"}, "capabilities": caps(), "missions": missions, "event_cursor": 0})
    .as_object()
    .unwrap()
    .clone()
}

fn text(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_light_mission_list_matches_a_full_parse() {
    let env = boot().await;
    let api = env.admin().await;
    let db = &env.srv.state.db;
    let summaries: Vec<Value> = vec![
        json!({"id": "m1", "name": "Alpha", "status": "RUNNING", "folder": "proj", "updated_at": "2026-01-03"}),
        json!({"id": "m2", "name": "Zeta", "status": "completed", "folder": "Zoo", "updated_at": "2026-01-01"}),
        json!({"id": "m3", "status": "PAUSED", "folder": "proj"}),
        json!({"id": "m4", "name": 42, "status": null, "folder": ["x"], "updated_at": 7}),
        json!({"id": "m5", "name": "Ünïcode Gamma", "status": "COMPLETED", "folder": "proj", "updated_at": "2026-01-02", "extra": {"deep": [1, 2, {"x": null}]}}),
        json!({"id": "m6", "name": "alpha two", "status": "RUNNING", "folder": "other", "updated_at": ""}),
        json!({"id": "a-m1", "name": "Alpha", "status": "RUNNING", "folder": "proj", "updated_at": "2026-01-03"}),
    ];
    for (worker, list) in [("w1", summaries[..6].to_vec()), ("w2", summaries[6..].to_vec())] {
        api.add_worker(worker).await;
        let outcome = db
            .worker_hello(worker, &format!("wid-{worker}"), &hello_with(worker, list), &caps(), "127.0.0.1")
            .unwrap();
        assert!(matches!(outcome, HelloOutcome::Ok { .. }));
    }
    let rows: Vec<(String, Value)> = db.missions_list().unwrap();
    for query in [
        "",
        "q=alpha",
        "q=proj&sort=name",
        "status=running,completed&sort=status",
        "sort=updated&order=desc",
        "sort=name",
        "sort=status&order=desc",
        "worker=w2",
        "q=%C3%BCn&sort=name",
    ] {
        let param = |k: &str| query.split('&').find_map(|kv| kv.strip_prefix(&format!("{k}=")));
        let needle = param("q").map(|q| q.replace("%C3%BC", "ü").to_lowercase());
        let statuses: Vec<String> = param("status").map(|s| s.split(',').map(str::to_uppercase).collect()).unwrap_or_default();
        let mut reference: Vec<&(String, Value)> = rows
            .iter()
            .filter(|(w, info)| {
                param("worker").is_none_or(|x| x == w)
                    && needle.as_ref().is_none_or(|n| {
                        [w.as_str(), &text(info, "name"), &text(info, "id"), &text(info, "folder")]
                            .iter()
                            .any(|f| f.to_lowercase().contains(n.as_str()))
                    })
            })
            .collect();
        let mut by_status = std::collections::BTreeMap::<String, u64>::new();
        for (_, info) in &reference {
            *by_status.entry(text(info, "status")).or_default() += 1;
        }
        reference.retain(|(_, info)| statuses.is_empty() || statuses.contains(&text(info, "status").to_uppercase()));
        if let Some(sort) = param("sort") {
            let key = |r: &&(String, Value)| match sort {
                "name" => text(&r.1, "name").to_lowercase(),
                "status" => text(&r.1, "status"),
                "worker" => r.0.clone(),
                _ => text(&r.1, "updated_at"),
            };
            reference.sort_by(|a, b| key(a).cmp(&key(b)).then_with(|| text(&a.1, "id").cmp(&text(&b.1, "id"))));
            if param("order") == Some("desc") {
                reference.reverse();
            }
        }
        let url = format!("/api/v1/missions?limit=500&{query}");
        let (status, body) = api.get(&url).await;
        assert_eq!(status, 200, "{url}: {body}");
        let got: Vec<(String, Value)> = body["missions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| (m["worker"].as_str().unwrap().to_string(), m["mission"].clone()))
            .collect();
        // Public timestamp fields are served as RFC 3339 UTC (API-004), so the numeric epoch fixture appears as text.
        let want: Vec<(String, Value)> = reference
            .iter()
            .map(|r| {
                let mut mission = r.1.clone();
                if mission["updated_at"] == json!(7) {
                    mission["updated_at"] = json!("1970-01-01T00:00:07Z");
                }
                (r.0.clone(), mission)
            })
            .collect();
        assert_eq!(got, want, "{url}");
        assert_eq!(body["total"].as_u64().unwrap() as usize, want.len(), "{url}");
        let counts: std::collections::BTreeMap<String, u64> = serde_json::from_value(body["statuses"].clone()).unwrap();
        assert_eq!(counts, by_status, "{url}");
    }
    env.stop().await;
}
