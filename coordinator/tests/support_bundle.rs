mod common;

use bridge_coordinator::{bundle, config::Config, db::Db};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::path::PathBuf;

struct Fixture(PathBuf);
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
fn seed() -> (Fixture, Vec<String>) {
    let dir = common::tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("config.toml"),
        r#"
bind = "192.168.77.12:8443"
exposure = "lan"
security_webhook_url = "https://hooks.example.test/hook/TOKENSECRET0123456789"
public_origin = "https://bridge-secret-name.example.test"
allowed_origins = ["https://bridge-secret-name.example.test:8443"]
trusted_proxies = ["10.9.8.0/24"]
backup_dir = "/home/secretuser/backups"
"#,
    )
    .unwrap();
    Config::load(&dir).expect("seed config must load");
    let path = bridge_coordinator::db_path(&dir);
    drop(Db::open(&path).unwrap());
    let c = Connection::open(path).unwrap();
    c.execute_batch(
        r#"
INSERT INTO workers(name,token_hash,created_at) VALUES('worker-secret-name','WTOKHASH-SEED-0123456789abcdef',1);
UPDATE workers SET host_name='secret-host.lan',remote_address='192.168.77.50' WHERE name='worker-secret-name';
INSERT INTO admin(id,username,password_hash,created_at) VALUES(1,'seed-admin','$argon2id$v=19$m=19456,t=2,p=1$SEEDSALTSEEDSALT$SEEDHASHVALUE0123456789',1);
INSERT INTO api_tokens(name,token_hash,created_at) VALUES('seed-token','APITOKHASH-SEED-0123456789abcdef',1);
INSERT INTO meta(key,value) VALUES('totp_secret','JBSWY3DPEHPK3PXP-SEEDTOTP');
INSERT INTO missions(worker,mission_id,info) VALUES('worker-secret-name','m1','{"prompt":"PROMPT rotate the seeded launch codes"}');
INSERT INTO audit(ts,actor,action,detail,remote) VALUES(1,'seed-admin','login','seeded','192.168.77.51');
"#,
    )
    .unwrap();
    drop(c);
    let logs = [
        "Authorization: Bearer abcdef0123456789abcdef0123456789",
        "connection from 192.168.77.50 accepted",
        "login user=seed-admin password=hunter2hunter2",
        "prompt: rotate the seeded launch codes",
        "https://hooks.example.test/hook/TOKENSECRET0123456789",
        "owner@secret-host.lan",
        "api_key=AKIAexample0123 rejected",
        "session=sess_abcdef0123456789 expired",
        "password_hash=$argon2id$v=19$m=19456,t=2,p=1$SEEDSALT$SEEDHASH",
        "opened /home/alice/projects/secret/notes.txt",
        "worker worker-secret-name connected from host secret-host.lan",
        "connect wss://worker.example.test/ws?token=abc failed",
        "GET HTTPS://UPPER.example.test/x failed",
        "digest abcdefabcdefabcdefabcdefabcdefab",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl",
        "Authorization: Basic dXNlcjpwdw==",
        "Basic dTpw",
        "task: fix the login bug in the billing service for ACME",
        "bridge-coordinator: automatic backup coordinator-20260101T000000Z.sqlite3 written (12.3 MiB, 3 old removed)",
        "bridge-coordinator: database quick_check passed in 2.1 s",
    ]
    .iter()
    .enumerate()
    .map(|(n, message)| format!("2026-11-13T21:00:{n:02}+0000 secret-host bridge-coordinator[123]: {message}"))
    .collect();
    (Fixture(dir), logs)
}
fn archive(f: &Fixture, logs: &[String]) -> Vec<(String, Vec<u8>)> {
    let out = f.0.join("support.tar");
    let names = bundle::create(&f.0, &out, logs).unwrap();
    let files = bundle::read_tar(&std::fs::read(out).unwrap()).unwrap();
    assert_eq!(names, files.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>());
    files
}
fn value(files: &[(String, Vec<u8>)], name: &str) -> Value {
    serde_json::from_slice(&files.iter().find(|(n, _)| n.ends_with(name)).unwrap().1).unwrap()
}

#[test]
fn bundle_contains_none_of_the_seeded_secrets() {
    let (f, logs) = seed();
    for (member, data) in archive(&f, &logs) {
        let text = std::str::from_utf8(&data).unwrap();
        for needle in [
            "$argon2id",
            "SEEDHASHVALUE",
            "SEEDSALT",
            "WTOKHASH",
            "APITOKHASH",
            "JBSWY3DPEHPK3PXP",
            "SEEDTOTP",
            "rotate the seeded launch codes",
            "PROMPT",
            "192.168.77",
            "10.9.8",
            "secret-host",
            "secret-name",
            "secretuser",
            "hooks.example.test",
            "TOKENSECRET",
            "abcdef0123456789abcdef",
            "hunter2",
            "seed-admin",
            "owner@",
            "AKIAexample",
            "sess_abcdef",
            "/home/alice",
            "alice",
            "worker-secret-name",
            "worker.example.test",
            "UPPER.example.test",
            "abcdefabcdef",
            "eyJhbGci",
            "dXNlcjpw",
            "dTpw",
            "billing service",
            "ACME",
            "login bug",
            "coordinator-20260101T000000Z",
        ] {
            assert!(!data.windows(needle.len()).any(|bytes| bytes == needle.as_bytes()), "{member} leaked {needle}");
            assert!(
                !text.to_ascii_lowercase().contains(&needle.to_ascii_lowercase()),
                "{member} leaked {needle} case-insensitively"
            );
        }
    }
}

#[test]
fn bundle_has_the_expected_members_and_safe_content() {
    let (f, logs) = seed();
    let files = archive(&f, &logs);
    let names: Vec<_> = files.iter().map(|(n, _)| n.as_str()).collect();
    assert_eq!(
        names,
        [
            "bridge-support-bundle/manifest.json",
            "bridge-support-bundle/version.json",
            "bridge-support-bundle/diagnostics.json",
            "bridge-support-bundle/config.json",
            "bridge-support-bundle/database.json",
            "bridge-support-bundle/logs.txt"
        ]
    );
    let version = value(&files, "version.json");
    assert_eq!(version["coordinator"], env!("CARGO_PKG_VERSION"));
    assert_eq!(version["schema_version_expected"], bridge_coordinator::db::SCHEMA_VERSION);
    let config = value(&files, "config.json");
    assert_eq!(config["bind"], json!({"scope":"private", "port":8443}));
    assert_eq!(config["exposure"], "lan");
    assert_eq!(config["backup_dir"], json!({"set":true}));
    let db = value(&files, "database.json");
    assert_eq!(db["tables"]["admin"], 1);
    assert_eq!(db["tables"]["workers"], 1);
    assert_eq!(db["user_version"], bridge_coordinator::db::SCHEMA_VERSION);
    let logs = String::from_utf8(files.last().unwrap().1.clone()).unwrap();
    assert!(logs.contains("backup_written 1"));
    assert!(logs.contains("# event histogram (all records read)"));
    assert!(!logs.lines().skip(1).any(|s| s.to_ascii_lowercase().contains("prompt")));
    let manifest = value(&files, "manifest.json");
    assert_eq!(manifest["format"], 1);
    assert_eq!(manifest["members"].as_array().unwrap().len(), 5);
}

#[test]
fn the_database_file_is_not_a_member_and_no_member_is_sqlite() {
    let (f, logs) = seed();
    for (name, data) in archive(&f, &logs) {
        assert!(!name.ends_with(".sqlite3"));
        assert!(!data.starts_with(b"SQLite format 3"));
    }
}

#[test]
fn config_view_omits_unknown_settings() {
    let view = bundle::config_view(&json!({"update_check":true,"future_secret_setting":"x","bind":"127.0.0.1:8443"}));
    assert_eq!(view["future_secret_setting"], json!({"omitted":true}));
    assert_eq!(view["update_check"], true);
    assert_eq!(view["bind"]["scope"], "loopback");
    let cfg = serde_json::to_value(Config::default()).unwrap();
    let view = bundle::config_view(&cfg);
    assert_eq!(view.as_object().unwrap().len(), cfg.as_object().unwrap().len());
    for key in cfg.as_object().unwrap().keys() {
        assert!(view.get(key).is_some());
    }
    assert!(!view.to_string().contains("127.0.0.1"));
    let malformed = bundle::config_view(&json!({"update_check":"secret", "exposure":"secret", "min_tls":"secret", "bind":"secret"}));
    for v in malformed.as_object().unwrap().values() {
        assert_eq!(v, &json!({"omitted":true}));
    }
    for (addr, scope) in [
        ("[::1]:9", "loopback"),
        ("[fc00::1]:9", "private"),
        ("[fe80::1]:9", "private"),
        ("8.8.8.8:9", "other"),
    ] {
        assert_eq!(bundle::config_view(&json!({"bind":addr}))["bind"]["scope"], scope);
    }
}

#[test]
fn diagnostics_view_keeps_numbers_and_drops_names() {
    let input = json!({"status":"warn","checks":{"command_queue_depth":{"value":{"worker secret name":{"queued":3}},"status":"ok","fix":null},
        "uptime_seconds":{"value":12,"status":"ok","fix":null}},"backup":{"dir":"/home/secretuser/b","last_result":"ok","count":2}});
    let view = bundle::diagnostics_view(&input);
    assert_eq!(view["checks"]["uptime_seconds"]["value"], 12);
    assert_eq!(view["backup"]["dir"], "[omitted]");
    assert_eq!(view["backup"]["last_result"], "ok");
    assert_eq!(view["backup"]["count"], 2);
    assert!(!view.to_string().contains("secret"));
    assert_eq!(view["checks"]["command_queue_depth"]["value"]["omitted_keys"], 1);
    let v =
        bundle::diagnostics_view(&json!({"status":"secret", "coordinator":"invalid/version", "sqlite":"3.41.0", "fix":"static advice", "values": vec![1; 70]}));
    assert_eq!(v["status"], "[omitted]");
    assert_eq!(v["coordinator"], "[omitted]");
    assert_eq!(v["sqlite"], "3.41.0");
    assert_eq!(v["fix"], "static advice");
    assert_eq!(v["values"].as_array().unwrap().len(), 64);
}

#[test]
fn scrub_line_table() {
    for (line, expected) in [
        ("api_key=AKIAexample0123 rejected", Some("api_key=[redacted] rejected")),
        ("session=sess_abcdef0123456789 expired", Some("session=[redacted] expired")),
        (
            "password_hash=$argon2id$v=19$m=19456,t=2,p=1$SEEDSALT$SEEDHASH",
            Some("password_hash=[redacted],t=2,p=1$SEEDSALT$SEEDHASH"),
        ),
        ("x-auth-token: abc", Some("x-auth-token: [redacted]")),
        ("XCredentialID=value", Some("XCredentialID=[redacted]")),
        ("\"api_key\":\"abc\"", Some("\"api_key\":\"[redacted]\"")),
        ("Authorization: Basic dXNlcjpwdw==", Some("Authorization: [redacted]")),
        ("aUtHoRiZaTiOn: Bearer x y", Some("aUtHoRiZaTiOn: [redacted]")),
        ("Basic dTpw", Some("Basic [redacted]")),
        ("bAsIc x", Some("bAsIc [redacted]")),
        ("wss://worker.example.test/ws?token=abc failed", Some("[url] failed")),
        ("GET HTTPS://UPPER.example.test/x failed", Some("GET [url] failed")),
        ("ftp://example.test/file", Some("[url]")),
        ("opened /home/alice/projects/secret/notes.txt", Some("opened [path]")),
        ("/Users/alice/file /root/.ssh/key /root", Some("[path] [path] [path]")),
        ("abcdefabcdefabcdefabcdefabcdefab", Some("[redacted]")),
        ("ABCDEFABCDEFABCDEFABCDEFABCDEFABCD", Some("[redacted]")),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl", Some("jwt [redacted]")),
        ("PROMPT is sensitive", None),
        ("unprompted", None),
        ("visit https://example.test/token now", Some("visit [url] now")),
        ("http://example.test", Some("[url]")),
        ("mail owner@example.test now", Some("mail [email] now")),
        ("Bearer shorttoken", Some("Bearer [redacted]")),
        ("token=abc", Some("token=[redacted]")),
        ("PASSWORD: abc", Some("PASSWORD: [redacted]")),
        ("\"secret\":\"abc\"", Some("\"secret\":\"[redacted]\"")),
        (
            "passwd=x totp=x authorization=x cookie=x key=x hash=x",
            Some("passwd=[redacted] totp=[redacted] authorization=[redacted] cookie=[redacted] key=[redacted] hash=[redacted]"),
        ),
        ("connection from 192.168.77.50 accepted", Some("connection from [address] accepted")),
        ("ip [2001:db8::1]", Some("ip [[address]]")),
        ("ip ::1", Some("ip [address]")),
        ("abcdef0123456789abcdef", Some("[redacted]")),
        ("abcdefghijklmnopqrst", Some("abcdefghijklmnopqrst")),
        ("backup written (3 old removed)", Some("backup written (3 old removed)")),
        (
            "login user=seed-admin password=hunter2hunter2",
            Some("login user=[redacted] password=[redacted]"),
        ),
    ] {
        assert_eq!(bundle::scrub_line(line).as_deref(), expected, "input {line}");
    }
    assert_eq!(bundle::scrub_line(&"é".repeat(401)).unwrap().chars().count(), 400);
}

#[test]
fn tar_round_trips_and_refuses_to_overwrite() {
    let dir = common::tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let f = Fixture(dir);
    let out = f.0.join("test.tar");
    let files = vec![("empty".into(), vec![]), ("sample".into(), vec![b'x'; 513])];
    bundle::write_tar(&out, &files).unwrap();
    let bytes = std::fs::read(&out).unwrap();
    let expected: Vec<_> = files.iter().map(|(n, b)| (format!("bridge-support-bundle/{n}"), b.clone())).collect();
    assert_eq!(bundle::read_tar(&bytes).unwrap(), expected);
    assert!(bundle::write_tar(&out, &files).unwrap_err().contains("refusing to overwrite"));
    assert_eq!(std::fs::read(&out).unwrap(), bytes);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&out).unwrap().permissions().mode() & 0o777, 0o600);
    }
    assert!(bundle::read_tar(&bytes[..bytes.len() - 100]).is_err());
    for end in [0, 511, 512, 1024, bytes.len() - 512] {
        assert!(bundle::read_tar(&bytes[..end]).is_err());
    }
    let bad = f.0.join("invalid.tar");
    assert!(bundle::write_tar(&bad, &[("../escape".into(), vec![])]).is_err());
    assert!(!bad.exists(), "partial output must be removed");
    let mut corrupt = bytes.clone();
    corrupt[0] ^= 1;
    assert!(bundle::read_tar(&corrupt).is_err());
}

#[test]
fn failures_are_generic_and_surviving_logs_are_bounded() {
    let dir = common::tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    let f = Fixture(dir);
    std::fs::write(f.0.join("config.toml"), "secret invalid configuration").unwrap();
    let mut lines = vec!["backup written".to_string(); 305];
    lines.extend(vec!["prompt hidden".to_string(); 20]);
    let files = bundle::members(&f.0, &lines, 123);
    let config: Value = serde_json::from_slice(&files[3].1).unwrap();
    assert_eq!(
        config["error"],
        "config.toml could not be read or is invalid; run bridge-coordinator check-config"
    );
    assert_eq!(
        bundle::database_view(&f.0.join("missing.sqlite3")),
        json!({"error":"database could not be read"})
    );
    assert!(!f.0.join("missing.sqlite3").exists());
    let manifest: Value = serde_json::from_slice(&files[0].1).unwrap();
    assert_eq!(manifest["created_at"], 123);
    let logs = String::from_utf8_lossy(&files[5].1);
    let last = logs.split("# last 300 records\n").nth(1).unwrap();
    assert_eq!(last.lines().count(), 300);
    assert!(logs.contains("unrecognized 325"));
}

#[test]
fn bundle_replaces_diagnostic_advice_that_interpolates_private_data() {
    let (f, _) = seed();
    let backups = f.0.join("secretuser-backups");
    std::fs::create_dir_all(&backups).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&backups, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    std::fs::write(f.0.join("config.toml"), format!("backup_dir = {}\n", json!(backups.to_str().unwrap()))).unwrap();
    std::fs::write(
        backups.join("backup-status.json"),
        json!({
            "attempted_at": bridge_coordinator::now(), "result": "failed",
            "reason": "secretuser path /private/secret-name and token TOKENSECRET", "file": null, "bytes": null
        })
        .to_string(),
    )
    .unwrap();
    let cfg = Config::load(&f.0).unwrap();
    let raw = bridge_coordinator::diagnostics::collect_read_only(&f.0, &cfg).unwrap();
    assert!(raw.to_string().contains("TOKENSECRET"), "exercise interpolated fix text");
    let files = archive(&f, &[]);
    let diagnostics = value(&files, "diagnostics.json").to_string();
    for needle in ["secretuser", "secret-name", "TOKENSECRET"] {
        assert!(!diagnostics.contains(needle), "diagnostics leaked {needle}");
    }
}

#[test]
fn worker_names_that_look_like_identifiers_are_not_copied_as_keys() {
    let (f, logs) = seed();
    let c = Connection::open(bridge_coordinator::db_path(&f.0)).unwrap();
    c.execute_batch(
        "INSERT INTO workers(name,token_hash,created_at) VALUES('alicelaptop','ALICETOKHASH-0123456789abcdef',1);
         INSERT INTO commands(request_id,worker,operation,mutating,request,state,created_at,updated_at)
         VALUES('r1','alicelaptop','status',0,'{}','queued',1,1);",
    )
    .unwrap();
    drop(c);
    let files = archive(&f, &logs);
    let diagnostics = files.iter().find(|(n, _)| n.ends_with("diagnostics.json")).unwrap();
    let text = String::from_utf8_lossy(&diagnostics.1).to_string();
    assert!(!text.contains("alicelaptop"), "{text}");
    assert!(text.contains("worker_1"), "{text}");
}

#[test]
fn log_member_has_only_ids_levels_and_timestamps() {
    let (f, logs) = seed();
    let files = archive(&f, &logs);
    let text = String::from_utf8_lossy(&files.last().unwrap().1);
    let known = |id: &str| id == "unrecognized" || bundle::LOG_EVENTS.iter().any(|e| e.id == id);
    for line in text.lines().filter(|line| !line.starts_with('#')) {
        let parts: Vec<_> = line.split_whitespace().collect();
        match parts.as_slice() {
            [id, count] => {
                assert!(known(id), "{line}");
                assert!(count.parse::<usize>().is_ok(), "{line}");
            }
            [timestamp, level, target, id] => {
                assert!(
                    *timestamp == "unknown"
                        || ((19..=32).contains(&timestamp.len()) && timestamp.bytes().all(|b| b.is_ascii_digit() || b"-T:+.Z".contains(&b))),
                    "{line}"
                );
                assert!(["info", "warn", "error", "unknown"].contains(level), "{line}");
                assert_eq!(*target, "bridge-coordinator");
                assert!(known(id), "{line}");
            }
            _ => panic!("unexpected log line: {line}"),
        }
    }
}

#[test]
fn unrecognized_lines_are_counted_not_copied() {
    let lines = vec![
        "private text".into(),
        "prompt secret".into(),
        "2026-11-13T21:00:00+0000 private-host bridge-coordinator[123]: private text".into(),
    ];
    let text = bundle::render_logs(&lines);
    assert!(text.contains("unrecognized 3"));
    assert!(!text.contains("private"));
    assert!(!text.contains("prompt"));
    let records = bundle::log_records(&lines);
    assert_eq!(records[0].timestamp, "unknown");
    assert_eq!(records[2].timestamp, "2026-11-13T21:00:00+0000");
}

#[test]
fn every_eprintln_in_the_source_has_an_allow_listed_event() {
    let mut failures = Vec::new();
    for entry in std::fs::read_dir(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|ext| ext != "rs") {
            continue;
        }
        let source = std::fs::read_to_string(&path).unwrap();
        for (offset, _) in source.match_indices("eprintln!(") {
            let tail = source[offset + "eprintln!(".len()..].trim_start();
            // Empty invocations have no message; all other invocations use a literal.
            if tail.starts_with(')') {
                continue;
            }
            let literal = tail.strip_prefix('"').expect("eprintln must start with a string literal");
            let prefix = literal.split(['{', '"']).next().unwrap();
            let prefix = prefix.strip_prefix("bridge-coordinator: ").unwrap_or(prefix);
            if prefix.is_empty() {
                continue;
            }
            if !bundle::LOG_EVENTS.iter().any(|e| !e.prefix.is_empty() && prefix.starts_with(e.prefix)) {
                failures.push(format!("{}:{}: {prefix:?}", path.display(), source[..offset].lines().count()));
            }
        }
    }
    assert!(failures.is_empty(), "missing log events:\n{}", failures.join("\n"));
    for (index, event) in bundle::LOG_EVENTS.iter().enumerate() {
        assert!(!event.prefix.is_empty());
        assert!(event.id.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'));
        assert!(["info", "warn", "error"].contains(&event.level));
        assert!(!bundle::LOG_EVENTS[..index].iter().any(|e| e.prefix == event.prefix || e.id == event.id));
    }
}

#[test]
fn log_histogram_covers_all_records_and_orders_ties_by_id() {
    let mut lines = vec!["shutting down".into(); 301];
    lines.extend(["database error: secret".into(), "error: secret".into()]);
    let text = bundle::render_logs(&lines);
    assert!(text.contains("shutdown 301\ncommand_error 1\ndatabase_error 1\n"));
    assert_eq!(text.split("# last 300 records\n").nth(1).unwrap().lines().count(), 300);
    let record = bundle::log_records(&["backup: running at idle".into()]);
    assert_eq!(record[0].event, "backup_started", "longest prefix must win");
}

#[test]
fn log_records_validate_timestamps_and_never_copy_message_values() {
    let lines = [
        "bridge-coordinator: automatic backup private-name written (secret)",
        "2026-11-13T21:00:00+0000 host bridge-coordinator[987]: bridge-coordinator: automatic backup private-name written (secret)",
        "not-a-timestamp host bridge-coordinator[987]: shutting down",
        "2026-11-13T21:00:00+0000",
        "",
    ]
    .map(str::to_string);
    let records = bundle::log_records(&lines);
    assert_eq!(records.len(), lines.len());
    assert_eq!(records[0].timestamp, "unknown");
    assert_eq!(records[0].event, "backup_written");
    assert_eq!(records[1].timestamp, "2026-11-13T21:00:00+0000");
    assert_eq!(records[1].event, "backup_written");
    assert_eq!(records[2].timestamp, "unknown");
    assert_eq!(records[2].event, "unrecognized");
    assert_eq!(records[3].event, "unrecognized");
    assert_eq!(records[4].timestamp, "unknown");
    let text = bundle::render_logs(&lines);
    assert!(!text.contains("private-name"));
    assert!(!text.contains("secret"));
    assert!(!text.contains("987"));
}
