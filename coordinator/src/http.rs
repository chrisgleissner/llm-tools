//! HTTP API under `/api/v1`, authentication extractor and WebSocket `/stream`.

use crate::auth::{self, RateLimiter};
use crate::config::Config;
use crate::db::{Db, InsertOutcome};
use crate::hub::{Hub, SendResult};
use crate::protocol::{self, valid_id, valid_name};
use axum::body::Bytes;
use axum::extract::ws::{CloseFrame, Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::request::Parts;
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{extract::DefaultBodyLimit, extract::FromRequestParts, Extension, Router};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{broadcast, watch, Semaphore};

pub const MAX_BODY: usize = 1024 * 1024;
pub const COOKIE: &str = "bridge_session";
const MAX_WAIT_MS: u64 = 30_000;

#[derive(Clone, Copy)]
pub struct Peer(pub SocketAddr);

pub struct AppState {
    pub db: Arc<Db>,
    pub hub: Arc<Hub>,
    pub cfg: Config,
    pub data_dir: PathBuf,
    pub limiter: RateLimiter,
    pub started: Instant,
    pub dummy_hash: String,
    pub argon_slots: Semaphore,
    pub shutdown: watch::Receiver<bool>,
    pub web_dir: Option<PathBuf>,
}

pub type St = Arc<AppState>;

// ---------------------------------------------------------------- errors

pub struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
    retry_after: Option<u64>,
}

pub fn err(status: StatusCode, code: &'static str, message: impl Into<String>) -> ApiError {
    ApiError {
        status,
        code,
        message: message.into(),
        retry_after: None,
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(e: rusqlite::Error) -> Self {
        eprintln!("database error: {e}");
        err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "internal error")
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut r = (self.status, axum::Json(json!({"error": {"code": self.code, "message": self.message}}))).into_response();
        if let Some(s) = self.retry_after {
            r.headers_mut().insert(header::RETRY_AFTER, s.to_string().parse().expect("number"));
        }
        r
    }
}

type ApiResult = Result<Response, ApiError>;

fn json_ok(status: StatusCode, v: Value) -> Response {
    (status, axum::Json(v)).into_response()
}

fn unauthorized() -> ApiError {
    err(StatusCode::UNAUTHORIZED, "unauthorized", "authentication required")
}

fn parse_body(bytes: &Bytes) -> Result<Map<String, Value>, ApiError> {
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(m)) => Ok(m),
        _ => Err(err(StatusCode::BAD_REQUEST, "invalid-request", "request body must be a JSON object")),
    }
}

fn str_field<'a>(m: &'a Map<String, Value>, k: &str) -> Result<&'a str, ApiError> {
    m.get(k)
        .and_then(Value::as_str)
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", format!("{k} must be a string")))
}

fn no_extra(m: &Map<String, Value>, allowed: &[&str]) -> Result<(), ApiError> {
    match m.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(err(StatusCode::BAD_REQUEST, "invalid-request", format!("unknown field {k}"))),
        None => Ok(()),
    }
}

// ---------------------------------------------------------------- authentication

pub enum Principal {
    Session { token_hash: String, csrf: String },
    Token { name: String },
}

fn cookie_value(headers: &HeaderMap) -> Option<String> {
    for h in headers.get_all(header::COOKIE) {
        for part in h.to_str().ok()?.split(';') {
            if let Some(v) = part.trim().strip_prefix("bridge_session=") {
                return Some(v.to_string());
            }
        }
    }
    None
}

pub fn authenticate(st: &AppState, headers: &HeaderMap) -> Result<Principal, ApiError> {
    if let Some(h) = headers.get(header::AUTHORIZATION) {
        let token = h.to_str().ok().and_then(|s| s.strip_prefix("Bearer ")).ok_or_else(unauthorized)?;
        return match st.db.token_lookup(&auth::sha256_hex(token))? {
            Some(name) => Ok(Principal::Token { name }),
            None => Err(unauthorized()),
        };
    }
    let cookie = cookie_value(headers).ok_or_else(unauthorized)?;
    let hash = auth::sha256_hex(&cookie);
    match st.db.session_get(&hash)? {
        Some(s) => Ok(Principal::Session {
            token_hash: hash,
            csrf: s.csrf,
        }),
        None => Err(unauthorized()),
    }
}

/// `Origin` must equal `https://<Host>` or an entry of `allowed_origins` (exact match).
pub fn origin_ok(headers: &HeaderMap, cfg: &Config) -> bool {
    let Some(origin) = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    if cfg.allowed_origins.iter().any(|o| o == origin) {
        return true;
    }
    headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|h| origin.eq_ignore_ascii_case(&format!("https://{h}")))
}

pub struct Authed(pub Principal, pub String);

impl Principal {
    pub fn actor(&self) -> String {
        match self {
            Principal::Session { .. } => "admin".into(),
            Principal::Token { name } => format!("token:{name}"),
        }
    }
}

impl FromRequestParts<St> for Authed {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &St) -> Result<Self, ApiError> {
        let principal = authenticate(st, &parts.headers)?;
        if let Principal::Session { csrf, .. } = &principal {
            if !matches!(parts.method, Method::GET | Method::HEAD | Method::OPTIONS) {
                if !origin_ok(&parts.headers, &st.cfg) {
                    return Err(err(
                        StatusCode::FORBIDDEN,
                        "bad-origin",
                        "Origin header missing or not the coordinator's origin",
                    ));
                }
                let sent = parts.headers.get("x-csrf-token").and_then(|v| v.to_str().ok()).unwrap_or("");
                if !auth::ct_eq(sent, csrf) {
                    return Err(err(StatusCode::FORBIDDEN, "bad-csrf", "missing or invalid X-CSRF-Token"));
                }
            }
        }
        let remote = parts.extensions.get::<Peer>().map(|p| p.0.ip().to_string()).unwrap_or_default();
        Ok(Authed(principal, remote))
    }
}

pub struct Remote(pub String);
impl<S: Send + Sync> FromRequestParts<S> for Remote {
    type Rejection = std::convert::Infallible;
    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        Ok(Remote(parts.extensions.get::<Peer>().map(|p| p.0.ip().to_string()).unwrap_or_default()))
    }
}

async fn hash_blocking(st: &AppState, password: String) -> String {
    let _slot = st.argon_slots.acquire().await.expect("semaphore open");
    tokio::task::spawn_blocking(move || auth::hash_password(&password)).await.expect("hash task")
}

async fn verify_blocking(st: &AppState, password: String, phc: String) -> bool {
    let _slot = st.argon_slots.acquire().await.expect("semaphore open");
    tokio::task::spawn_blocking(move || auth::verify_password(&password, &phc))
        .await
        .expect("verify task")
}

fn session_cookie(value: &str, max_age: u64) -> String {
    format!("{COOKIE}={value}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={max_age}")
}

fn limited(retry: u64) -> ApiError {
    let mut e = err(StatusCode::TOO_MANY_REQUESTS, "rate-limited", "too many failed attempts; try again later");
    e.retry_after = Some(retry);
    e
}

fn check_public_origin(st: &AppState, headers: &HeaderMap) -> Result<(), ApiError> {
    if headers.contains_key(header::ORIGIN) && !origin_ok(headers, &st.cfg) {
        return Err(err(StatusCode::FORBIDDEN, "bad-origin", "Origin is not the coordinator's origin"));
    }
    Ok(())
}

fn clean(s: &str) -> String {
    s.chars().filter(|c| !c.is_control()).take(64).collect()
}

// ---------------------------------------------------------------- handlers: auth

async fn healthz() -> Response {
    json_ok(StatusCode::OK, json!({"status": "ok"}))
}

async fn auth_state(State(st): State<St>, headers: HeaderMap) -> ApiResult {
    let setup_required = st.db.admin_get()?.is_none();
    let mut body = json!({"setup_required": setup_required, "authenticated": false});
    if let Ok(Principal::Session { csrf, .. }) = authenticate(&st, &headers) {
        body["authenticated"] = json!(true);
        body["csrf_token"] = json!(csrf);
    } else if matches!(authenticate(&st, &headers), Ok(Principal::Token { .. })) {
        body["authenticated"] = json!(true);
    }
    Ok(json_ok(StatusCode::OK, body))
}

async fn setup(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, body: Bytes) -> ApiResult {
    check_public_origin(&st, &headers)?;
    let key = format!("setup:{ip}");
    if let Some(r) = st.limiter.blocked(&key) {
        return Err(limited(r));
    }
    let m = parse_body(&body)?;
    no_extra(&m, &["setup_token", "username", "password"])?;
    let (token, username, password) = (str_field(&m, "setup_token")?, str_field(&m, "username")?, str_field(&m, "password")?);
    if st.db.admin_get()?.is_some() {
        return Err(err(StatusCode::CONFLICT, "setup-complete", "an administrator already exists"));
    }
    let path = st.data_dir.join(crate::SETUP_TOKEN_FILE);
    let expected = std::fs::read_to_string(&path).map(|s| s.trim().to_string()).unwrap_or_default();
    // Compare even when the file is missing so timing does not reveal its presence.
    if !auth::ct_eq(token, &expected) || expected.is_empty() {
        st.limiter.fail(&key);
        st.db.audit("anonymous", "setup.failure", "invalid setup token", &ip);
        return Err(err(StatusCode::FORBIDDEN, "invalid-setup-token", "invalid setup token"));
    }
    if !auth::valid_username(username) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-username",
            "username must be 1-64 characters of letters, digits and ._-@",
        ));
    }
    auth::valid_password(password).map_err(|e| err(StatusCode::BAD_REQUEST, "invalid-password", e))?;
    let hash = hash_blocking(&st, password.to_string()).await;
    if !st.db.admin_create(username, &hash)? {
        return Err(err(StatusCode::CONFLICT, "setup-complete", "an administrator already exists"));
    }
    let _ = std::fs::remove_file(&path);
    st.db.audit(username, "setup", "administrator created, setup token consumed", &ip);
    Ok(json_ok(StatusCode::CREATED, json!({"username": username})))
}

async fn login(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, body: Bytes) -> ApiResult {
    check_public_origin(&st, &headers)?;
    let m = parse_body(&body)?;
    no_extra(&m, &["username", "password"])?;
    let (username, password) = (str_field(&m, "username")?, str_field(&m, "password")?);
    let ip_key = format!("ip:{ip}");
    let user_key = format!("user:{}", clean(&username.to_lowercase()));
    for k in [&ip_key, &user_key] {
        if let Some(r) = st.limiter.blocked(k) {
            return Err(limited(r));
        }
    }
    if password.len() > auth::MAX_PASSWORD_LEN {
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
    }
    let admin = st.db.admin_get()?;
    // Always run one Argon2 verification so unknown users cost the same as wrong passwords.
    let (known, phc) = match &admin {
        Some((u, h)) if auth::ct_eq(u, username) => (true, h.clone()),
        _ => (false, st.dummy_hash.clone()),
    };
    let verified = verify_blocking(&st, password.to_string(), phc).await;
    if !(known && verified) {
        st.limiter.fail(&ip_key);
        st.limiter.fail(&user_key);
        st.db.audit(&clean(username), "login.failure", "invalid credentials", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
    }
    let (token, csrf) = (auth::random_token(), auth::random_token());
    st.db.session_create(&auth::sha256_hex(&token), &csrf)?;
    st.db.audit(username, "login.success", "", &ip);
    let mut r = json_ok(StatusCode::OK, json!({"csrf_token": csrf}));
    r.headers_mut().insert(
        header::SET_COOKIE,
        session_cookie(&token, crate::db::SESSION_ABSOLUTE_SECS as u64).parse().expect("cookie"),
    );
    Ok(r)
}

async fn logout(State(st): State<St>, Authed(p, ip): Authed) -> ApiResult {
    if let Principal::Session { token_hash, .. } = &p {
        st.db.session_delete(token_hash)?;
    }
    st.db.audit(&p.actor(), "logout", "", &ip);
    let mut r = json_ok(StatusCode::OK, json!({"ok": true}));
    r.headers_mut().insert(header::SET_COOKIE, session_cookie("", 0).parse().expect("cookie"));
    Ok(r)
}

async fn change_password(State(st): State<St>, Authed(p, ip): Authed, body: Bytes) -> ApiResult {
    let m = parse_body(&body)?;
    no_extra(&m, &["current", "new"])?;
    let (current, new) = (str_field(&m, "current")?, str_field(&m, "new")?);
    let key = "password-change".to_string();
    if let Some(r) = st.limiter.blocked(&key) {
        return Err(limited(r));
    }
    let (_, phc) = st.db.admin_get()?.ok_or_else(unauthorized)?;
    if current.len() > auth::MAX_PASSWORD_LEN || !verify_blocking(&st, current.to_string(), phc).await {
        st.limiter.fail(&key);
        st.db.audit(&p.actor(), "password.failure", "wrong current password", &ip);
        return Err(err(StatusCode::FORBIDDEN, "invalid-credentials", "current password is wrong"));
    }
    auth::valid_password(new).map_err(|e| err(StatusCode::BAD_REQUEST, "invalid-password", e))?;
    let hash = hash_blocking(&st, new.to_string()).await;
    st.db.admin_set_password(&hash)?;
    st.db.audit(&p.actor(), "password.change", "all sessions deleted", &ip);
    let mut r = json_ok(StatusCode::OK, json!({"ok": true}));
    r.headers_mut().insert(header::SET_COOKIE, session_cookie("", 0).parse().expect("cookie"));
    Ok(r)
}

// ---------------------------------------------------------------- handlers: workers, missions, events

fn worker_json(st: &AppState, w: &crate::db::WorkerRow) -> Value {
    json!({"name": w.name, "id": w.worker_id, "state": if st.hub.online(&w.name) { "online" } else { "offline" },
           "last_seen": w.last_seen, "generation": w.generation, "event_cursor": w.event_cursor,
           "versions": {"software": w.software_version, "protocol": w.protocol_versions},
           "platform": w.platform, "capabilities": w.capabilities})
}

async fn list_workers(State(st): State<St>, _a: Authed) -> ApiResult {
    let workers: Vec<Value> = st.db.workers_list()?.iter().map(|w| worker_json(&st, w)).collect();
    Ok(json_ok(StatusCode::OK, json!({"workers": workers})))
}

async fn add_worker(State(st): State<St>, Authed(p, ip): Authed, body: Bytes) -> ApiResult {
    let m = parse_body(&body)?;
    no_extra(&m, &["name"])?;
    let name = str_field(&m, "name")?;
    if !valid_name(name) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-name",
            "worker name must be lowercase letters, digits and '-' (at most 63)",
        ));
    }
    let token = auth::random_token();
    if !st.db.worker_add(name, &auth::sha256_hex(&token))? {
        return Err(err(StatusCode::CONFLICT, "worker-exists", "a worker with this name exists"));
    }
    st.db.audit(&p.actor(), "worker.add", name, &ip);
    Ok(json_ok(StatusCode::CREATED, json!({"name": name, "token": token})))
}

async fn revoke_worker(State(st): State<St>, Authed(p, ip): Authed, Path(name): Path<String>) -> ApiResult {
    if !st.db.worker_revoke(&name)? {
        return Err(err(StatusCode::NOT_FOUND, "unknown-target", "no such worker"));
    }
    st.hub.close(&name, 1008, "worker revoked");
    st.db.audit(&p.actor(), "worker.revoke", &clean(&name), &ip);
    st.hub.publish(json!({"type": "worker", "worker": name, "state": "revoked"}));
    Ok(json_ok(StatusCode::OK, json!({"ok": true})))
}

async fn list_missions(State(st): State<St>, _a: Authed) -> ApiResult {
    let workers: HashMap<String, crate::db::WorkerRow> = st.db.workers_list()?.into_iter().map(|w| (w.name.clone(), w)).collect();
    let missions: Vec<Value> = st
        .db
        .missions_list()?
        .into_iter()
        .filter_map(|(worker, info)| {
            let w = workers.get(&worker)?;
            let newer_event = matches!((w.hello_at, w.last_event_at), (Some(h), Some(e)) if h < e);
            let stale = !st.hub.online(&worker) || newer_event;
            Some(json!({"worker": worker, "stale": stale, "mission": info}))
        })
        .collect();
    Ok(json_ok(StatusCode::OK, json!({"missions": missions})))
}

async fn list_events(State(st): State<St>, _a: Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    let num = |k: &str, default: u64| -> Result<u64, ApiError> {
        q.get(k).map_or(Ok(default), |v| {
            v.parse()
                .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", format!("{k} must be a non-negative integer")))
        })
    };
    let (after, limit) = (num("after", 0)?, num("limit", 100)?.clamp(1, 1000));
    let events = st.db.events_list(q.get("worker").map(String::as_str), after, limit)?;
    Ok(json_ok(StatusCode::OK, json!({"events": events})))
}

// ---------------------------------------------------------------- handlers: commands

async fn post_command(State(st): State<St>, Authed(p, ip): Authed, body: Bytes) -> ApiResult {
    let m = parse_body(&body)?;
    no_extra(&m, &["target", "request", "wait_ms"])?;
    let target = str_field(&m, "target")?;
    let Some(Value::Object(request)) = m.get("request") else {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "request must be a JSON object"));
    };
    let wait_ms = match m.get("wait_ms") {
        None | Some(Value::Null) => 0,
        Some(v) => v
            .as_u64()
            .filter(|w| *w <= MAX_WAIT_MS)
            .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer of at most 30000"))?,
    };
    protocol::check_command_request(request).map_err(|e| err(StatusCode::BAD_REQUEST, e.code, e.message))?;
    let request_id = request["request_id"].as_str().expect("checked");
    if !valid_id(request_id) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "request_id must be 1-128 characters of letters, digits, '.', '_' or '-'",
        ));
    }
    if !valid_name(target) || matches!(target, "all" | "local") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-target", "target must be a worker name"));
    }
    let worker = st
        .db
        .worker_get(target)?
        .ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown-target", format!("no worker named {target:?}")))?;
    let operation = request["operation"].as_str().expect("checked");
    let mutating = worker.capability(operation).ok_or_else(|| {
        err(
            StatusCode::UNPROCESSABLE_ENTITY,
            "unsupported-operation",
            format!("worker {target:?} did not advertise operation {operation:?}"),
        )
    })?;
    let request_value = Value::Object(request.clone());
    let row = match st.db.command_insert(target, operation, mutating, &request_value)? {
        InsertOutcome::Conflict => {
            return Err(err(
                StatusCode::CONFLICT,
                "request-id-conflict",
                "request_id exists with a different request or target",
            ))
        }
        InsertOutcome::QueueFull => return Err(err(StatusCode::TOO_MANY_REQUESTS, "queue-full", "too many unfinished commands for this worker")),
        InsertOutcome::Existing(row) => row,
        InsertOutcome::New(row) => {
            if mutating {
                let detail = format!(
                    "worker={target} operation={operation} request_id={request_id} mission={}",
                    clean(request.get("mission_id").and_then(Value::as_str).unwrap_or(""))
                );
                st.db.audit(&p.actor(), "command.submit", &detail, &ip);
            }
            st.hub.publish(json!({"type": "command", "command": row.to_json()}));
            let mut payload = request.clone();
            payload.insert("target".into(), json!("local"));
            payload.entry("params").or_insert_with(|| json!({}));
            payload.entry("mission_id").or_insert(Value::Null);
            // Offline or a full queue is fine: the journal entry is redelivered after the next welcome.
            let _ = matches!(st.hub.send_command(target, Value::Object(payload), request_id), SendResult::Queued);
            row
        }
    };
    let mut row = row;
    if wait_ms > 0 && row.state != "done" {
        let deadline = Instant::now() + Duration::from_millis(wait_ms);
        while Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(25)).await;
            if let Some(r) = st.db.command_get(request_id)? {
                row = r;
                if row.state == "done" {
                    break;
                }
            }
        }
    }
    let status = if row.state == "done" { StatusCode::OK } else { StatusCode::ACCEPTED };
    Ok(json_ok(status, row.to_json()))
}

async fn get_command(State(st): State<St>, _a: Authed, Path(id): Path<String>) -> ApiResult {
    match st.db.command_get(&id)? {
        Some(r) => Ok(json_ok(StatusCode::OK, r.to_json())),
        None => Err(err(StatusCode::NOT_FOUND, "unknown-command", "no such request_id")),
    }
}

// ---------------------------------------------------------------- diagnostics

fn rss_kib() -> Option<u64> {
    std::fs::read_to_string("/proc/self/status")
        .ok()?
        .lines()
        .find_map(|l| l.strip_prefix("VmRSS:")?.trim().trim_end_matches("kB").trim().parse().ok())
}

async fn diagnostics(State(st): State<St>, _a: Authed) -> ApiResult {
    let mut d = st.db.diagnostics()?;
    d["version"] = json!(env!("CARGO_PKG_VERSION"));
    d["protocol_version"] = json!(protocol::PROTOCOL_VERSION);
    d["uptime_seconds"] = json!(st.started.elapsed().as_secs());
    d["rss_kib"] = json!(rss_kib());
    d["connected_workers"] = json!(st.hub.connected());
    d["sqlite_version"] = json!(rusqlite::version());
    Ok(json_ok(StatusCode::OK, d))
}

// ---------------------------------------------------------------- /stream

async fn stream(State(st): State<St>, Authed(p, _): Authed, headers: HeaderMap, ws: WebSocketUpgrade) -> ApiResult {
    if matches!(p, Principal::Session { .. }) && !origin_ok(&headers, &st.cfg) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "bad-origin",
            "Origin header missing or not the coordinator's origin",
        ));
    }
    let rx = st.hub.stream.subscribe();
    let shutdown = st.shutdown.clone();
    Ok(ws
        .max_message_size(64 * 1024)
        .max_frame_size(64 * 1024)
        .on_upgrade(move |s| stream_session(s, rx, shutdown))
        .into_response())
}

async fn stream_session(mut socket: WebSocket, mut rx: broadcast::Receiver<Arc<str>>, mut shutdown: watch::Receiver<bool>) {
    let close = |code: u16, reason: &'static str| WsMessage::Close(Some(CloseFrame { code, reason: reason.into() }));
    loop {
        tokio::select! {
            m = rx.recv() => match m {
                Ok(text) => if socket.send(WsMessage::Text(text.to_string().into())).await.is_err() { return },
                Err(broadcast::error::RecvError::Lagged(_)) => { let _ = socket.send(close(1013, "client too slow")).await; return }
                Err(_) => return,
            },
            incoming = socket.recv() => match incoming {
                Some(Ok(WsMessage::Close(_))) | None | Some(Err(_)) => return,
                Some(Ok(_)) => {}
            },
            _ = shutdown.changed() => { let _ = socket.send(close(1001, "shutting down")).await; return }
        }
    }
}

// ---------------------------------------------------------------- router

async fn static_files(State(st): State<St>, method: Method, uri: axum::http::Uri, headers: HeaderMap) -> Response {
    crate::web::serve(st.web_dir.clone(), method, uri, headers).await
}

async fn security_headers(mut r: Response) -> Response {
    let h = r.headers_mut();
    // The static handler sets its own Content-Security-Policy and cache policy.
    for (k, v) in [
        ("cache-control", "no-store"),
        ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
    ] {
        if !h.contains_key(k) {
            h.insert(k, v.parse().expect("static header"));
        }
    }
    for (k, v) in [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("strict-transport-security", "max-age=31536000"),
    ] {
        h.insert(k, v.parse().expect("static header"));
    }
    r
}

pub fn router(st: St) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/v1/auth/state", get(auth_state))
        .route("/api/v1/auth/setup", post(setup))
        .route("/api/v1/auth/login", post(login))
        .route("/api/v1/auth/logout", post(logout))
        .route("/api/v1/auth/password", post(change_password))
        .route("/api/v1/workers", get(list_workers).post(add_worker))
        .route("/api/v1/workers/{name}", delete(revoke_worker))
        .route("/api/v1/missions", get(list_missions))
        .route("/api/v1/commands", post(post_command))
        .route("/api/v1/commands/{id}", get(get_command))
        .route("/api/v1/events", get(list_events))
        .route("/api/v1/stream", get(stream))
        .route("/api/v1/diagnostics", get(diagnostics))
        .route("/api/v1/worker/connect", get(crate::ws_worker::connect))
        .fallback(static_files)
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(axum::middleware::map_response(security_headers))
        .with_state(st)
}

pub fn with_peer(app: Router, peer: SocketAddr) -> Router {
    app.layer(Extension(Peer(peer)))
}
