//! Validation of `config.toml` and the environment overrides (PRD-004).
//!
//! The file is read as a generic TOML table first, so that every problem is found in one pass
//! instead of stopping at the first one. Each problem is one line:
//! `config error: <key> = <value>: <problem>; fix: <what to do>`.
//! The same code runs for `check-config` and before `serve` binds anything.

use crate::config::{self, Config, Exposure};
use std::net::{IpAddr, SocketAddr};
use std::path::Path;

/// Which checks beyond syntax, types and ranges apply.
#[derive(Debug, Clone, Copy, Default)]
pub struct CheckOptions {
    /// Refuse a non-loopback bind while `exposure` is `loopback` (start-up and `check-config`).
    pub require_lan: bool,
    /// Check that `web_dir` holds an `index.html` (start-up and `check-config`).
    pub check_paths: bool,
}

#[derive(Clone, Copy)]
enum Kind {
    Bind,
    Bool,
    UInt {
        min: i64,
    },
    /// Whole number within an inclusive range.
    UIntRange {
        min: i64,
        max: i64,
    },
    /// One of a fixed list of strings.
    Choice(&'static [&'static str]),
    /// Optional non-empty file path (TLS certificate provider).
    FilePath,
    Origins,
    Path,
    /// List of strings (trusted proxy CIDRs; element validity is checked in the cross-key pass).
    StringList,
    /// Sub-table of optional listener addresses.
    Table,
}

struct Spec {
    key: &'static str,
    kind: Kind,
    /// Default shown in the fix text; `None` for keys without a default.
    default: Option<String>,
}

fn specs() -> Vec<Spec> {
    let d = Config::default();
    let n = |v: u64| Some(v.to_string());
    vec![
        Spec {
            key: "bind",
            kind: Kind::Bind,
            default: Some(format!("{:?}", d.bind)),
        },
        Spec {
            key: "exposure",
            kind: Kind::Choice(&["loopback", "lan", "internet"]),
            default: Some("\"loopback\"".into()),
        },
        // Deprecated: replaced by `exposure`; still read, and rewritten by `Config::load`.
        Spec {
            key: "lan_enabled",
            kind: Kind::Bool,
            default: Some("false".into()),
        },
        Spec {
            key: "listener_groups",
            kind: Kind::Table,
            default: None,
        },
        Spec {
            key: "worker_mtls",
            kind: Kind::Bool,
            default: Some(d.worker_mtls.to_string()),
        },
        Spec {
            key: "tls_terminator",
            kind: Kind::Bool,
            default: Some(d.tls_terminator.to_string()),
        },
        Spec {
            key: "security_webhook_url",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "trusted_proxies",
            kind: Kind::StringList,
            default: Some("[]".into()),
        },
        Spec {
            key: "public_origin",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "max_token_lifetime_days",
            kind: Kind::UIntRange { min: 1, max: 3650 },
            default: n(d.max_token_lifetime_days as u64),
        },
        Spec {
            key: "backup_interval_hours",
            kind: Kind::UIntRange { min: 0, max: 8760 },
            default: n(d.backup_interval_hours),
        },
        Spec {
            key: "backup_keep",
            kind: Kind::UIntRange { min: 1, max: 365 },
            default: n(d.backup_keep as u64),
        },
        Spec {
            key: "backup_dir",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "backup_interval_seconds",
            kind: Kind::UInt { min: 0 },
            default: n(d.backup_interval_seconds),
        },
        Spec {
            key: "update_check",
            kind: Kind::Bool,
            default: Some(d.update_check.to_string()),
        },
        Spec {
            key: "min_tls",
            kind: Kind::Choice(&["1.3", "1.2"]),
            default: Some(format!("{:?}", d.min_tls)),
        },
        Spec {
            key: "max_events_per_worker",
            kind: Kind::UInt { min: 1 },
            default: n(d.max_events_per_worker),
        },
        Spec {
            key: "allowed_origins",
            kind: Kind::Origins,
            default: Some("[]".into()),
        },
        Spec {
            key: "web_dir",
            kind: Kind::Path,
            default: None,
        },
        Spec {
            key: "tls_cert_file",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "tls_key_file",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "tls_cert_name",
            kind: Kind::FilePath,
            default: None,
        },
        Spec {
            key: "max_connections",
            kind: Kind::UInt { min: 1 },
            default: n(d.max_connections as u64),
        },
        Spec {
            key: "max_connections_per_ip",
            kind: Kind::UInt { min: 1 },
            default: n(d.max_connections_per_ip as u64),
        },
        Spec {
            key: "worker_reserve_connections",
            kind: Kind::UInt { min: 0 },
            default: n(d.worker_reserve_connections as u64),
        },
        Spec {
            key: "request_body_timeout_secs",
            kind: Kind::UInt { min: 1 },
            default: n(d.request_body_timeout_secs),
        },
        Spec {
            key: "http_idle_timeout_secs",
            kind: Kind::UInt { min: 1 },
            default: n(d.http_idle_timeout_secs),
        },
        Spec {
            key: "ws_ping_interval_secs",
            kind: Kind::UInt { min: 1 },
            default: n(d.ws_ping_interval_secs),
        },
        Spec {
            key: "ws_idle_timeout_secs",
            kind: Kind::UInt { min: 1 },
            default: n(d.ws_idle_timeout_secs),
        },
        Spec {
            key: "mutation_limit_per_token",
            kind: Kind::UIntRange { min: 1, max: 10_000 },
            default: n(d.mutation_limit_per_token as u64),
        },
        Spec {
            key: "mutation_limit_per_address",
            kind: Kind::UIntRange { min: 1, max: 10_000 },
            default: n(d.mutation_limit_per_address as u64),
        },
    ]
}

/// Edit distance between two strings (insert, delete and replace cost 1).
pub fn levenshtein(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.chars().enumerate() {
        let mut cur = vec![i + 1];
        for (j, cb) in b.iter().enumerate() {
            let cost = usize::from(ca != *cb);
            cur.push((prev[j] + cost).min(prev[j + 1] + 1).min(cur[j] + 1));
        }
        prev = cur;
    }
    prev[b.len()]
}

/// The known name closest to `unknown`, when one is close enough to be a plausible typo.
pub fn suggest<'a>(unknown: &str, known: &[&'a str]) -> Option<&'a str> {
    let norm = |s: &str| s.to_ascii_lowercase().replace('-', "_");
    let u = norm(unknown);
    let mut best: Option<(usize, &'a str)> = None;
    for k in known {
        let mut dist = levenshtein(&u, k);
        // An unknown key that is the start of a known one (`ws_ping_interval` for `ws_ping_interval_secs`).
        if u.len() >= 4 && k.starts_with(&u) {
            dist = dist.min(1);
        }
        if best.is_none_or(|(d, _)| dist < d) {
            best = Some((dist, k));
        }
    }
    let limit = (u.chars().count() / 3).max(2);
    best.filter(|(d, _)| *d <= limit).map(|(_, k)| k)
}

fn show(v: &toml::Value) -> String {
    let s = v.to_string();
    let s = s.trim().replace('\n', " ");
    if s.chars().count() > 60 {
        format!("{}...", s.chars().take(57).collect::<String>())
    } else {
        s
    }
}

fn err(key: &str, value: &str, problem: &str, fix: &str) -> String {
    format!("config error: {key} = {value}: {problem}; fix: {fix}")
}

fn type_name(v: &toml::Value) -> &'static str {
    match v {
        toml::Value::String(_) => "a string",
        toml::Value::Integer(_) => "a whole number",
        toml::Value::Float(_) => "a decimal number",
        toml::Value::Boolean(_) => "a boolean",
        toml::Value::Datetime(_) => "a date or time",
        toml::Value::Array(_) => "a list",
        toml::Value::Table(_) => "a table",
    }
}

const BIND_FIX: &str = "write host:port with a numeric address, for example \"127.0.0.1:8443\", \"192.168.1.20:8443\" or \"[::1]:8443\"";

/// Problem text and fix for a bind address that does not parse.
fn bind_problem(s: &str) -> (String, String) {
    let problem = match s.parse::<SocketAddr>() {
        Ok(_) => unreachable!("only called for unparsable bind addresses"),
        Err(e) => format!("not a valid listen address ({e})"),
    };
    let fix = if s.is_empty() {
        BIND_FIX.to_string()
    } else if s.parse::<IpAddr>().is_ok() {
        format!(
            "add a port, for example \"{}:8443\"",
            if s.contains(':') { format!("[{s}]") } else { s.to_string() }
        )
    } else if s.rsplit_once(':').is_some_and(|(_, p)| p.parse::<u16>().is_err()) {
        "the port must be a whole number from 0 to 65535, for example \"127.0.0.1:8443\"".to_string()
    } else {
        "the host must be an IP address, not a name (use 127.0.0.1, a LAN address, 0.0.0.0 or [::]), for example \"127.0.0.1:8443\"".to_string()
    };
    (problem, fix)
}

fn check_origin(key: &str, o: &str, errors: &mut Vec<String>) {
    let rest = o.strip_prefix("https://").or_else(|| o.strip_prefix("http://"));
    let fix = "use an exact origin, scheme://host[:port] without a path, for example \"https://bridge.home.arpa:8443\"";
    match rest {
        None => errors.push(err(key, &format!("{o:?}"), "not an origin: it must start with https:// or http://", fix)),
        Some(r) if r.is_empty() || r.contains('/') || r.contains(char::is_whitespace) => errors.push(err(
            key,
            &format!("{o:?}"),
            "not an origin: it must not be empty after the scheme and must have no path, trailing slash or spaces",
            fix,
        )),
        Some(_) => {}
    }
}

/// Check one value against its spec. `Ok(())` means the value is usable as is.
fn check_value(spec: &Spec, v: &toml::Value, opts: CheckOptions, errors: &mut Vec<String>) {
    let key = spec.key;
    let shown = show(v);
    let default = spec.default.as_deref().unwrap_or("unset");
    match (spec.kind, v) {
        (Kind::Bind, toml::Value::String(s)) => {
            if s.parse::<SocketAddr>().is_err() {
                let (problem, fix) = bind_problem(s);
                errors.push(err(key, &shown, &problem, &fix));
            }
        }
        (Kind::Bool, toml::Value::Boolean(_)) => {}
        (Kind::UInt { min }, toml::Value::Integer(n)) => {
            if *n < min {
                errors.push(err(
                    key,
                    &shown,
                    &format!("must be at least {min}"),
                    &format!("use a whole number of {min} or more (default {default})"),
                ));
            }
        }
        (Kind::UIntRange { min, max }, toml::Value::Integer(n)) => {
            if *n < min || *n > max {
                errors.push(err(
                    key,
                    &shown,
                    &if key.starts_with("mutation_limit") {
                        format!("mutation limits must be between {min} and {max} per 60 seconds")
                    } else {
                        format!("{key} must be between {min} and {max}")
                    },
                    &format!("use a whole number from {min} to {max} (default {default})"),
                ));
            }
        }
        (Kind::Choice(allowed), toml::Value::String(s)) => {
            if !allowed.contains(&s.as_str()) {
                let list = allowed.iter().map(|a| format!("\"{a}\"")).collect::<Vec<_>>().join(" or ");
                errors.push(err(key, &shown, &format!("must be {list}"), &format!("write {key} = {} (default)", default)));
            }
        }
        (Kind::FilePath, toml::Value::String(s)) => {
            if s.trim().is_empty() {
                errors.push(err(key, &shown, "must not be empty", "set a value, or remove the key"));
            }
        }
        (Kind::Origins, toml::Value::Array(items)) => {
            for (i, item) in items.iter().enumerate() {
                match item {
                    toml::Value::String(s) => check_origin(&format!("{key}[{i}]"), s, errors),
                    other => errors.push(err(
                        &format!("{key}[{i}]"),
                        &show(other),
                        &format!("must be a string, found {}", type_name(other)),
                        "write each origin as a quoted string",
                    )),
                }
            }
        }
        (Kind::StringList, toml::Value::Array(items)) => {
            for (i, item) in items.iter().enumerate() {
                if !item.is_str() {
                    errors.push(err(
                        &format!("{key}[{i}]"),
                        &show(item),
                        &format!("must be a string, found {}", type_name(item)),
                        "write each entry as a quoted string",
                    ));
                }
            }
        }
        (Kind::Table, toml::Value::Table(t)) => {
            for (name, value) in t {
                if !["console_rest", "worker_channel", "health"].contains(&name.as_str()) {
                    errors.push(err(
                        &format!("{key}.{name}"),
                        &show(value),
                        "unknown key",
                        "use console_rest, worker_channel or health",
                    ));
                } else if let toml::Value::String(addr) = value {
                    if addr.parse::<SocketAddr>().is_err() {
                        let (problem, fix) = bind_problem(addr);
                        errors.push(err(&format!("{key}.{name}"), &show(value), &problem, &fix));
                    }
                } else {
                    errors.push(err(
                        &format!("{key}.{name}"),
                        &show(value),
                        &format!("must be a string, found {}", type_name(value)),
                        "write host:port with a numeric address, for example \"127.0.0.1:8443\"",
                    ));
                }
            }
        }
        (Kind::Path, toml::Value::String(s)) => {
            if s.is_empty() {
                errors.push(err(
                    key,
                    &shown,
                    "must not be empty",
                    "set the directory that holds index.html, or remove the key to use the default location",
                ));
            } else if opts.check_paths && !Path::new(s).join("index.html").is_file() {
                errors.push(err(
                    key,
                    &shown,
                    &format!("web directory {s} has no index.html"),
                    "point web_dir at the directory with the built web interface, or remove the key to use the default location",
                ));
            }
        }
        (kind, other) => {
            let (want, example) = match kind {
                Kind::Bind => ("a string", format!("\"{}\"", "127.0.0.1:8443")),
                Kind::Bool => ("true or false", "true".to_string()),
                Kind::UInt { .. } | Kind::UIntRange { .. } => ("a whole number", default.to_string()),
                Kind::Choice(_) => ("a string", default.to_string()),
                Kind::FilePath => ("a string", "\"/path/to/file\"".to_string()),
                Kind::Origins => ("a list of strings", "[\"https://bridge.home.arpa:8443\"]".to_string()),
                Kind::Path => ("a string", "\"/path/to/web\"".to_string()),
                Kind::StringList => ("a list of strings", "[\"10.0.0.0/8\"]".to_string()),
                Kind::Table => ("a table", "{ }".to_string()),
            };
            errors.push(err(
                key,
                &shown,
                &format!("must be {want}, found {}", type_name(other)),
                &format!("write {key} = {example} (no quotes around numbers and booleans)"),
            ));
        }
    }
}

fn syntax_error(text: &str, e: &toml::de::Error) -> String {
    let line = e.span().map(|s| text[..s.start.min(text.len())].matches('\n').count() + 1);
    let at = line.map(|l| format!(" (line {l})")).unwrap_or_default();
    let msg = e.message().lines().next().unwrap_or("syntax error");
    err(
        "config.toml",
        "<unparsable>",
        &format!("not valid TOML{at}: {msg}"),
        "correct the syntax at that line; keys are written name = value, strings in double quotes",
    )
}

/// Validate `text` (the contents of `config.toml`) and the environment, returning the effective
/// configuration or every problem found.
pub fn evaluate(text: &str, env: &dyn Fn(&str) -> Option<String>, opts: CheckOptions) -> Result<Config, Vec<String>> {
    let mut errors = Vec::new();
    let table: toml::Table = match text.parse() {
        Ok(t) => t,
        Err(e) => return Err(vec![syntax_error(text, &e)]),
    };
    let specs = specs();
    let known: Vec<&str> = specs.iter().map(|s| s.key).collect();
    for (key, value) in &table {
        match specs.iter().find(|s| s.key == key) {
            Some(spec) => check_value(spec, value, opts, &mut errors),
            None => {
                let (problem, fix) = match suggest(key, &known) {
                    Some(k) => (format!("unknown key; did you mean \"{k}\"?"), format!("rename it to {k}, or remove it")),
                    None => ("unknown key".to_string(), format!("remove it; the known keys are {}", known.join(", "))),
                };
                errors.push(err(key, &show(value), &problem, &fix));
            }
        }
    }

    // Environment overrides win over the file.
    let mut bind_source = ("bind".to_string(), table.get("bind").and_then(|v| v.as_str()).map(str::to_string));
    // Exposure: the environment wins over the file; within the file `exposure` wins over the
    // deprecated `lan_enabled`.
    let legacy_exposure = |lan: bool| if lan { Exposure::Lan } else { Exposure::Loopback };
    let mut exposure_source: (String, Option<Exposure>) = match (
        table
            .get("exposure")
            .and_then(|v| v.as_str())
            .and_then(|v| config::parse_exposure("exposure", v).ok()),
        table.get("lan_enabled").and_then(|v| v.as_bool()),
    ) {
        (Some(e), _) => ("exposure".to_string(), Some(e)),
        (None, Some(lan)) => ("lan_enabled".to_string(), Some(legacy_exposure(lan))),
        (None, None) => ("exposure".to_string(), None),
    };
    if let Some(v) = env(config::ENV_BIND) {
        if v.parse::<SocketAddr>().is_err() {
            let (problem, fix) = bind_problem(&v);
            errors.push(err(
                config::ENV_BIND,
                &format!("{v:?}"),
                &problem,
                &fix.replace("write host:port", "set host:port"),
            ));
        } else {
            bind_source = (config::ENV_BIND.to_string(), Some(v));
        }
    }
    if let Some(v) = env(config::ENV_LAN_ENABLED) {
        match config::parse_bool(config::ENV_LAN_ENABLED, &v) {
            Ok(b) => exposure_source = (config::ENV_LAN_ENABLED.to_string(), Some(legacy_exposure(b))),
            Err(_) => errors.push(err(
                config::ENV_LAN_ENABLED,
                &format!("{v:?}"),
                "not a boolean",
                "set it to true or false (1, 0, yes, no, on and off are also accepted)",
            )),
        }
    }
    if let Some(v) = env(config::ENV_EXPOSURE) {
        match config::parse_exposure(config::ENV_EXPOSURE, &v) {
            Ok(e) => exposure_source = (config::ENV_EXPOSURE.to_string(), Some(e)),
            Err(_) => errors.push(err(
                config::ENV_EXPOSURE,
                &format!("{v:?}"),
                "not an exposure level",
                "set it to loopback, lan or internet",
            )),
        }
    }
    if let Some(v) = env(config::ENV_PUBLIC_PORT).filter(|v| !v.trim().is_empty()) {
        if !v.trim().parse::<u16>().is_ok_and(|p| p != 0) {
            errors.push(err(
                config::ENV_PUBLIC_PORT,
                &format!("{v:?}"),
                "not a port number",
                "set a whole number from 1 to 65535, or unset it to use the bind port",
            ));
        }
    }
    if let Some(v) = env(config::ENV_PUBLIC_NAMES) {
        for item in v.split(',').map(str::trim).filter(|s| !s.is_empty()) {
            let bare = item.trim_start_matches('[').trim_end_matches(']');
            let ok = bare.parse::<IpAddr>().is_ok()
                || (!bare.starts_with(['.', '-']) && bare.len() <= 253 && bare.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '.'));
            if !ok {
                errors.push(err(
                    &format!("{}[{item}]", config::ENV_PUBLIC_NAMES),
                    &format!("{item:?}"),
                    "not a host name or IP address",
                    "use comma-separated DNS names (letters, digits, - and .) and IP addresses",
                ));
            }
        }
    }

    // Operator-managed TLS identity: environment overrides win over the file; all three or none.
    let mut tls_set = [false; 3];
    for (i, (key, env_name)) in [
        ("tls_cert_file", "BRIDGE_COORDINATOR_TLS_CERT_FILE"),
        ("tls_key_file", "BRIDGE_COORDINATOR_TLS_KEY_FILE"),
        ("tls_cert_name", "BRIDGE_COORDINATOR_TLS_CERT_NAME"),
    ]
    .iter()
    .enumerate()
    {
        tls_set[i] = env(env_name).is_some_and(|v| !v.trim().is_empty()) || table.get(*key).is_some();
    }
    if tls_set.iter().any(|set| *set) && !tls_set.iter().all(|set| *set) {
        errors.push(err(
            "tls_cert_file",
            "<partly set>",
            "tls_cert_file, tls_key_file and tls_cert_name must be configured together",
            "set all three (in config.toml or BRIDGE_COORDINATOR_TLS_CERT_FILE, _KEY_FILE and _CERT_NAME), or none",
        ));
    }

    // Cross-key checks, only when each value involved is itself valid.
    let effective = |k: &str, default: u64| match table.get(k) {
        None => Some(default as i64),
        Some(v) => v.as_integer().filter(|n| *n >= 1),
    };
    let d = Config::default();
    if let (Some(ping), Some(idle)) = (
        effective("ws_ping_interval_secs", d.ws_ping_interval_secs),
        effective("ws_idle_timeout_secs", d.ws_idle_timeout_secs),
    ) {
        if idle <= ping {
            errors.push(err(
                "ws_idle_timeout_secs",
                &idle.to_string(),
                &format!("must be larger than ws_ping_interval_secs ({ping})"),
                "raise ws_idle_timeout_secs or lower ws_ping_interval_secs",
            ));
        }
    }
    let bind_text = bind_source.1.clone().unwrap_or_else(|| Config::default().bind);
    let groups: Vec<(String, String)> = table
        .get("listener_groups")
        .and_then(|v| v.as_table())
        .map(|t| {
            t.iter()
                .filter_map(|(k, v)| v.as_str().map(|a| (format!("listener_groups.{k}"), a.to_string())))
                .collect()
        })
        .unwrap_or_default();
    if opts.require_lan {
        let exposure = exposure_source.1.unwrap_or(Exposure::Loopback);
        let exposure_valid = table
            .get("exposure")
            .is_none_or(|v| v.as_str().is_some_and(|s| config::parse_exposure("exposure", s).is_ok()))
            && table.get("lan_enabled").is_none_or(|v| v.is_bool());
        let by = if exposure_source.1.is_some() {
            format!("set by {}", exposure_source.0)
        } else {
            "the default".to_string()
        };
        let checked: Vec<(String, String)> = if groups.is_empty() {
            vec![(bind_source.0.clone(), bind_text.clone())]
        } else {
            groups.clone()
        };
        for (key, text) in checked {
            if let Ok(addr) = text.parse::<SocketAddr>() {
                if !addr.ip().is_loopback() && exposure == Exposure::Loopback && exposure_valid {
                    errors.push(err(
                        &key,
                        &format!("{text:?}"),
                        &format!("{addr} is not a loopback address and exposure is loopback ({by})"),
                        "set exposure = \"lan\" in config.toml (or BRIDGE_COORDINATOR_EXPOSURE=lan) to serve the LAN, or bind a loopback address such as \"127.0.0.1:8443\"",
                    ));
                }
            }
        }
    }
    if let Some(toml::Value::Array(items)) = table.get("trusted_proxies") {
        let strings: Vec<&str> = items.iter().filter_map(|v| v.as_str()).collect();
        if strings.len() == items.len() && (strings.len() > 64 || strings.iter().any(|c| !crate::client_ip::valid_cidr(c))) {
            errors.push(err(
                "trusted_proxies",
                &show(&toml::Value::Array(items.clone())),
                "trusted_proxies must contain at most 64 valid IPv4 or IPv6 CIDRs",
                "write each proxy as address/prefix, for example \"10.0.0.0/8\" or \"2001:db8::/32\"",
            ));
        }
    }
    if let Some(origin) = table.get("public_origin").and_then(|v| v.as_str()).filter(|v| !v.trim().is_empty()) {
        match crate::passkeys::canonical_origin(origin) {
            Err(e) => errors.push(err(
                "public_origin",
                &format!("{origin:?}"),
                &e,
                "write an https:// origin with a DNS name, for example \"https://bridge.example.test\"",
            )),
            Ok(canonical) if canonical != origin => errors.push(err(
                "public_origin",
                &format!("{origin:?}"),
                &format!("public_origin must use its canonical form {canonical:?}"),
                &format!("write public_origin = {canonical:?}"),
            )),
            Ok(_) => {}
        }
    }
    if let Some(webhook) = table.get("security_webhook_url").and_then(|v| v.as_str()).filter(|v| !v.trim().is_empty()) {
        let ok = url::Url::parse(webhook).is_ok_and(|u| u.scheme() == "https" && u.host_str().is_some() && u.username().is_empty() && u.password().is_none());
        if !ok {
            errors.push(err(
                "security_webhook_url",
                &format!("{webhook:?}"),
                "security_webhook_url must be an HTTPS URL without embedded credentials",
                "write an https:// URL without a user name or password",
            ));
        }
    }

    if !errors.is_empty() {
        return Err(errors);
    }
    let mut table = table;
    table.remove("lan_enabled");
    let mut cfg: Config = toml::Value::Table(table)
        .try_into()
        .map_err(|e: toml::de::Error| vec![err("config.toml", "<table>", e.message(), "fix the value named above")])?;
    if let Some(b) = bind_source.1 {
        cfg.bind = b;
    }
    if let Some(e) = exposure_source.1 {
        cfg.exposure = e;
    }
    for (env_name, slot) in [
        ("BRIDGE_COORDINATOR_TLS_CERT_FILE", &mut cfg.tls_cert_file),
        ("BRIDGE_COORDINATOR_TLS_KEY_FILE", &mut cfg.tls_key_file),
        ("BRIDGE_COORDINATOR_TLS_CERT_NAME", &mut cfg.tls_cert_name),
    ] {
        if let Some(v) = env(env_name).filter(|v| !v.trim().is_empty()) {
            *slot = Some(v);
        }
    }
    cfg.validate_internet_static().map_err(|e| {
        vec![err(
            "exposure",
            "\"internet\"",
            &e,
            "satisfy each listed requirement, or use exposure = \"lan\"",
        )]
    })?;
    Ok(cfg)
}

/// [`evaluate`] with the real process environment.
pub fn evaluate_env(text: &str, opts: CheckOptions) -> Result<Config, Vec<String>> {
    evaluate(text, &|n| std::env::var_os(n).map(|v| v.to_string_lossy().into_owned()), opts)
}
