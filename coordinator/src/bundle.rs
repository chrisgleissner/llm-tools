//! Redacted support archives. Structured data is copied only through explicit allow-lists.
use crate::config::Config;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};
use std::io::Write;
use std::net::{IpAddr, Ipv6Addr, SocketAddr};
use std::path::Path;
use std::time::Duration;

const PREFIX: &str = "bridge-support-bundle/";
const LOG_HEADER: &str = "# recent coordinator log records reduced to timestamp, level and a fixed event id from an allow-list of the coordinator's static messages; message text, values and host names are never copied";

pub struct LogEvent {
    pub id: &'static str,
    pub level: &'static str,
    pub prefix: &'static str,
}

/// Allow-list of the coordinator's static log messages. `prefix` is the static text up to
/// the first interpolated value, after removing a leading "bridge-coordinator: ".
/// Matching uses `message.starts_with(prefix)`; the longest matching prefix wins.
pub const LOG_EVENTS: &[LogEvent] = &[
    LogEvent {
        id: "database_error",
        level: "error",
        prefix: "database error: ",
    },
    LogEvent {
        id: "passkey_origin_failed",
        level: "warn",
        prefix: "could not persist the canonical passkey origin: ",
    },
    LogEvent {
        id: "notification_persist_failed",
        level: "error",
        prefix: "security notification could not be persisted",
    },
    LogEvent {
        id: "webhook_client_failed",
        level: "error",
        prefix: "security notification webhook client could not be created",
    },
    LogEvent {
        id: "webhook_response",
        level: "info",
        prefix: "security notification webhook returned HTTP ",
    },
    LogEvent {
        id: "webhook_failed",
        level: "warn",
        prefix: "security notification webhook delivery failed",
    },
    LogEvent {
        id: "backup_progress",
        level: "info",
        prefix: "backup: ",
    },
    LogEvent {
        id: "backup_remove_failed",
        level: "warn",
        prefix: "cannot remove old backup ",
    },
    LogEvent {
        id: "backup_status_failed",
        level: "error",
        prefix: "cannot write ",
    },
    LogEvent {
        id: "backup_written",
        level: "info",
        prefix: "automatic backup ",
    },
    LogEvent {
        id: "backups_disabled",
        level: "info",
        prefix: "automatic backups are off (backup_interval_hours = 0)",
    },
    LogEvent {
        id: "backup_panicked",
        level: "error",
        prefix: "automatic backup task panicked",
    },
    LogEvent {
        id: "maintenance_slow",
        level: "warn",
        prefix: "slow database maintenance step ",
    },
    LogEvent {
        id: "database_slow",
        level: "warn",
        prefix: "slow database call ",
    },
    LogEvent {
        id: "audit_failed",
        level: "error",
        prefix: "audit write failed for ",
    },
    LogEvent {
        id: "schema_migration",
        level: "info",
        prefix: "database schema ",
    },
    LogEvent {
        id: "backup_started",
        level: "info",
        prefix: "backup: running at ",
    },
    LogEvent {
        id: "compact_started",
        level: "info",
        prefix: "compact: running at ",
    },
    LogEvent {
        id: "listener_started",
        level: "info",
        prefix: "bridge-coordinator ",
    },
    LogEvent {
        id: "database_free_space",
        level: "warn",
        prefix: "database file free space: ",
    },
    LogEvent {
        id: "shutdown",
        level: "info",
        prefix: "shutting down",
    },
    LogEvent {
        id: "token_scopes",
        level: "info",
        prefix: "note: token scopes: ",
    },
    LogEvent {
        id: "command_error",
        level: "error",
        prefix: "error: ",
    },
    LogEvent {
        id: "quick_check_slow",
        level: "warn",
        prefix: "database quick_check passed in ",
    },
    LogEvent {
        id: "tls_reload_deferred",
        level: "warn",
        prefix: "TLS file certificate reload deferred: ",
    },
    LogEvent {
        id: "maintenance_failed",
        level: "error",
        prefix: "maintenance failed: ",
    },
];

pub struct LogRecord {
    pub timestamp: String,
    pub level: &'static str,
    pub target: &'static str,
    pub event: &'static str,
}

pub fn log_records(lines: &[String]) -> Vec<LogRecord> {
    lines
        .iter()
        .map(|line| {
            let mut fields = line.split_whitespace();
            let first = fields.next().unwrap_or("");
            let valid = (19..=32).contains(&first.len()) && first.bytes().all(|b| b.is_ascii_digit() || b"-T:+.Z".contains(&b));
            let (timestamp, message) = if valid {
                // Discard the host name, then the unit/pid token.
                fields.next();
                fields.by_ref().find(|token| token.ends_with(':'));
                (first, fields.collect::<Vec<_>>().join(" "))
            } else {
                ("unknown", line.clone())
            };
            let message = message.strip_prefix("bridge-coordinator: ").unwrap_or(&message);
            let event = LOG_EVENTS
                .iter()
                .filter(|e| !e.prefix.is_empty() && message.starts_with(e.prefix))
                .max_by_key(|e| e.prefix.len());
            LogRecord {
                timestamp: timestamp.into(),
                level: event.map_or("unknown", |e| e.level),
                target: "bridge-coordinator",
                event: event.map_or("unrecognized", |e| e.id),
            }
        })
        .collect()
}

pub fn render_logs(lines: &[String]) -> String {
    let records = log_records(lines);
    let mut counts = std::collections::BTreeMap::new();
    for record in &records {
        *counts.entry(record.event).or_insert(0usize) += 1;
    }
    let mut counts: Vec<_> = counts.into_iter().collect();
    counts.sort_by(|(a, n), (b, m)| m.cmp(n).then_with(|| a.cmp(b)));
    let mut output = vec![LOG_HEADER.to_string(), "# event histogram (all records read)".into()];
    output.extend(counts.into_iter().map(|(id, count)| format!("{id} {count}")));
    output.push("# last 300 records".into());
    output.extend(
        records
            .iter()
            .skip(records.len().saturating_sub(300))
            .map(|r| format!("{} {} {} {}", r.timestamp, r.level, r.target, r.event)),
    );
    output.iter().filter_map(|line| scrub_line(line)).map(|line| format!("{line}\n")).collect()
}

fn safe_key(s: &str) -> bool {
    !s.is_empty() && s.len() <= 48 && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

pub fn config_view(value: &Value) -> Value {
    let mut result = Map::new();
    if let Some(obj) = value.as_object() {
        for (key, v) in obj {
            let omitted = json!({"omitted": true});
            let safe = match key.as_str() {
                "worker_mtls"
                | "tls_terminator"
                | "max_events_per_worker"
                | "max_connections"
                | "max_connections_per_ip"
                | "worker_reserve_connections"
                | "request_body_timeout_secs"
                | "http_idle_timeout_secs"
                | "ws_ping_interval_secs"
                | "ws_idle_timeout_secs"
                | "mutation_limit_per_token"
                | "mutation_limit_per_address"
                | "max_token_lifetime_days"
                | "backup_interval_hours"
                | "backup_keep"
                | "backup_interval_seconds"
                | "update_check"
                    if v.is_boolean() || v.is_number() =>
                {
                    v.clone()
                }
                "exposure" if matches!(v.as_str(), Some("loopback" | "lan" | "internet")) => v.clone(),
                "min_tls" if matches!(v.as_str(), Some("1.2" | "1.3")) => v.clone(),
                "bind" => v.as_str().and_then(|s| s.parse::<SocketAddr>().ok()).map_or(omitted.clone(), |addr| {
                    let private = match addr.ip() {
                        IpAddr::V4(ip) => ip.is_private() || ip.is_link_local(),
                        IpAddr::V6(ip) => ip.segments()[0] & 0xfe00 == 0xfc00 || ip.segments()[0] & 0xffc0 == 0xfe80,
                    };
                    json!({"scope": if addr.ip().is_loopback() { "loopback" } else if private { "private" } else { "other" }, "port": addr.port()})
                }),
                "security_webhook_url" | "public_origin" | "web_dir" | "tls_cert_file" | "tls_key_file" | "tls_cert_name" | "backup_dir" => {
                    json!({"set": !v.is_null()})
                }
                "allowed_origins" | "trusted_proxies" => json!({"count": v.as_array().map_or(0, Vec::len)}),
                "listener_groups" => {
                    let mut groups = Map::new();
                    for group in ["console_rest", "worker_channel", "health"] {
                        groups.insert(group.into(), json!({"set": v.get(group).is_some_and(|x| !x.is_null())}));
                    }
                    Value::Object(groups)
                }
                _ => omitted,
            };
            result.insert(key.clone(), safe);
        }
    }
    Value::Object(result)
}

pub fn diagnostics_view(report: &Value) -> Value {
    fn filter(v: &Value, key: &str) -> Value {
        match v {
            Value::Null | Value::Bool(_) | Value::Number(_) => v.clone(),
            Value::String(s) => {
                let allowed = match key {
                    "status" => matches!(s.as_str(), "ok" | "warn" | "fail"),
                    "last_result" => matches!(s.as_str(), "ok" | "skipped" | "failed"),
                    "fix" => true,
                    "coordinator" | "sqlite" => !s.is_empty() && s.len() <= 40 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b".+_-".contains(&b)),
                    _ => false,
                };
                if allowed {
                    v.clone()
                } else {
                    json!("[omitted]")
                }
            }
            Value::Array(a) => Value::Array(a.iter().take(64).map(|v| filter(v, key)).collect()),
            Value::Object(o) => {
                let mut result = Map::new();
                let mut omitted = 0;
                for (k, v) in o {
                    if safe_key(k) {
                        result.insert(k.clone(), filter(v, k));
                    } else {
                        omitted += 1;
                    }
                }
                if omitted > 0 {
                    result.insert("omitted_keys".into(), json!(omitted));
                }
                Value::Object(result)
            }
        }
    }
    filter(report, "")
}

pub fn database_view(db_file: &Path) -> Value {
    fn read(path: &Path) -> Result<Value, Box<dyn std::error::Error>> {
        let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        c.busy_timeout(Duration::from_secs(5))?;
        let mut result = Map::new();
        for name in ["user_version", "page_count", "page_size", "freelist_count"] {
            let n: i64 = c.query_row(&format!("PRAGMA {name}"), [], |r| r.get(0))?;
            result.insert(name.into(), json!(n));
        }
        result.insert("file_bytes".into(), json!(std::fs::metadata(path)?.len()));
        let mut wal = path.as_os_str().to_os_string();
        wal.push("-wal");
        let wal_bytes = match std::fs::metadata(Path::new(&wal)) {
            Ok(m) => m.len(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(e.into()),
        };
        result.insert("wal_bytes".into(), json!(wal_bytes));
        let mut tables = Map::new();
        let mut stmt = c.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")?;
        for name in stmt.query_map([], |r| r.get::<_, String>(0))? {
            let name = name?;
            if safe_key(&name) {
                let count: i64 = c.query_row(&format!("SELECT COUNT(*) FROM \"{name}\""), [], |r| r.get(0))?;
                tables.insert(name, json!(count));
            }
        }
        result.insert("tables".into(), Value::Object(tables));
        Ok(Value::Object(result))
    }
    read(db_file).unwrap_or_else(|_| json!({"error": "database could not be read"}))
}

/// Members of the bundle as (file name without directory, bytes), in archive order.
pub fn members(data_dir: &Path, log_lines: &[String], now: i64) -> Vec<(String, Vec<u8>)> {
    let (cfg, config) = match Config::load(data_dir) {
        Ok(cfg) => {
            let view = config_view(&serde_json::to_value(&cfg).expect("Config serializes"));
            (cfg, view)
        }
        Err(_) => (
            Config::default(),
            json!({"error": "config.toml could not be read or is invalid; run bridge-coordinator check-config"}),
        ),
    };
    let diagnostics = crate::diagnostics::collect_read_only(data_dir, &cfg)
        .map(|mut v| {
            // These producer messages interpolate a directory, worker name or persisted
            // backup error. Copy static advice instead, before trusting `fix` strings.
            for (check, advice) in [
                ("backup_directory", "restrict the backup directory to its owner (chmod 700)"),
                ("event_lag", "check the worker service and its network connection"),
                ("last_backup", "check backup settings, free disk space and recent service logs"),
            ] {
                if let Some(fix) = v.get_mut("checks").and_then(|c| c.get_mut(check)).and_then(|c| c.get_mut("fix")) {
                    if fix.is_string() {
                        *fix = json!(advice);
                    }
                }
            }
            // Worker names are keys of these two maps. Replace them by a number so that a name
            // that happens to look like a plain identifier is not copied either.
            for path in [["command_queue_depth", "by_worker"], ["event_lag", ""]] {
                let slot = v.get_mut("checks").and_then(|c| c.get_mut(path[0])).and_then(|c| c.get_mut("value"));
                let slot = if path[1].is_empty() { slot } else { slot.and_then(|x| x.get_mut(path[1])) };
                if let Some(Value::Object(map)) = slot {
                    let numbered: Map<String, Value> = std::mem::take(map)
                        .into_iter()
                        .enumerate()
                        .map(|(n, (_, value))| (format!("worker_{}", n + 1), value))
                        .collect();
                    *map = numbered;
                }
            }
            diagnostics_view(&v)
        })
        .unwrap_or_else(|_| json!({"error": "diagnostics unavailable"}));
    let names = ["version.json", "diagnostics.json", "config.json", "database.json", "logs.txt"];
    let manifest = json!({"format": 1, "created_at": now, "members": names,
        "note": "Redacted by allow-list. Contains no database rows, credentials, private input, host names or network addresses."});
    let version = json!({"coordinator": env!("CARGO_PKG_VERSION"), "protocol": crate::protocol::PROTOCOL_VERSION,
        "schema_version_expected": crate::db::SCHEMA_VERSION, "sqlite": rusqlite::version(),
        "os": std::env::consts::OS, "arch": std::env::consts::ARCH});
    let text = render_logs(log_lines);
    vec![
        ("manifest.json".into(), manifest),
        ("version.json".into(), version),
        ("diagnostics.json".into(), diagnostics),
        ("config.json".into(), config),
        ("database.json".into(), database_view(&crate::db_path(data_dir))),
    ]
    .into_iter()
    .map(|(name, v)| (name, serde_json::to_vec_pretty(&v).expect("JSON values serialize")))
    .chain(std::iter::once(("logs.txt".into(), text.into_bytes())))
    .collect()
}

pub fn create(data_dir: &Path, out: &Path, log_lines: &[String]) -> Result<Vec<String>, String> {
    let files = members(data_dir, log_lines, crate::now());
    write_tar(out, &files)?;
    Ok(files.iter().map(|(n, _)| format!("{PREFIX}{n}")).collect())
}

fn octal(field: &mut [u8], n: u64) -> Result<(), String> {
    let text = format!("{:0width$o}", n, width = field.len() - 1);
    if text.len() >= field.len() {
        return Err("tar numeric field overflow".into());
    }
    field.fill(0);
    field[..text.len()].copy_from_slice(text.as_bytes());
    Ok(())
}

fn member_name(name: &str) -> Result<String, String> {
    let full = format!("{PREFIX}{name}");
    if name.is_empty() || name == "." || name == ".." || name.contains(['/', '\\', '\0']) || full.len() > 100 {
        return Err("invalid support bundle member name".into());
    }
    Ok(full)
}

pub fn write_tar(out: &Path, members: &[(String, Vec<u8>)]) -> Result<(), String> {
    let mut file = crate::private_options(0o600).write(true).create_new(true).open(out).map_err(|e| {
        if e.kind() == std::io::ErrorKind::AlreadyExists {
            "refusing to overwrite".into()
        } else {
            format!("cannot create support bundle: {e}")
        }
    })?;
    let result = (|| -> Result<(), String> {
        for (name, bytes) in members {
            let name = member_name(name)?;
            let mut h = [0u8; 512];
            h[..name.len()].copy_from_slice(name.as_bytes());
            octal(&mut h[100..108], 0o600)?;
            octal(&mut h[108..116], 0)?;
            octal(&mut h[116..124], 0)?;
            octal(&mut h[124..136], bytes.len() as u64)?;
            octal(&mut h[136..148], crate::now().max(0) as u64)?;
            h[148..156].fill(b' ');
            h[156] = b'0';
            h[257..263].copy_from_slice(b"ustar\0");
            h[263..265].copy_from_slice(b"00");
            h[265..271].copy_from_slice(b"bridge");
            h[297..303].copy_from_slice(b"bridge");
            octal(&mut h[329..337], 0)?;
            octal(&mut h[337..345], 0)?;
            let checksum: u64 = h.iter().map(|&b| b as u64).sum();
            octal(&mut h[148..155], checksum)?;
            h[155] = b' ';
            file.write_all(&h)
                .and_then(|_| file.write_all(bytes))
                .and_then(|_| file.write_all(&[0; 512][..(512 - bytes.len() % 512) % 512]))
                .map_err(|e| e.to_string())?;
        }
        file.write_all(&[0; 1024]).and_then(|_| file.sync_all()).map_err(|e| e.to_string())
    })();
    drop(file);
    if result.is_err() {
        let _ = std::fs::remove_file(out);
    }
    result
}

pub fn read_tar(bytes: &[u8]) -> Result<Vec<(String, Vec<u8>)>, String> {
    fn number(b: &[u8]) -> Result<usize, String> {
        let s = std::str::from_utf8(b).map_err(|_| "invalid tar number")?.trim_matches(['\0', ' ']);
        usize::from_str_radix(s, 8).map_err(|_| "invalid tar number".into())
    }
    let mut offset = 0usize;
    let mut files = Vec::new();
    loop {
        let h = bytes
            .get(offset..offset.checked_add(512).ok_or("tar overflow")?)
            .ok_or("truncated tar header")?;
        if h.iter().all(|&b| b == 0) {
            let end = bytes.get(offset..).ok_or("truncated tar end")?;
            if end.len() < 1024 || end.len() % 512 != 0 || end.iter().any(|&b| b != 0) {
                return Err("truncated or invalid tar end".into());
            }
            return Ok(files);
        }
        if &h[257..263] != b"ustar\0" || &h[263..265] != b"00" || h[156] != b'0' {
            return Err("unsupported tar header".into());
        }
        let sum: usize = h.iter().enumerate().map(|(i, &b)| if (148..156).contains(&i) { 32 } else { b as usize }).sum();
        if number(&h[148..156])? != sum {
            return Err("invalid tar checksum".into());
        }
        let name = std::str::from_utf8(&h[..100]).map_err(|_| "invalid tar name")?.trim_end_matches('\0');
        let short = name.strip_prefix(PREFIX).ok_or("invalid tar member prefix")?;
        member_name(short)?;
        let size = number(&h[124..136])?;
        offset = offset.checked_add(512).ok_or("tar overflow")?;
        let end = offset.checked_add(size).ok_or("tar overflow")?;
        let data = bytes.get(offset..end).ok_or("truncated tar data")?;
        files.push((name.to_string(), data.to_vec()));
        offset = end.checked_add((512 - size % 512) % 512).ok_or("tar overflow")?;
        if offset > bytes.len() {
            return Err("truncated tar padding".into());
        }
    }
}

/// Map runs satisfying `part` without splitting UTF-8, preserving all other characters.
fn runs(text: &str, part: impl Fn(char) -> bool, replace: impl Fn(&str) -> Option<String>) -> String {
    let mut out = String::new();
    let mut start = None;
    for (i, c) in text.char_indices().chain(std::iter::once((text.len(), '\0'))) {
        if i < text.len() && part(c) {
            start.get_or_insert(i);
        } else {
            if let Some(s) = start.take() {
                let word = &text[s..i];
                out.push_str(&replace(word).unwrap_or_else(|| word.into()));
            }
            if i < text.len() {
                out.push(c);
            }
        }
    }
    out
}

pub fn scrub_line(line: &str) -> Option<String> {
    if line.to_ascii_lowercase().contains("prompt") {
        return None;
    }
    let mut text = String::new();
    let mut i = 0;
    while i < line.len() {
        let rest = &line[i..];
        let scheme = rest.bytes().take_while(u8::is_ascii_alphabetic).count();
        let home = rest.starts_with("/home/") || rest.starts_with("/Users/") || rest.starts_with("/root");
        if (scheme > 0 && rest[scheme..].starts_with("://")) || home {
            i += rest.find(char::is_whitespace).unwrap_or(rest.len());
            text.push_str(if home { "[path]" } else { "[url]" });
        } else {
            let c = rest.chars().next()?;
            text.push(c);
            i += c.len_utf8();
        }
    }
    text = runs(
        &text,
        |c| !c.is_whitespace(),
        |w| {
            w.split_once('@').and_then(|(a, b)| {
                (a.chars().any(|c| c.is_ascii_alphabetic()) && b.chars().any(|c| c.is_ascii_alphabetic()) && b.contains('.')).then(|| "[email]".into())
            })
        },
    );
    // Authentication schemes are case insensitive; redact their following word.
    let lower = text.to_ascii_lowercase();
    let mut bearer = String::new();
    i = 0;
    while let Some((pos, length)) = ["bearer ", "basic "]
        .iter()
        .filter_map(|scheme| lower[i..].find(scheme).map(|pos| (pos, scheme.len())))
        .min_by_key(|(pos, _)| *pos)
    {
        let start = i + pos;
        let value = start + length;
        bearer.push_str(&text[i..value]);
        let end = text[value..].find(char::is_whitespace).map_or(text.len(), |n| value + n);
        if end > value {
            bearer.push_str("[redacted]");
        }
        i = end;
    }
    bearer.push_str(&text[i..]);
    text = bearer;
    let lower = text.to_ascii_lowercase();
    let keys = [
        "token",
        "password",
        "passwd",
        "secret",
        "totp",
        "authorization",
        "cookie",
        "key",
        "hash",
        // Login names are sensitive too (the seeded support-bundle fixture includes one).
        "user",
        "session",
        "credential",
        "bearer",
    ];
    let mut scrubbed = String::new();
    i = 0;
    while i < text.len() {
        let mut found = None;
        // Scan the whole key so sensitive substrings also cover api_key and x-auth-token.
        let key_len = text[i..].find(|c: char| c.is_whitespace() || "\"'=:{},".contains(c)).unwrap_or(text.len() - i);
        let key = &lower[i..i + key_len];
        if key_len > 0 && keys.iter().any(|sensitive| key.contains(sensitive)) {
            let mut j = i + key_len;
            if text.as_bytes().get(j) == Some(&b'"') {
                j += 1;
            }
            while text.as_bytes().get(j).is_some_and(u8::is_ascii_whitespace) {
                j += 1;
            }
            if matches!(text.as_bytes().get(j), Some(b'=' | b':')) {
                let authorization = key.contains("authorization") && text.as_bytes()[j] == b':';
                j += 1;
                while text.as_bytes().get(j).is_some_and(u8::is_ascii_whitespace) {
                    j += 1;
                }
                if text.as_bytes().get(j) == Some(&b'"') {
                    j += 1;
                }
                let end = if authorization {
                    text.len()
                } else {
                    text[j..]
                        .find(|c: char| c.is_whitespace() || c == ',' || c == '"')
                        .map_or(text.len(), |n| j + n)
                };
                found = Some((j, end));
            }
        }
        if let Some((j, end)) = found {
            scrubbed.push_str(&text[i..j]);
            scrubbed.push_str("[redacted]");
            i = end;
        } else {
            let c = text[i..].chars().next()?;
            scrubbed.push(c);
            i += c.len_utf8();
        }
    }
    text = runs(
        &scrubbed,
        |c| c.is_ascii_digit() || c == '.',
        |w| {
            let groups: Vec<_> = w.trim_matches('.').split('.').collect();
            (groups.len() == 4 && groups.iter().all(|g| !g.is_empty() && g.len() <= 3 && g.bytes().all(|b| b.is_ascii_digit()))).then(|| "[address]".into())
        },
    );
    text = runs(
        &text,
        |c| c.is_ascii_hexdigit() || c == ':',
        |w| (w.matches(':').count() >= 2 && w.parse::<Ipv6Addr>().is_ok()).then(|| "[address]".into()),
    );
    text = runs(
        &text,
        |c| c.is_ascii_alphanumeric() || "_-.".contains(c),
        |w| {
            let parts: Vec<_> = w.split('.').collect();
            (parts.len() >= 3
                && parts[0].starts_with("eyJ")
                && parts
                    .iter()
                    .all(|p| p.len() >= 8 && p.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))))
            .then(|| "[redacted]".into())
        },
    );
    text = runs(&text, |c| c.is_ascii_hexdigit(), |w| (w.len() >= 32).then(|| "[redacted]".into()));
    text = runs(
        &text,
        |c| c.is_ascii_alphanumeric() || "+/_=-".contains(c),
        |w| (w.len() >= 20 && w.bytes().any(|b| b.is_ascii_digit())).then(|| "[redacted]".into()),
    );
    Some(text.chars().take(400).collect())
}
