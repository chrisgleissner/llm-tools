//! Worker protocol v1 envelope and payload validation, mirroring `llm_tools/bridge_protocol.py`.

use serde_json::{json, Map, Value};

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
pub const MAX_EVENTS_PER_BATCH: usize = 500;
pub const MAX_MISSIONS_PER_HELLO: usize = 5000;
pub const COMMAND_SCHEMA_VERSION: u64 = 1;

const ENVELOPE: [&str; 6] = ["v", "type", "id", "worker_id", "generation", "payload"];

#[derive(Debug, Clone)]
pub struct ProtocolError {
    pub code: &'static str,
    pub message: String,
}

fn perr<T>(code: &'static str, message: impl Into<String>) -> Result<T, ProtocolError> {
    Err(ProtocolError { code, message: message.into() })
}

#[derive(Debug, Clone)]
pub struct Message {
    pub kind: String,
    pub id: String,
    pub worker_id: String,
    pub generation: u64,
    pub payload: Map<String, Value>,
}

/// (required, optional) payload fields per message type.
fn fields(kind: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    Some(match kind {
        "hello" => (
            &[
                "worker_id",
                "name",
                "software_version",
                "protocol_versions",
                "platform",
                "capabilities",
                "missions",
                "event_cursor",
            ],
            &["host_name", "worker_policy"],
        ),
        "welcome" => (&["generation", "protocol_version", "event_cursor"], &[]),
        "command" => (&["schema_version", "operation", "params", "mission_id", "target", "request_id"], &[]),
        "command.ack" => (&["request_id", "status"], &[]),
        "command.result" => (&["schema_version", "request_id", "operation", "outcome", "result", "error"], &[]),
        "events" => (&["after", "through", "events"], &["mission_cursor", "missions"]),
        "events.nack" => (&["expected_after"], &[]),
        "ping" | "pong" => (&[], &[]),
        "terminal.open" => (&["session_id", "mission_id", "mode", "cols", "rows"], &[]),
        "terminal.close" => (&["session_id"], &[]),
        "terminal.status" => (&["session_id", "state", "mode", "reason"], &[]),
        "error" => (&["code", "message"], &["details", "in_reply_to"]),
        _ => return None,
    })
}

/// 1-128 characters of `[A-Za-z0-9_.-]`.
pub fn valid_id(s: &str) -> bool {
    (1..=128).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

/// `[a-z0-9][a-z0-9-]{0,62}`.
pub fn valid_name(s: &str) -> bool {
    let b = s.as_bytes();
    (1..=63).contains(&b.len())
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn uint(v: Option<&Value>, name: &str, minimum: u64) -> Result<u64, ProtocolError> {
    match v.and_then(Value::as_u64) {
        Some(n) if n >= minimum => Ok(n),
        _ => perr("invalid-message", format!("{name} must be an integer >= {minimum}")),
    }
}

/// Shape checks shared by `command` messages and the HTTP `request` object.
pub fn check_command_request(p: &Map<String, Value>) -> Result<(), ProtocolError> {
    if p.get("schema_version").and_then(Value::as_u64) != Some(COMMAND_SCHEMA_VERSION) {
        return perr(
            "unsupported-version",
            format!("unsupported command schema version {:?}", p.get("schema_version")),
        );
    }
    let known = ["schema_version", "operation", "params", "mission_id", "target", "request_id"];
    if let Some(k) = p.keys().find(|k| !known.contains(&k.as_str())) {
        return perr("invalid-request", format!("unknown request field {k}"));
    }
    if !p.get("operation").is_some_and(Value::is_string) || !p.get("params").is_none_or(Value::is_object) {
        return perr("invalid-request", "operation must be a string and params an object");
    }
    if !p.get("mission_id").is_none_or(|m| m.is_null() || m.is_string()) {
        return perr("invalid-request", "mission_id must be a string");
    }
    if !p.get("target").is_none_or(Value::is_string) || !p.get("request_id").is_some_and(Value::is_string) {
        return perr("invalid-request", "target and request_id must be strings");
    }
    Ok(())
}

fn validate_payload(kind: &str, p: &Map<String, Value>) -> Result<(), ProtocolError> {
    let (required, optional) = fields(kind).expect("kind checked");
    let missing: Vec<&&str> = required.iter().filter(|k| !p.contains_key(**k)).collect();
    let unknown: Vec<&String> = p
        .keys()
        .filter(|k| !required.contains(&k.as_str()) && !optional.contains(&k.as_str()))
        .collect();
    if !missing.is_empty() || !unknown.is_empty() {
        return perr("invalid-message", format!("{kind}: missing {missing:?} unknown {unknown:?}"));
    }
    match kind {
        "command" => check_command_request(p)?,
        "command.ack" => {
            if !matches!(p["status"].as_str(), Some("received" | "duplicate")) || !p["request_id"].is_string() {
                return perr("invalid-message", "command.ack: bad status or request_id");
            }
        }
        "command.result" => {
            if p["schema_version"].as_u64() != Some(COMMAND_SCHEMA_VERSION) {
                return perr("unsupported-version", "unsupported command schema version");
            }
            let ok = p["request_id"].as_str().is_some_and(valid_id)
                && p["operation"].is_string()
                && p["outcome"].is_string()
                && (p["result"].is_null() || p["result"].is_object())
                && (p["error"].is_null() || p["error"].is_object());
            if !ok {
                return perr("invalid-message", "command.result: bad request_id, operation, outcome, result or error");
            }
        }
        "events" => {
            let after = uint(p.get("after"), "after", 0)?;
            let through = uint(p.get("through"), "through", 0)?;
            let Some(events) = p["events"].as_array().filter(|e| e.len() <= MAX_EVENTS_PER_BATCH) else {
                return perr("invalid-message", format!("events must be a list of at most {MAX_EVENTS_PER_BATCH}"));
            };
            let mut previous = after;
            for e in events {
                if !e.is_object() {
                    return perr("invalid-message", "each event must be an object");
                }
                let id = uint(e.get("id"), "event id", 1)?;
                if id <= previous || id > through {
                    return perr("invalid-message", "event ids must ascend within (after, through]");
                }
                previous = id;
            }
            if let Some(missions) = p.get("missions") {
                let Some(missions) = missions.as_array().filter(|m| m.len() <= 10_000) else {
                    return perr("invalid-message", "missions must be a list of at most 10000 objects");
                };
                if missions.iter().any(|m| !m.is_object()) {
                    return perr("invalid-message", "each mission snapshot must be an object");
                }
            }
            if let Some(mission_cursor) = p.get("mission_cursor") {
                if uint(Some(mission_cursor), "mission_cursor", 0)? > through {
                    return perr("invalid-message", "mission_cursor must not be greater than through");
                }
            }
            if through < after {
                return perr("invalid-message", "through must not be below after");
            }
        }
        "events.nack" => {
            uint(p.get("expected_after"), "expected_after", 0)?;
        }
        "welcome" => {
            uint(p.get("generation"), "generation", 1)?;
            uint(p.get("event_cursor"), "event_cursor", 0)?;
            if p["protocol_version"].as_u64() != Some(PROTOCOL_VERSION) {
                return perr("unsupported-version", "protocol version is not supported");
            }
        }
        "hello" => {
            uint(p.get("event_cursor"), "event_cursor", 0)?;
            if !p["capabilities"].is_array() || !p["missions"].is_array() {
                return perr("invalid-message", "hello: capabilities and missions must be lists");
            }
        }
        "terminal.open" | "terminal.close" | "terminal.status" => {
            if !p["session_id"].as_str().is_some_and(valid_id) {
                return perr("invalid-message", "session_id must be 1-128 characters of [A-Za-z0-9_.-]");
            }
            match kind {
                "terminal.open" => {
                    if !p["mission_id"].is_string() || !matches!(p["mode"].as_str(), Some("view" | "write")) {
                        return perr("invalid-message", "terminal.open: bad mission_id or mode");
                    }
                    for name in ["cols", "rows"] {
                        if uint(p.get(name), name, 1)? > 1000 {
                            return perr("invalid-message", format!("{name} must be at most 1000"));
                        }
                    }
                }
                "terminal.status"
                    if !matches!(p["state"].as_str(), Some("opened" | "refused" | "closed"))
                        || !matches!(p["mode"].as_str(), Some("view" | "write"))
                        || !p["reason"].is_string() =>
                {
                    return perr("invalid-message", "terminal.status: bad state, mode or reason");
                }
                _ => {}
            }
        }
        "error" if !p["code"].is_string() || !p["message"].is_string() => {
            return perr("invalid-message", "error: code and message must be strings");
        }
        _ => {}
    }
    Ok(())
}

/// Parse and fully validate one message.
pub fn decode(raw: &[u8]) -> Result<Message, ProtocolError> {
    if raw.len() > MAX_MESSAGE_BYTES {
        return perr("message-too-large", format!("message exceeds {MAX_MESSAGE_BYTES} bytes"));
    }
    let Ok(Value::Object(mut v)) = serde_json::from_slice::<Value>(raw) else {
        return perr("invalid-message", "message is not a valid UTF-8 JSON object");
    };
    if v.len() != ENVELOPE.len() || !ENVELOPE.iter().all(|k| v.contains_key(*k)) {
        return perr("invalid-message", format!("envelope must have exactly the fields {ENVELOPE:?}"));
    }
    if v["v"].as_u64() != Some(PROTOCOL_VERSION) {
        return perr("unsupported-version", format!("protocol version {} is not supported", v["v"]));
    }
    let Some(kind) = v["type"].as_str().map(str::to_owned) else {
        return perr("unsupported-message", "unsupported message type");
    };
    if fields(&kind).is_none() {
        return perr("unsupported-message", format!("unsupported message type {kind:?}"));
    }
    let mut ids = [String::new(), String::new()];
    for (slot, name) in ids.iter_mut().zip(["id", "worker_id"]) {
        match v[name].as_str().filter(|s| valid_id(s)) {
            Some(s) => *slot = s.to_owned(),
            None => return perr("invalid-message", format!("{name} must be 1-128 characters of [A-Za-z0-9_.-]")),
        }
    }
    let generation = uint(v.get("generation"), "generation", 0)?;
    let Some(Value::Object(payload)) = v.remove("payload") else {
        return perr("invalid-message", "payload must be an object");
    };
    validate_payload(&kind, &payload)?;
    let [id, worker_id] = ids;
    Ok(Message {
        kind,
        id,
        worker_id,
        generation,
        payload,
    })
}

pub fn new_id() -> String {
    use rand::TryRng;
    let mut b = [0u8; 12];
    rand::rngs::SysRng.try_fill_bytes(&mut b).expect("operating system random source");
    hex::encode(b)
}

/// Serialise an outbound envelope.
pub fn encode(kind: &str, worker_id: &str, generation: u64, payload: Value) -> String {
    encode_with_id(&new_id(), kind, worker_id, generation, payload)
}

/// Like [`encode`] with a caller-chosen message id (to recognise replies to it).
pub fn encode_with_id(id: &str, kind: &str, worker_id: &str, generation: u64, payload: Value) -> String {
    json!({"v": PROTOCOL_VERSION, "type": kind, "id": id, "worker_id": worker_id, "generation": generation, "payload": payload}).to_string()
}

pub fn error_message(worker_id: &str, generation: u64, code: &str, message: &str, in_reply_to: Option<&str>) -> String {
    let mut p = json!({"code": code, "message": message});
    if let Some(r) = in_reply_to {
        p["in_reply_to"] = json!(r);
    }
    encode("error", worker_id, generation, p)
}

/// Extra hello checks the Python validator leaves to the receiver; returns the normalised capability list.
pub fn check_hello(msg: &Message) -> Result<Value, ProtocolError> {
    let p = &msg.payload;
    if p["worker_id"].as_str() != Some(msg.worker_id.as_str()) {
        return perr("invalid-message", "hello: payload worker_id differs from the envelope worker_id");
    }
    if msg.generation != 0 {
        return perr("invalid-message", "hello: generation must be 0");
    }
    for k in ["name", "software_version"] {
        if !p[k].is_string() {
            return perr("invalid-message", format!("hello: {k} must be a string"));
        }
    }
    if let Some(host) = p.get("host_name") {
        if !host
            .as_str()
            .is_some_and(|value| !value.is_empty() && value.len() <= 255 && !value.chars().any(char::is_control))
        {
            return perr("invalid-message", "hello: host_name must be 1-255 bytes without control characters");
        }
    }
    if let Some(policy) = p.get("worker_policy") {
        let Some(policy) = policy.as_object() else {
            return perr("invalid-message", "hello: worker_policy must be an object");
        };
        for key in ["allow_control", "allow_terminal_view", "allow_create", "allow_power", "allow_restore"] {
            if !policy.get(key).is_some_and(Value::is_boolean) {
                return perr("invalid-message", format!("hello: worker_policy.{key} must be boolean"));
            }
        }
        let roots = policy.get("allow_root").and_then(Value::as_array).ok_or_else(|| ProtocolError {
            code: "invalid-message",
            message: "hello: worker_policy.allow_root must be a list".into(),
        })?;
        if roots.len() > 32
            || roots
                .iter()
                .any(|root| !root.as_str().is_some_and(|text| !text.is_empty() && text.len() <= 4096))
        {
            return perr(
                "invalid-message",
                "hello: worker_policy.allow_root must contain at most 32 paths of 1-4096 bytes",
            );
        }
    }
    if !p["platform"].is_object() {
        return perr("invalid-message", "hello: platform must be an object");
    }
    if !p["protocol_versions"]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| v.as_u64() == Some(PROTOCOL_VERSION)))
    {
        return perr("unsupported-version", "hello: protocol version 1 is not offered");
    }
    let caps = p["capabilities"].as_array().expect("validated");
    if caps.iter().any(|c| !c.get("operation").is_some_and(Value::is_string)) {
        return perr("invalid-message", "hello: every capability needs an operation name");
    }
    if p["missions"].as_array().expect("validated").len() > MAX_MISSIONS_PER_HELLO {
        return perr("invalid-message", "hello: too many missions");
    }
    Ok(Value::Array(caps.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hello_payload(host_name: Option<&str>) -> Value {
        let mut payload = json!({
            "worker_id": "worker-1",
            "name": "worker-1",
            "software_version": "1.0",
            "protocol_versions": [PROTOCOL_VERSION],
            "platform": {},
            "capabilities": [],
            "missions": [],
            "event_cursor": 0
        });
        if let Some(host) = host_name {
            payload["host_name"] = json!(host);
        }
        payload
    }

    #[test]
    fn hello_accepts_optional_host_name_and_older_workers() {
        for host_name in [None, Some("build-host")] {
            let raw = encode("hello", "worker-1", 0, hello_payload(host_name));
            let decoded = decode(raw.as_bytes()).expect("compatible hello");
            assert_eq!(decoded.payload.get("host_name").and_then(Value::as_str), host_name);
        }
    }

    #[test]
    fn hello_validates_and_preserves_optional_worker_policy() {
        let mut payload = hello_payload(None);
        let policy = json!({"allow_control":true,"allow_terminal_view":true,"allow_create":false,
            "allow_root":["/srv/work"],"allow_power":false,"allow_restore":false});
        payload["worker_policy"] = policy.clone();
        let decoded = decode(encode("hello", "worker-1", 0, payload).as_bytes()).expect("valid policy");
        assert_eq!(check_hello(&decoded).unwrap(), json!([]));
        assert_eq!(decoded.payload["worker_policy"], policy);

        let mut malformed = hello_payload(None);
        malformed["worker_policy"] = json!({"allow_create":"yes","allow_root":[]});
        let decoded = decode(encode("hello", "worker-1", 0, malformed).as_bytes()).expect("envelope parse");
        let error = check_hello(&decoded).unwrap_err();
        assert_eq!(error.code, "invalid-message");
    }
}
