//! HTTP API under `/api/v1`, authentication extractor and WebSocket `/stream`.

use crate::auth::{self, RateLimiter};
use crate::config::Config;
use crate::db::{Db, InsertOutcome};
use crate::hub::{Hub, SendResult};
use crate::protocol::{self, valid_id, valid_name};
use axum::body::Bytes;
use axum::extract::ws::{CloseFrame, Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{FromRequest, Path, Query, Request, State};
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
use tokio::sync::{broadcast, watch, OwnedSemaphorePermit, Semaphore};

pub const MAX_BODY: usize = 1024 * 1024;
/// Request bodies of the unauthenticated `/api/v1/auth/*` routes (login, setup) are tiny.
pub const AUTH_BODY_MAX: usize = 4096;
/// Bodies declared (or streamed with unknown length) above this size need one of
/// [`BIG_BODY_SLOTS`] while they are buffered.
pub const BIG_BODY_BYTES: u64 = 64 * 1024;
/// At most this many bodies above [`BIG_BODY_BYTES`] are buffered at once: 8 x 1 MiB.
pub const BIG_BODY_SLOTS: usize = 8;
/// `__Host-` makes the browser accept the cookie only with `Secure`, `Path=/` and no `Domain`,
/// so a cookie set from a sibling host name cannot replace it.
pub const COOKIE: &str = "__Host-bridge_session";
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
    /// Permits for request bodies larger than [`BIG_BODY_BYTES`] (see [`read_body`]).
    pub big_bodies: Arc<Semaphore>,
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
        if matches!(self.status, StatusCode::REQUEST_TIMEOUT | StatusCode::SERVICE_UNAVAILABLE) {
            // The request body may be only partly read: do not reuse the connection.
            r.headers_mut().insert(header::CONNECTION, "close".parse().expect("static header"));
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

/// A buffered request body. Holds its [`BIG_BODY_SLOTS`] permit, if it needed one, until dropped.
pub struct Buffered {
    pub bytes: Bytes,
    _permit: Option<OwnedSemaphorePermit>,
}

/// Buffer the request body within the route's size limit and `request_body_timeout_secs`.
/// A body above [`BIG_BODY_BYTES`] (declared, or chunked with unknown length) first takes one of
/// the [`BIG_BODY_SLOTS`] permits, so total buffered body memory stays bounded.
pub async fn read_body(st: &St, req: Request) -> Result<Buffered, ApiError> {
    let declared = req
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    let big = declared.map_or(req.headers().contains_key(header::TRANSFER_ENCODING), |n| n > BIG_BODY_BYTES);
    let permit = if big {
        let p = st
            .big_bodies
            .clone()
            .try_acquire_owned()
            .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "busy", "too many large requests in progress; retry shortly"))?;
        Some(p)
    } else {
        None
    };
    let read = Bytes::from_request(req, st);
    match tokio::time::timeout(Duration::from_secs(st.cfg.request_body_timeout_secs), read).await {
        Err(_) => Err(err(StatusCode::REQUEST_TIMEOUT, "request-timeout", "request body not received in time")),
        Ok(Err(rej)) => Err(err(
            rej.status(),
            if rej.status() == StatusCode::PAYLOAD_TOO_LARGE {
                "payload-too-large"
            } else {
                "invalid-request"
            },
            "request body too large or unreadable",
        )),
        Ok(Ok(bytes)) => Ok(Buffered { bytes, _permit: permit }),
    }
}

impl FromRequest<St> for Buffered {
    type Rejection = ApiError;
    async fn from_request(req: Request, st: &St) -> Result<Self, ApiError> {
        read_body(st, req).await
    }
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
    Session {
        token_hash: String,
        csrf: String,
        remember: bool,
        expires_at: i64,
        idle_secs: i64,
    },
    Token {
        name: String,
    },
}

fn cookie_value(headers: &HeaderMap) -> Option<String> {
    let prefix = format!("{COOKIE}=");
    for h in headers.get_all(header::COOKIE) {
        for part in h.to_str().ok()?.split(';') {
            if let Some(v) = part.trim().strip_prefix(prefix.as_str()) {
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
            remember: s.remember,
            expires_at: s.expires_at,
            idle_secs: s.idle_secs,
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

/// `max_age: None` makes a browser-session cookie (no `Max-Age`): the browser drops it on exit.
fn session_cookie(value: &str, max_age: Option<u64>) -> String {
    let age = max_age.map(|a| format!("; Max-Age={a}")).unwrap_or_default();
    format!("{COOKIE}={value}; HttpOnly; Secure; SameSite=Strict; Path={}{age}", "/")
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
    let mut body = json!({
        "setup_required": setup_required,
        "authenticated": false,
        "password_policy": {"min_length": auth::MIN_PASSWORD_LEN, "max_length": auth::MAX_PASSWORD_LEN},
        "session_policy": {
            "remembered": {"idle_secs": crate::db::session_policy(true).0, "absolute_secs": crate::db::session_policy(true).1},
            "default": {"idle_secs": crate::db::session_policy(false).0, "absolute_secs": crate::db::session_policy(false).1},
        },
    });
    if let Ok(Principal::Session {
        csrf,
        remember,
        expires_at,
        idle_secs,
        ..
    }) = authenticate(&st, &headers)
    {
        body["authenticated"] = json!(true);
        body["csrf_token"] = json!(csrf);
        body["username"] = json!(st.db.admin_get()?.map(|(u, _)| u));
        body["session"] = json!({"remember": remember, "expires_at": expires_at, "idle_timeout_secs": idle_secs});
    } else if matches!(authenticate(&st, &headers), Ok(Principal::Token { .. })) {
        body["authenticated"] = json!(true);
    }
    Ok(json_ok(StatusCode::OK, body))
}

/// Rate-limit key part for the client address (IPv6 by /64).
fn client_key(ip: &str) -> String {
    ip.parse::<std::net::IpAddr>().map_or_else(|_| ip.to_string(), |a| auth::addr_key(&a))
}

async fn setup(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, req: Request) -> ApiResult {
    check_public_origin(&st, &headers)?;
    let addr = client_key(&ip);
    // The attempt is reserved before the body is read; only a wrong setup token counts as a failure.
    let attempt = st.limiter.acquire(&[format!("setup:{addr}")], false).map_err(limited)?;
    let body = match read_body(&st, req).await {
        Ok(b) => b,
        Err(e) => {
            attempt.release();
            return Err(e);
        }
    };
    let parsed = parse_body(&body.bytes).and_then(|m| {
        no_extra(&m, &["setup_token", "username", "password"])?;
        Ok((
            str_field(&m, "setup_token")?.to_string(),
            str_field(&m, "username")?.to_string(),
            str_field(&m, "password")?.to_string(),
        ))
    });
    let (token, username, password) = match parsed {
        Ok(t) => t,
        Err(e) => {
            attempt.release();
            return Err(e);
        }
    };
    let (token, username, password) = (token.as_str(), username.as_str(), password.as_str());
    if st.db.admin_get()?.is_some() {
        attempt.release();
        return Err(err(StatusCode::CONFLICT, "setup-complete", "an administrator already exists"));
    }
    let path = st.data_dir.join(crate::SETUP_TOKEN_FILE);
    let expected = std::fs::read_to_string(&path).map(|s| s.trim().to_string()).unwrap_or_default();
    // Compare even when the file is missing so timing does not reveal its presence.
    if !auth::ct_eq(token, &expected) || expected.is_empty() {
        attempt.fail();
        st.db.audit("anonymous", "setup.failure", "invalid setup token", &ip);
        return Err(err(StatusCode::FORBIDDEN, "invalid-setup-token", "invalid setup token"));
    }
    attempt.release();
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

/// Login rate limiting (see docs/distributed/threat-model.md):
/// * one attempt is reserved under the limiter lock for the client address (IPv6 by /64) before
///   the body is read or Argon2 starts, and attempts still in progress count against the limit, so
///   concurrent requests cannot all pass the check;
/// * a second key per user name stops guessing from many addresses, but is not applied to an
///   address with a successful login in the last 7 days, so anyone sending bad passwords for
///   `admin` cannot lock the administrator out of the addresses they normally use.
async fn login(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, req: Request) -> ApiResult {
    check_public_origin(&st, &headers)?;
    let addr = client_key(&ip);
    let known_addr = st.db.known_addr(&addr)?;
    let mut attempt = st.limiter.acquire(&[format!("ip:{addr}")], known_addr).map_err(limited)?;
    let body = match read_body(&st, req).await {
        Ok(b) => b,
        Err(e) => {
            attempt.release();
            return Err(e);
        }
    };
    let parsed = parse_body(&body.bytes).and_then(|m| {
        no_extra(&m, &["username", "password", "remember"])?;
        let remember = match m.get("remember") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(b)) => *b,
            _ => return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "remember must be true or false")),
        };
        Ok((str_field(&m, "username")?.to_string(), str_field(&m, "password")?.to_string(), remember))
    });
    drop(body);
    let (username, password, remember) = match parsed {
        Ok(c) => c,
        Err(e) => {
            attempt.release();
            return Err(e);
        }
    };
    if !known_addr {
        if let Err(r) = attempt.add_keys(&[format!("user:{}", clean(&username.to_lowercase()))], false) {
            attempt.release();
            return Err(limited(r));
        }
    }
    if password.len() > auth::MAX_PASSWORD_LEN {
        attempt.fail();
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
    }
    let admin = st.db.admin_get()?;
    // Always run one Argon2 verification so unknown users cost the same as wrong passwords.
    let (known, phc) = match &admin {
        Some((u, h)) if auth::ct_eq(u, &username) => (true, h.clone()),
        _ => (false, st.dummy_hash.clone()),
    };
    let verified = verify_blocking(&st, password, phc).await;
    if !(known && verified) {
        attempt.fail();
        st.db.audit(&clean(&username), "login.failure", "invalid credentials", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
    }
    attempt.succeed();
    st.db.known_addr_remember(&addr)?;
    let (token, csrf) = (auth::random_token(), auth::random_token());
    st.db.session_create_with(&auth::sha256_hex(&token), &csrf, remember)?;
    st.db.audit(&username, "login.success", "", &ip);
    let mut r = json_ok(StatusCode::OK, json!({"csrf_token": csrf}));
    r.headers_mut().insert(
        header::SET_COOKIE,
        session_cookie(&token, remember.then_some(crate::db::SESSION_ABSOLUTE_SECS as u64))
            .parse()
            .expect("cookie"),
    );
    Ok(r)
}

async fn logout(State(st): State<St>, Authed(p, ip): Authed) -> ApiResult {
    if let Principal::Session { token_hash, .. } = &p {
        st.db.session_delete(token_hash)?;
    }
    st.db.audit(&p.actor(), "logout", "", &ip);
    let mut r = json_ok(StatusCode::OK, json!({"ok": true}));
    r.headers_mut().insert(header::SET_COOKIE, session_cookie("", Some(0)).parse().expect("cookie"));
    Ok(r)
}

async fn change_password(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["current", "new", "keep_api_tokens"])?;
    let (current, new) = (str_field(&m, "current")?, str_field(&m, "new")?);
    let keep_tokens = match m.get("keep_api_tokens") {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        _ => return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "keep_api_tokens must be true or false")),
    };
    let attempt = st.limiter.acquire(&["password-change".to_string()], true).map_err(limited)?;
    let Some((_, phc)) = st.db.admin_get()? else {
        attempt.release();
        return Err(unauthorized());
    };
    if current.len() > auth::MAX_PASSWORD_LEN || !verify_blocking(&st, current.to_string(), phc).await {
        attempt.fail();
        st.db.audit(&p.actor(), "password.failure", "wrong current password", &ip);
        return Err(err(StatusCode::FORBIDDEN, "invalid-credentials", "current password is wrong"));
    }
    attempt.succeed();
    auth::valid_password(new).map_err(|e| err(StatusCode::BAD_REQUEST, "invalid-password", e))?;
    let hash = hash_blocking(&st, new.to_string()).await;
    let revoked = st.db.admin_set_password(&hash, keep_tokens)?;
    st.db.audit(
        &p.actor(),
        "password.change",
        &format!("all sessions deleted; {revoked} API token(s) revoked"),
        &ip,
    );
    let mut r = json_ok(StatusCode::OK, json!({"ok": true, "revoked_api_tokens": revoked}));
    r.headers_mut().insert(header::SET_COOKIE, session_cookie("", Some(0)).parse().expect("cookie"));
    Ok(r)
}

// ---------------------------------------------------------------- handlers: workers, missions, events

fn worker_json(st: &AppState, w: &crate::db::WorkerRow) -> Value {
    // A connected worker has been heard from within the last ping interval; the stored value is
    // persisted only every few minutes (ws_worker.rs LAST_SEEN_EVERY_SECS) to spare storage.
    let online = st.hub.online(&w.name);
    let last_seen = if online { Some(crate::now()) } else { w.last_seen };
    json!({"name": w.name, "id": w.worker_id, "state": if online { "online" } else { "offline" },
           "last_seen": last_seen, "generation": w.generation, "event_cursor": w.event_cursor,
           "versions": {"software": w.software_version, "protocol": w.protocol_versions},
           "platform": w.platform, "capabilities": w.capabilities})
}

async fn list_workers(State(st): State<St>, _a: Authed) -> ApiResult {
    let workers: Vec<Value> = st.db.workers_list()?.iter().map(|w| worker_json(&st, w)).collect();
    Ok(json_ok(StatusCode::OK, json!({"workers": workers})))
}

async fn add_worker(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
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

/// `GET /missions` query: `q` (case-insensitive substring of name, id, folder or worker), `worker`,
/// `status` (comma list), `sort` (`name`, `status`, `worker`, `updated`), `order` (`asc`, `desc`),
/// `limit` (1-500) and `offset`. Without `limit` every match is returned. The reply carries
/// `total` (matches before paging) and `statuses` (match counts per status ignoring the status
/// filter, for filter chips).
async fn list_missions(State(st): State<St>, _a: Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    let bad = |m: &str| err(StatusCode::BAD_REQUEST, "invalid-request", m.to_string());
    let num = |k: &str| -> Result<Option<usize>, ApiError> {
        q.get(k)
            .map(|v| v.parse::<usize>().map_err(|_| bad(&format!("{k} must be a non-negative integer"))))
            .transpose()
    };
    let limit = num("limit")?.map(|l| l.clamp(1, 500));
    let offset = num("offset")?.unwrap_or(0);
    let sort = q.get("sort").map(String::as_str);
    if !matches!(sort, None | Some("name" | "status" | "worker" | "updated")) {
        return Err(bad("sort must be name, status, worker or updated"));
    }
    let descending = match q.get("order").map(String::as_str) {
        None | Some("asc") => false,
        Some("desc") => true,
        Some(_) => return Err(bad("order must be asc or desc")),
    };
    let needle = q.get("q").map(|v| v.to_lowercase()).filter(|v| !v.is_empty());
    let statuses: Vec<String> = q
        .get("status")
        .map(|v| v.split(',').filter(|s| !s.is_empty()).map(str::to_uppercase).collect())
        .unwrap_or_default();
    let workers: HashMap<String, crate::db::WorkerRow> = st.db.workers_list()?.into_iter().map(|w| (w.name.clone(), w)).collect();
    let text = |info: &Value, k: &str| info.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let mut rows: Vec<(String, bool, Value)> = st
        .db
        .missions_list()?
        .into_iter()
        .filter_map(|(worker, info)| {
            let w = workers.get(&worker)?;
            // The worker advances mission_cursor atomically with its event batch and
            // mission snapshot. Unrelated events no longer make a caught-up worker stale.
            let stale = !st.hub.online(&worker) || w.event_cursor > w.mission_cursor;
            Some((worker, stale, info))
        })
        .filter(|(worker, _, info)| {
            q.get("worker").is_none_or(|w| w == worker)
                && needle.as_ref().is_none_or(|n| {
                    [worker.as_str(), &text(info, "name"), &text(info, "id"), &text(info, "folder")]
                        .iter()
                        .any(|f| f.to_lowercase().contains(n.as_str()))
                })
        })
        .collect();
    let mut by_status: std::collections::BTreeMap<String, usize> = std::collections::BTreeMap::new();
    for (_, _, info) in &rows {
        *by_status.entry(text(info, "status")).or_default() += 1;
    }
    rows.retain(|(_, _, info)| statuses.is_empty() || statuses.contains(&text(info, "status").to_uppercase()));
    if let Some(sort) = sort {
        let key = |r: &(String, bool, Value)| match sort {
            "name" => text(&r.2, "name").to_lowercase(),
            "status" => text(&r.2, "status"),
            "worker" => r.0.clone(),
            _ => text(&r.2, "updated_at"),
        };
        rows.sort_by(|a, b| key(a).cmp(&key(b)).then_with(|| text(&a.2, "id").cmp(&text(&b.2, "id"))));
        if descending {
            rows.reverse();
        }
    }
    let total = rows.len();
    let page = rows.into_iter().skip(offset).take(limit.unwrap_or(usize::MAX));
    let missions: Vec<Value> = page
        .map(|(worker, stale, info)| json!({"worker": worker, "stale": stale, "mission": info}))
        .collect();
    Ok(json_ok(
        StatusCode::OK,
        json!({"missions": missions, "total": total, "offset": offset, "statuses": by_status}),
    ))
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

async fn post_command(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
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
    let keepalive = (
        Duration::from_secs(st.cfg.ws_ping_interval_secs),
        Duration::from_secs(st.cfg.ws_idle_timeout_secs),
    );
    Ok(ws
        .max_message_size(64 * 1024)
        .max_frame_size(64 * 1024)
        .on_upgrade(move |s| stream_session(s, rx, shutdown, keepalive))
        .into_response())
}

/// Pushes updates to a browser. The server pings every `ping` and closes the socket once nothing
/// (no pong, no message) was received for `idle` (NET-010).
async fn stream_session(mut socket: WebSocket, mut rx: broadcast::Receiver<Arc<str>>, mut shutdown: watch::Receiver<bool>, (ping, idle): (Duration, Duration)) {
    let mut tick = tokio::time::interval(ping);
    tick.tick().await;
    let mut last_heard = Instant::now();
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
                Some(Ok(_)) => last_heard = Instant::now(),
            },
            _ = tick.tick() => {
                if last_heard.elapsed() >= idle { let _ = socket.send(close(1001, "idle timeout")).await; return }
                if socket.send(WsMessage::Ping(Vec::new().into())).await.is_err() { return }
            }
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

/// Who may call a route without further credentials.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Access {
    /// Anyone (health, auth state, setup, login; the SPA shell is the router fallback).
    Public,
    /// An admin session cookie or an API bearer token (the `Authed` extractor).
    Authed,
    /// A worker token as bearer (worker WebSocket and worker terminal attach).
    WorkerToken,
}

/// Every route [`router`] serves with the methods it accepts and its access class.
/// `tests/routes.rs` checks this table against the `.route(...)` lines of `router` and then
/// requests each entry without credentials, so a route added to one but not the other fails.
pub const ROUTES: &[(&str, &[&str], Access)] = &[
    ("/healthz", &["GET"], Access::Public),
    ("/api/v1/auth/state", &["GET"], Access::Public),
    ("/api/v1/auth/setup", &["POST"], Access::Public),
    ("/api/v1/auth/login", &["POST"], Access::Public),
    ("/api/v1/auth/logout", &["POST"], Access::Authed),
    ("/api/v1/auth/password", &["POST"], Access::Authed),
    ("/api/v1/workers", &["GET", "POST"], Access::Authed),
    ("/api/v1/workers/{name}", &["DELETE"], Access::Authed),
    ("/api/v1/missions", &["GET"], Access::Authed),
    ("/api/v1/commands", &["POST"], Access::Authed),
    ("/api/v1/commands/{id}", &["GET"], Access::Authed),
    ("/api/v1/events", &["GET"], Access::Authed),
    ("/api/v1/stream", &["GET"], Access::Authed),
    ("/api/v1/diagnostics", &["GET"], Access::Authed),
    ("/api/v1/worker/connect", &["GET"], Access::WorkerToken),
    ("/api/v1/terminal", &["GET"], Access::Authed),
    ("/api/v1/worker/terminal/{session_id}", &["GET"], Access::WorkerToken),
];

pub fn router(st: St) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/v1/auth/state", get(auth_state))
        .route("/api/v1/auth/setup", post(setup).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/login", post(login).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/logout", post(logout).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/password", post(change_password).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/workers", get(list_workers).post(add_worker))
        .route("/api/v1/workers/{name}", delete(revoke_worker))
        .route("/api/v1/missions", get(list_missions))
        .route("/api/v1/commands", post(post_command))
        .route("/api/v1/commands/{id}", get(get_command))
        .route("/api/v1/events", get(list_events))
        .route("/api/v1/stream", get(stream))
        .route("/api/v1/diagnostics", get(diagnostics))
        .route("/api/v1/worker/connect", get(crate::ws_worker::connect))
        .route("/api/v1/terminal", get(crate::terminal::browser))
        .route("/api/v1/worker/terminal/{session_id}", get(crate::terminal::worker))
        .fallback(static_files)
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(axum::middleware::from_fn_with_state(st.clone(), reserve_gate))
        .layer(axum::middleware::map_response(security_headers))
        .with_state(st)
}

pub fn with_peer(app: Router, peer: SocketAddr) -> Router {
    app.layer(Extension(Peer(peer)))
}

/// Which connection slots a TCP connection holds (see `server.rs`).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tier {
    General,
    /// Accepted from the slots kept for workers while the general slots were full.
    Reserve,
}

pub fn with_tier(app: Router, tier: Tier) -> Router {
    app.layer(Extension(tier))
}

/// A connection in the worker reserve may only carry a worker WebSocket request with a valid
/// worker token; anything else is refused and the connection closed, so unauthenticated clients
/// cannot occupy the reserve for longer than the (short) header timeout.
async fn reserve_gate(State(st): State<St>, req: Request, next: axum::middleware::Next) -> Response {
    if req.extensions().get::<Tier>() == Some(&Tier::Reserve) {
        let valid_worker = req.uri().path().starts_with("/api/v1/worker/")
            && req
                .headers()
                .get(header::AUTHORIZATION)
                .and_then(|h| h.to_str().ok())
                .and_then(|s| s.strip_prefix("Bearer "))
                .is_some_and(|t| matches!(st.db.worker_by_token(&auth::sha256_hex(t)), Ok(Some(_))));
        if !valid_worker {
            return err(
                StatusCode::SERVICE_UNAVAILABLE,
                "busy",
                "the coordinator is at its connection limit; only workers are accepted",
            )
            .into_response();
        }
    }
    next.run(req).await
}
