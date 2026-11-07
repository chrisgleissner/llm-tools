//! Table-driven tests for configuration validation (PRD-004): every problem is reported at
//! once as `config error: <key> = <value>: <problem>; fix: <fix>`.

mod common;

use bridge_coordinator::config_check::{evaluate, levenshtein, suggest, CheckOptions};
use common::{sans, tmp_dir};
use std::process::{Command, Output};

struct Row {
    name: &'static str,
    config: &'static str,
    env: &'static [(&'static str, &'static str)],
    /// Exact expected error lines, in order. Empty: the configuration is valid.
    errors: &'static [&'static str],
}

const STRICT: CheckOptions = CheckOptions {
    require_lan: true,
    check_paths: true,
};

const ALL_KEYS: &str = "bind, lan_enabled, min_tls, max_events_per_worker, allowed_origins, web_dir, tls_cert_file, tls_key_file, tls_cert_name, \
max_connections, max_connections_per_ip, worker_reserve_connections, request_body_timeout_secs, http_idle_timeout_secs, ws_ping_interval_secs, \
ws_idle_timeout_secs, mutation_limit_per_token, mutation_limit_per_address";

const ROWS: &[Row] = &[
    Row {
        name: "empty file is valid (all defaults)",
        config: "",
        env: &[],
        errors: &[],
    },
    Row {
        name: "complete valid file",
        config: "bind = \"0.0.0.0:9000\"\nlan_enabled = true\nmax_events_per_worker = 10\nallowed_origins = [\"https://bridge.home.arpa:8443\"]\n\
                 max_connections = 8\nmax_connections_per_ip = 2\nworker_reserve_connections = 0\nrequest_body_timeout_secs = 5\n\
                 http_idle_timeout_secs = 5\nws_ping_interval_secs = 5\nws_idle_timeout_secs = 6\n",
        env: &[],
        errors: &[],
    },
    Row {
        name: "unknown key with a did-you-mean",
        config: "max_connection = 10\n",
        env: &[],
        errors: &["config error: max_connection = 10: unknown key; did you mean \"max_connections\"?; fix: rename it to max_connections, or remove it"],
    },
    Row {
        name: "unknown key that lacks a suffix of a longer key",
        config: "ws_ping_interval = 10\n",
        env: &[],
        errors: &["config error: ws_ping_interval = 10: unknown key; did you mean \"ws_ping_interval_secs\"?; fix: rename it to ws_ping_interval_secs, or remove it"],
    },
    Row {
        name: "unknown key with no close match lists the known keys",
        config: "colour = \"red\"\n",
        env: &[],
        errors: &["@UNKNOWN_COLOUR"],
    },
    Row {
        name: "several problems of different kinds are all reported, in key order",
        config: "bnd = \"127.0.0.1:8443\"\nlan_enabled = \"yes\"\nmax_connections = 0\nmax_events_per_worker = -5\nhttp_idle_timeout_secs = 1.5\n",
        env: &[],
        errors: &[
            "config error: bnd = \"127.0.0.1:8443\": unknown key; did you mean \"bind\"?; fix: rename it to bind, or remove it",
            "config error: http_idle_timeout_secs = 1.5: must be a whole number, found a decimal number; fix: write http_idle_timeout_secs = 30 (no quotes around numbers and booleans)",
            "config error: lan_enabled = \"yes\": must be true or false, found a string; fix: write lan_enabled = true (no quotes around numbers and booleans)",
            "config error: max_connections = 0: must be at least 1; fix: use a whole number of 1 or more (default 256)",
            "config error: max_events_per_worker = -5: must be at least 1; fix: use a whole number of 1 or more (default 50000)",
        ],
    },
    Row {
        name: "bind without a port",
        config: "bind = \"127.0.0.1\"\n",
        env: &[],
        errors: &["config error: bind = \"127.0.0.1\": not a valid listen address (invalid socket address syntax); fix: add a port, for example \"127.0.0.1:8443\""],
    },
    Row {
        name: "bind with a host name",
        config: "bind = \"localhost:8443\"\n",
        env: &[],
        errors: &["config error: bind = \"localhost:8443\": not a valid listen address (invalid socket address syntax); fix: the host must be an IP address, not a name (use 127.0.0.1, a LAN address, 0.0.0.0 or [::]), for example \"127.0.0.1:8443\""],
    },
    Row {
        name: "bind with a port out of range",
        config: "bind = \"127.0.0.1:99999\"\n",
        env: &[],
        errors: &["config error: bind = \"127.0.0.1:99999\": not a valid listen address (invalid socket address syntax); fix: the port must be a whole number from 0 to 65535, for example \"127.0.0.1:8443\""],
    },
    Row {
        name: "bind of the wrong type",
        config: "bind = 8443\n",
        env: &[],
        errors: &["config error: bind = 8443: must be a string, found a whole number; fix: write bind = \"127.0.0.1:8443\" (no quotes around numbers and booleans)"],
    },
    Row {
        name: "non-loopback bind without lan_enabled",
        config: "bind = \"0.0.0.0:8443\"\n",
        env: &[],
        errors: &["config error: bind = \"0.0.0.0:8443\": 0.0.0.0:8443 is not a loopback address and lan_enabled is not true (it is false in lan_enabled, or unset); fix: set lan_enabled = true in config.toml (or BRIDGE_COORDINATOR_LAN_ENABLED=true) to serve the LAN, or bind a loopback address such as \"127.0.0.1:8443\""],
    },
    Row {
        name: "ws_idle_timeout_secs must exceed ws_ping_interval_secs",
        config: "ws_ping_interval_secs = 30\nws_idle_timeout_secs = 30\n",
        env: &[],
        errors: &["config error: ws_idle_timeout_secs = 30: must be larger than ws_ping_interval_secs (30); fix: raise ws_idle_timeout_secs or lower ws_ping_interval_secs"],
    },
    Row {
        name: "ws ordering is also checked against the default of the other key",
        config: "ws_ping_interval_secs = 120\n",
        env: &[],
        errors: &["config error: ws_idle_timeout_secs = 90: must be larger than ws_ping_interval_secs (120); fix: raise ws_idle_timeout_secs or lower ws_ping_interval_secs"],
    },
    Row {
        name: "allowed_origins entries must be origins",
        config: "allowed_origins = [\"bridge.home.arpa\", \"https://ok.example:8443\", \"https://x.example/path\", 7]\n",
        env: &[],
        errors: &[
            "config error: allowed_origins[0] = \"bridge.home.arpa\": not an origin: it must start with https:// or http://; fix: use an exact origin, scheme://host[:port] without a path, for example \"https://bridge.home.arpa:8443\"",
            "config error: allowed_origins[2] = \"https://x.example/path\": not an origin: it must not be empty after the scheme and must have no path, trailing slash or spaces; fix: use an exact origin, scheme://host[:port] without a path, for example \"https://bridge.home.arpa:8443\"",
            "config error: allowed_origins[3] = 7: must be a string, found a whole number; fix: write each origin as a quoted string",
        ],
    },
    Row {
        name: "allowed_origins of the wrong type",
        config: "allowed_origins = \"https://a.example\"\n",
        env: &[],
        errors: &["config error: allowed_origins = \"https://a.example\": must be a list of strings, found a string; fix: write allowed_origins = [\"https://bridge.home.arpa:8443\"] (no quotes around numbers and booleans)"],
    },
    Row {
        name: "web_dir without index.html",
        config: "web_dir = \"/nonexistent/web\"\n",
        env: &[],
        errors: &["config error: web_dir = \"/nonexistent/web\": web directory /nonexistent/web has no index.html; fix: point web_dir at the directory with the built web interface, or remove the key to use the default location"],
    },
    Row {
        name: "empty web_dir",
        config: "web_dir = \"\"\n",
        env: &[],
        errors: &["config error: web_dir = \"\": must not be empty; fix: set the directory that holds index.html, or remove the key to use the default location"],
    },
    Row {
        name: "TOML syntax error",
        config: "bind = \"127.0.0.1:8443\"\nlan_enabled = \n",
        env: &[],
        errors: &["@SYNTAX"],
    },
    Row {
        name: "environment overrides with bad values are reported with the variable name",
        config: "",
        env: &[
            ("BRIDGE_COORDINATOR_BIND", "example.org:8443"),
            ("BRIDGE_COORDINATOR_LAN_ENABLED", "maybe"),
            ("BRIDGE_COORDINATOR_PUBLIC_PORT", "70000"),
            ("BRIDGE_COORDINATOR_PUBLIC_NAMES", "bridge.home.arpa, bad name,192.168.1.20"),
        ],
        errors: &[
            "config error: BRIDGE_COORDINATOR_BIND = \"example.org:8443\": not a valid listen address (invalid socket address syntax); fix: the host must be an IP address, not a name (use 127.0.0.1, a LAN address, 0.0.0.0 or [::]), for example \"127.0.0.1:8443\"",
            "config error: BRIDGE_COORDINATOR_LAN_ENABLED = \"maybe\": not a boolean; fix: set it to true or false (1, 0, yes, no, on and off are also accepted)",
            "config error: BRIDGE_COORDINATOR_PUBLIC_PORT = \"70000\": not a port number; fix: set a whole number from 1 to 65535, or unset it to use the bind port",
            "config error: BRIDGE_COORDINATOR_PUBLIC_NAMES[bad name] = \"bad name\": not a host name or IP address; fix: use comma-separated DNS names (letters, digits, - and .) and IP addresses",
        ],
    },
    Row {
        name: "good environment overrides win over the file (container: lan from the environment)",
        config: "bind = \"127.0.0.1:8443\"\nlan_enabled = false\n",
        env: &[
            ("BRIDGE_COORDINATOR_BIND", "0.0.0.0:8443"),
            ("BRIDGE_COORDINATOR_LAN_ENABLED", "true"),
            ("BRIDGE_COORDINATOR_PUBLIC_PORT", "18443"),
            ("BRIDGE_COORDINATOR_PUBLIC_NAMES", "localhost,bridge.home.arpa"),
        ],
        errors: &[],
    },
    Row {
        name: "environment lan_enabled=false refuses a non-loopback bind from the file",
        config: "bind = \"0.0.0.0:8443\"\nlan_enabled = true\n",
        env: &[("BRIDGE_COORDINATOR_LAN_ENABLED", "false")],
        errors: &["config error: bind = \"0.0.0.0:8443\": 0.0.0.0:8443 is not a loopback address and lan_enabled is not true (it is false in BRIDGE_COORDINATOR_LAN_ENABLED, or unset); fix: set lan_enabled = true in config.toml (or BRIDGE_COORDINATOR_LAN_ENABLED=true) to serve the LAN, or bind a loopback address such as \"127.0.0.1:8443\""],
    },
    Row {
        name: "file and environment problems are reported together",
        config: "max_connection = 3\n",
        env: &[("BRIDGE_COORDINATOR_LAN_ENABLED", "2")],
        errors: &[
            "config error: max_connection = 3: unknown key; did you mean \"max_connections\"?; fix: rename it to max_connections, or remove it",
            "config error: BRIDGE_COORDINATOR_LAN_ENABLED = \"2\": not a boolean; fix: set it to true or false (1, 0, yes, no, on and off are also accepted)",
        ],
    },
    Row {
        name: "min_tls other than 1.3 or 1.2 is refused",
        config: "min_tls = \"1.1\"\n",
        env: &[],
        errors: &["config error: min_tls = \"1.1\": must be \"1.3\" or \"1.2\"; fix: write min_tls = \"1.3\" (default)"],
    },
    Row {
        name: "mutation limits outside 1 to 10000 are refused",
        config: "mutation_limit_per_token = 0\nmutation_limit_per_address = 10001\n",
        env: &[],
        errors: &[
            "config error: mutation_limit_per_address = 10001: mutation limits must be between 1 and 10000 per 60 seconds; fix: use a whole number from 1 to 10000 (default 120)",
            "config error: mutation_limit_per_token = 0: mutation limits must be between 1 and 10000 per 60 seconds; fix: use a whole number from 1 to 10000 (default 30)",
        ],
    },
    Row {
        name: "an operator TLS identity needs the certificate, the key and the name together",
        config: "tls_cert_file = \"/etc/bridge/cert.pem\"\n",
        env: &[],
        errors: &["config error: tls_cert_file = <partly set>: tls_cert_file, tls_key_file and tls_cert_name must be configured together; fix: set all three (in config.toml or BRIDGE_COORDINATOR_TLS_CERT_FILE, _KEY_FILE and _CERT_NAME), or none"],
    },
    Row {
        name: "an operator TLS identity set completely through the file is valid",
        config: "tls_cert_file = \"/etc/bridge/cert.pem\"\ntls_key_file = \"/etc/bridge/key.pem\"\ntls_cert_name = \"bridge.example.ts.net\"\n",
        env: &[],
        errors: &[],
    },
];

fn expected(row: &Row) -> Vec<String> {
    row.errors
        .iter()
        .map(|e| match *e {
            "@UNKNOWN_COLOUR" => format!("config error: colour = \"red\": unknown key; fix: remove it; the known keys are {ALL_KEYS}"),
            "@SYNTAX" => String::new(),
            other => other.to_string(),
        })
        .collect()
}

#[test]
fn table_of_configurations() {
    let mut failures = Vec::new();
    for row in ROWS {
        let env = |n: &str| row.env.iter().find(|(k, _)| *k == n).map(|(_, v)| v.to_string());
        let got: Vec<String> = match evaluate(row.config, &env, STRICT) {
            Ok(_) => vec![],
            Err(e) => e,
        };
        let want = expected(row);
        // The wording of a TOML syntax error comes from the toml crate: check the stable parts.
        let ok = if row.errors == ["@SYNTAX"] {
            got.len() == 1
                && got[0].starts_with("config error: config.toml = <unparsable>: not valid TOML (line 2): ")
                && got[0].ends_with("; fix: correct the syntax at that line; keys are written name = value, strings in double quotes")
        } else {
            got == want
        };
        if !ok {
            failures.push(format!("{}:\n  expected {:#?}\n  got      {:#?}", row.name, want, got));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn valid_configuration_yields_the_effective_values() {
    let env = |n: &str| (n == "BRIDGE_COORDINATOR_BIND").then(|| "127.0.0.1:9999".to_string());
    let cfg = evaluate("bind = \"127.0.0.1:1\"\nmax_connections = 7\n", &env, STRICT).unwrap();
    assert_eq!(cfg.bind, "127.0.0.1:9999", "environment wins");
    assert_eq!(cfg.max_connections, 7);
    assert_eq!(cfg.max_connections_per_ip, 32, "unset keys keep their defaults");
}

#[test]
fn lan_and_path_checks_are_off_without_the_strict_options() {
    let text = "bind = \"0.0.0.0:8443\"\nweb_dir = \"/nonexistent/web\"\n";
    assert!(evaluate(text, &|_| None, CheckOptions::default()).is_ok());
    assert_eq!(evaluate(text, &|_| None, STRICT).unwrap_err().len(), 2);
}

#[test]
fn edit_distance_and_suggestions() {
    assert_eq!(levenshtein("", ""), 0);
    assert_eq!(levenshtein("bind", "bnd"), 1);
    assert_eq!(levenshtein("kitten", "sitting"), 3);
    let known = ["bind", "lan_enabled", "max_connections", "max_connections_per_ip"];
    assert_eq!(suggest("Bind", &known), Some("bind"));
    assert_eq!(suggest("lan-enabled", &known), Some("lan_enabled"));
    assert_eq!(suggest("max_connection", &known), Some("max_connections"));
    assert_eq!(suggest("max_connections_perip", &known), Some("max_connections_per_ip"));
    assert_eq!(suggest("zzzzzz", &known), None);
}

fn run(args: &[&str], envs: &[(&str, &str)]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_bridge-coordinator"))
        .args(args)
        .env_remove("BRIDGE_COORDINATOR_BIND")
        .env_remove("BRIDGE_COORDINATOR_LAN_ENABLED")
        .env_remove("BRIDGE_COORDINATOR_PUBLIC_PORT")
        .env_remove("BRIDGE_COORDINATOR_PUBLIC_NAMES")
        .envs(envs.iter().copied())
        .output()
        .unwrap()
}

fn init_dir() -> std::path::PathBuf {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    dir
}

#[test]
fn check_config_accepts_the_config_written_by_init() {
    let dir = init_dir();
    let o = run(&["check-config", "--data-dir", dir.to_str().unwrap()], &[]);
    let out = String::from_utf8_lossy(&o.stdout);
    assert!(o.status.success(), "{o:?}");
    assert_eq!(out.lines().count(), 1, "{out}");
    assert!(out.starts_with("config ok: ") && out.contains("bind 127.0.0.1:8443"), "{out}");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn check_config_and_serve_report_every_error_and_exit_nonzero() {
    let dir = init_dir();
    common::edit_config(&dir, |t| t + "max_connection = 3\nlan_enable = true\n");
    let d = dir.to_str().unwrap();
    let env = [("BRIDGE_COORDINATOR_LAN_ENABLED", "perhaps")];
    for cmd in [["check-config", "--data-dir", d], ["serve", "--data-dir", d]] {
        let o = run(&cmd, &env);
        let err = String::from_utf8_lossy(&o.stderr);
        assert_eq!(o.status.code(), Some(1), "{cmd:?}: {err}");
        assert!(o.stdout.is_empty(), "nothing is reported as success");
        let lines: Vec<&str> = err.lines().collect();
        assert_eq!(lines.len(), 3, "{err}");
        assert!(lines.iter().all(|l| l.starts_with("config error: ")), "{err}");
        assert!(lines[0].contains("lan_enable =") && lines[0].contains("did you mean \"lan_enabled\"?"), "{err}");
        assert!(
            lines[1].contains("max_connection =") && lines[1].contains("did you mean \"max_connections\"?"),
            "{err}"
        );
        assert!(lines[2].contains("BRIDGE_COORDINATOR_LAN_ENABLED"), "{err}");
    }
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn check_config_without_a_data_directory_says_to_run_init() {
    let dir = tmp_dir();
    let o = run(&["check-config", "--data-dir", dir.to_str().unwrap()], &[]);
    assert_eq!(o.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&o.stderr).contains("run `init` first"), "{o:?}");
}
