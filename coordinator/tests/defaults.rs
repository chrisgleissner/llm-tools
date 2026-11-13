use bridge_coordinator::config::{config_text, Config, Exposure};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::Path};

fn doc() -> String {
    fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("../docs/distributed/safe-defaults.md")).expect("read docs/distributed/safe-defaults.md")
}

fn table(doc: &str, marker: &str) -> Vec<(String, String, String)> {
    let begin = format!("<!-- {marker}:begin -->");
    let end = format!("<!-- {marker}:end -->");
    let (_, rest) = doc.split_once(&begin).unwrap_or_else(|| panic!("missing table marker {begin}"));
    let (body, _) = rest.split_once(&end).unwrap_or_else(|| panic!("missing table marker {end}"));
    body.lines()
        .map(str::trim)
        .filter(|line| line.starts_with('|') && !line.starts_with("|---"))
        .filter_map(|line| {
            let cells: Vec<_> = line.trim_matches('|').split('|').map(str::trim).collect();
            assert_eq!(cells.len(), 3, "expected three cells in {marker}: {line}");
            if cells[0] == "Setting" || cells[0] == "Constant" {
                return None;
            }
            Some((cells[0].trim_matches('`').into(), cells[1].trim_matches('`').into(), cells[2].into()))
        })
        .collect()
}

fn flatten(v: &Value) -> Vec<(String, String)> {
    fn visit(prefix: &str, value: &Value, rows: &mut Vec<(String, String)>) {
        if let Some(object) = value.as_object() {
            for (key, child) in object {
                let key = if prefix.is_empty() { key.clone() } else { format!("{prefix}.{key}") };
                visit(&key, child, rows);
            }
        } else {
            rows.push((prefix.into(), serde_json::to_string(value).expect("serialize default")));
        }
    }
    assert!(v.is_object(), "Config must serialize as an object");
    let mut rows = Vec::new();
    visit("", v, &mut rows);
    rows
}

fn code_defaults() -> BTreeMap<String, String> {
    flatten(&serde_json::to_value(Config::default()).expect("serialize Config defaults"))
        .into_iter()
        .collect()
}

fn documented_defaults(marker: &str) -> BTreeMap<String, String> {
    let rows = table(&doc(), marker);
    let mut defaults = BTreeMap::new();
    for (key, value, reason) in rows {
        assert!(!reason.is_empty(), "missing reason for {key}");
        assert!(defaults.insert(key.clone(), value).is_none(), "duplicate documented setting {key}");
    }
    defaults
}

fn problems(code: &BTreeMap<String, String>, docs: &BTreeMap<String, String>) -> Vec<String> {
    let mut problems = Vec::new();
    for (key, value) in code {
        match docs.get(key) {
            None => problems.push(format!(
                "undocumented setting {key} (default {value}): add a row to docs/distributed/safe-defaults.md"
            )),
            Some(documented) if documented != value => {
                problems.push(format!("default of {key} is {value} in the code but {documented} in the docs"));
            }
            Some(_) => {}
        }
    }
    for key in docs.keys() {
        if !code.contains_key(key) {
            problems.push(format!("documented setting {key} does not exist in Config"));
        }
    }
    problems
}

#[test]
fn every_config_setting_has_a_documented_default_and_the_value_matches() {
    let problems = problems(&code_defaults(), &documented_defaults("coordinator-defaults"));
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn adding_a_setting_without_a_row_fails() {
    let code = code_defaults();
    let docs = documented_defaults("coordinator-defaults");
    let mut extra = code.clone();
    extra.insert("new_setting".into(), "1".into());
    assert!(problems(&extra, &docs).iter().any(|p| p.contains("undocumented setting new_setting")));

    let mut missing = docs.clone();
    assert!(missing.remove("bind").is_some());
    assert!(problems(&code, &missing).iter().any(|p| p.contains("undocumented setting bind")));

    let mut changed = docs.clone();
    changed.insert("bind".into(), "null".into());
    assert!(problems(&code, &changed)
        .iter()
        .any(|p| p.contains("default of bind") && p.contains("in the docs")));

    let mut obsolete = docs;
    obsolete.insert("removed_setting".into(), "1".into());
    assert!(problems(&code, &obsolete)
        .iter()
        .any(|p| p == "documented setting removed_setting does not exist in Config"));
}

#[test]
fn defaults_are_the_safe_choices() {
    let d = Config::default();
    assert!(d.bind.parse::<std::net::SocketAddr>().expect("default bind address").ip().is_loopback());
    assert_eq!(d.exposure, Exposure::Loopback);
    assert!(!d.worker_mtls && !d.tls_terminator);
    assert_eq!(d.min_tls, "1.3");
    assert!(d.allowed_origins.is_empty());
    assert!(d.trusted_proxies.is_empty());
    assert!(d.security_webhook_url.is_none());
    assert!(d.tls_cert_file.is_none() && d.tls_key_file.is_none());
    assert!(d.listener_groups.console_rest.is_none());
    assert!(d.listener_groups.worker_channel.is_none());
    assert!(d.listener_groups.health.is_none());
    assert!(d.max_token_lifetime_days > 0 && d.max_token_lifetime_days <= 3650);
    assert!(d.backup_interval_hours > 0);
    assert!(d.backup_keep >= 1);
    assert_eq!(d.backup_interval_seconds, 0);
    assert!(d.mutation_limit_per_token > 0 && d.mutation_limit_per_address > 0);
    assert!(d.max_connections > 0);
    assert!(d.worker_reserve_connections <= d.max_connections / 4);
    assert!(d.request_body_timeout_secs > 0 && d.http_idle_timeout_secs > 0 && d.ws_idle_timeout_secs > d.ws_ping_interval_secs);
    if let Some(update_check) = serde_json::to_value(&d).expect("serialize defaults").get("update_check") {
        assert_eq!(update_check, &Value::Bool(false));
    }
}

#[test]
fn generated_config_file_matches_the_built_in_defaults() {
    let generated: Config = toml::from_str(&config_text("127.0.0.1:8443", false)).expect("parse init config");
    assert_eq!(serde_json::to_value(generated).unwrap(), serde_json::to_value(Config::default()).unwrap());
}

#[test]
fn fixed_defaults_match_the_docs() {
    use bridge_coordinator::{auth, db, http, hub};
    let code = [
        ("db::SESSION_IDLE_SECS", format!("{}", db::SESSION_IDLE_SECS)),
        ("db::SESSION_ABSOLUTE_SECS", format!("{}", db::SESSION_ABSOLUTE_SECS)),
        ("db::SESSION_IDLE_SECS_SHORT", format!("{}", db::SESSION_IDLE_SECS_SHORT)),
        ("db::SESSION_ABSOLUTE_SECS_SHORT", format!("{}", db::SESSION_ABSOLUTE_SECS_SHORT)),
        ("auth::MIN_PASSWORD_LEN", format!("{}", auth::MIN_PASSWORD_LEN)),
        ("http::MAX_BODY", format!("{}", http::MAX_BODY)),
        ("db::MAX_ENROLLED_WORKERS", format!("{}", db::MAX_ENROLLED_WORKERS)),
        ("db::MAX_MISSIONS_PER_WORKER", format!("{}", db::MAX_MISSIONS_PER_WORKER)),
        ("hub::MAX_WORKER_CONNECTIONS", format!("{}", hub::MAX_WORKER_CONNECTIONS)),
        ("http::COOKIE", format!("{:?}", http::COOKIE)),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_string(), value))
    .collect();
    let problems = problems(&code, &documented_defaults("coordinator-fixed-defaults"));
    assert!(problems.is_empty(), "{}", problems.join("\n"));
    assert!(http::COOKIE.starts_with("__Host-"));
}
