//! HTTP API under `/api/v1`, authentication extractor and WebSocket `/stream`.

use crate::auth::{self, MutationLimiter, RateLimiter};
use crate::config::Config;
use crate::db::{CommandRow, Db, InsertOutcome};
use crate::hub::{Hub, SendResult};
use crate::protocol::{self, valid_id, valid_name};
use crate::scopes;
use axum::body::Bytes;
use axum::extract::ws::{CloseFrame, Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{FromRequest, Path, Query, Request, State};
use axum::http::request::Parts;
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{extract::DefaultBodyLimit, extract::FromRequestParts, Extension, Router};
use base64::Engine;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{broadcast, watch, OwnedSemaphorePermit, Semaphore};

pub const MAX_BODY: usize = 1024 * 1024;
pub const MAX_JSON_DEPTH: usize = 4;
pub const MAX_JSON_STRING_BYTES: usize = 64 * 1024;
pub const MAX_JSON_ARRAY_ITEMS: usize = 1000;
pub const MAX_JSON_OBJECT_FIELDS: usize = 128;
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

#[derive(Clone, Debug)]
pub struct WorkerCertificate(pub Option<String>);

pub fn with_worker_certificate(app: Router, fingerprint_sha256: Option<String>) -> Router {
    app.layer(Extension(WorkerCertificate(fingerprint_sha256)))
}

pub struct AppState {
    pub db: Arc<Db>,
    pub hub: Arc<Hub>,
    pub cfg: Config,
    pub data_dir: PathBuf,
    pub limiter: RateLimiter,
    pub mutation_limiter: MutationLimiter,
    pub started: Instant,
    pub dummy_hash: String,
    pub cursor_key: String,
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
    correlation_id: Option<String>,
}

pub fn err(status: StatusCode, code: &'static str, message: impl Into<String>) -> ApiError {
    let correlation_id = status.is_server_error().then(auth::random_token);
    if let Some(id) = &correlation_id {
        eprintln!("coordinator internal error correlation_id={id}");
    }
    ApiError {
        status,
        code,
        message: message.into(),
        retry_after: None,
        correlation_id,
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(_error: rusqlite::Error) -> Self {
        err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "internal error")
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = self.status;
        let is_internal = status.is_server_error();
        let code = if is_internal { "internal" } else { self.code };
        let message = if is_internal { "internal server error" } else { &self.message };
        let mut problem = json!({
            "type": format!("https://llm-tools.dev/problems/{code}"),
            "title": "Coordinator request failed",
            "status": status.as_u16(),
            "detail": message,
            "code": code,
            "error": {"code": code, "message": message}
        });
        if let Some(id) = &self.correlation_id {
            problem["correlation_id"] = json!(id);
        }
        let mut r = (status, axum::Json(problem)).into_response();
        if let Some(id) = self.correlation_id {
            if let Ok(value) = id.parse::<header::HeaderValue>() {
                r.headers_mut().insert("x-correlation-id", value);
            }
        }
        r.headers_mut()
            .insert(header::CONTENT_TYPE, "application/problem+json".parse().expect("static content type"));
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

fn json_ok(status: StatusCode, mut v: Value) -> Response {
    normalize_api_timestamps(&mut v);
    (status, axum::Json(v)).into_response()
}

/// Persisted times are Unix seconds, but timestamps crossing the public JSON API use RFC 3339 UTC.
/// The field-name allowlist also normalizes timestamps inside worker-provided resource projections.
fn normalize_api_timestamps(value: &mut Value) {
    const FIELDS: &[&str] = &[
        "created_at",
        "updated_at",
        "last_seen",
        "expires_at",
        "last_used",
        "last_used_at",
        "occurred_at",
        "timestamp",
        "ts",
    ];
    match value {
        Value::Object(fields) => {
            for (name, value) in fields {
                if FIELDS.contains(&name.as_str()) {
                    if let Some(epoch) = value.as_i64() {
                        *value = json!(timestamp(Some(epoch)));
                    }
                } else {
                    normalize_api_timestamps(value);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(normalize_api_timestamps),
        _ => {}
    }
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
    if matches!(req.method(), &Method::POST | &Method::PUT | &Method::PATCH) {
        let is_json = req
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                let mut parts = value.split(';');
                parts
                    .next()
                    .is_some_and(|media_type| media_type.trim().eq_ignore_ascii_case("application/json"))
                    && parts.all(|parameter| {
                        let (name, value) = parameter.split_once('=').unwrap_or((parameter, ""));
                        name.trim().eq_ignore_ascii_case("charset") && value.trim().trim_matches('"').eq_ignore_ascii_case("utf-8")
                    })
            });
        if !is_json {
            return Err(err(
                StatusCode::UNSUPPORTED_MEDIA_TYPE,
                "unsupported-media-type",
                "mutations require Content-Type: application/json",
            ));
        }
    }
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
        Ok(value @ Value::Object(_)) => {
            check_json_limits(&value, 0)?;
            let Value::Object(m) = value else { unreachable!() };
            Ok(m)
        }
        _ => Err(err(StatusCode::BAD_REQUEST, "invalid-request", "request body must be a JSON object")),
    }
}

fn check_json_limits(value: &Value, depth: usize) -> Result<(), ApiError> {
    if depth > MAX_JSON_DEPTH {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "JSON nesting exceeds the maximum depth"));
    }
    match value {
        Value::String(text) if text.len() > MAX_JSON_STRING_BYTES => Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "JSON strings must not exceed 65536 UTF-8 bytes",
        )),
        Value::Array(values) => {
            if values.len() > MAX_JSON_ARRAY_ITEMS {
                return Err(err(
                    StatusCode::BAD_REQUEST,
                    "invalid-request",
                    "JSON arrays must not contain more than 1000 items",
                ));
            }
            for child in values {
                check_json_limits(child, depth + 1)?;
            }
            Ok(())
        }
        Value::Object(fields) => {
            if fields.len() > MAX_JSON_OBJECT_FIELDS {
                return Err(err(
                    StatusCode::BAD_REQUEST,
                    "invalid-request",
                    "JSON objects must not contain more than 128 fields",
                ));
            }
            for (key, child) in fields {
                if key.len() > 128 {
                    return Err(err(
                        StatusCode::BAD_REQUEST,
                        "invalid-request",
                        "JSON field names must not exceed 128 UTF-8 bytes",
                    ));
                }
                check_json_limits(child, depth + 1)?;
            }
            Ok(())
        }
        _ => Ok(()),
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
        principal_id: String,
        token_hash: String,
        csrf: String,
        remember: bool,
        expires_at: i64,
        idle_secs: i64,
    },
    Token {
        name: String,
        principal_id: String,
        scopes: Vec<String>,
        allowed_cidrs: Vec<String>,
        allowed_workers: Vec<String>,
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
        let token_record = if let Some(rest) = token.strip_prefix("lbr_pat_") {
            let Some((token_id, secret)) = rest.split_once('_') else {
                return Err(unauthorized());
            };
            if token_id.len() != 16
                || !token_id.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                || secret.len() != 64
                || !secret.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return Err(unauthorized());
            }
            st.db.token_lookup_identified(token_id, &auth::sha256_hex(secret))?
        } else {
            st.db.token_lookup(&auth::sha256_hex(token))?
        };
        return match token_record {
            Some(token) => Ok(Principal::Token {
                name: token.name,
                principal_id: token.principal_id,
                scopes: token.scopes,
                allowed_cidrs: token.allowed_cidrs,
                allowed_workers: token.allowed_workers,
            }),
            None => Err(unauthorized()),
        };
    }
    let cookie = cookie_value(headers).ok_or_else(unauthorized)?;
    let hash = auth::sha256_hex(&cookie);
    match st.db.session_get(&hash)? {
        Some(s) => Ok(Principal::Session {
            principal_id: s.principal_id,
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
    if cfg.allowed_origins.iter().any(|o| o == origin) || cfg.public_origin.as_deref() == Some(origin) {
        return true;
    }
    headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|h| origin.eq_ignore_ascii_case(&format!("https://{h}")))
}

pub struct Authed(pub Principal, pub String);

fn resolved_client_address(parts: &Parts, state: &St) -> String {
    let Some(peer) = parts.extensions.get::<Peer>().map(|p| p.0.ip()) else {
        return String::new();
    };
    let forwarded = parts.headers.get("forwarded").and_then(|v| v.to_str().ok());
    let x_forwarded_for = parts.headers.get("x-forwarded-for").and_then(|v| v.to_str().ok());
    crate::client_ip::resolve(peer, forwarded, x_forwarded_for, &state.cfg.trusted_proxies).to_string()
}

impl Principal {
    pub fn principal_id(&self) -> &str {
        match self {
            Principal::Session { principal_id, .. } | Principal::Token { principal_id, .. } => principal_id,
        }
    }

    pub fn actor(&self) -> String {
        match self {
            Principal::Session { .. } => "admin".into(),
            Principal::Token { name, .. } => format!("token:{name}"),
        }
    }

    fn worker_restrictions(&self) -> &[String] {
        match self {
            Principal::Session { .. } => &[],
            Principal::Token { allowed_workers, .. } => allowed_workers,
        }
    }

    fn allows_worker(&self, worker: &str) -> bool {
        let allowed = self.worker_restrictions();
        allowed.is_empty() || allowed.iter().any(|name| name == worker)
    }
}

fn query_values(parts: &Parts, key: &str) -> Vec<String> {
    parts
        .uri
        .query()
        .into_iter()
        .flat_map(|query| url::form_urlencoded::parse(query.as_bytes()))
        .filter_map(|(candidate, value)| (candidate == key).then(|| value.into_owned()))
        .collect()
}

/// Bound tokens may enter worker-owned resources only through a route that names one worker.
/// Global and low-level command routes are denied because they cannot safely apply this object
/// restriction at the router boundary.
fn worker_bound_token_request_allowed(parts: &Parts, principal: &Principal) -> bool {
    if principal.worker_restrictions().is_empty() {
        return true;
    }
    let path = parts.uri.path();
    let method = parts.method.as_str();
    if path == "/api/v1/workers" {
        return method == "GET";
    }
    if path == "/api/v1/missions" {
        return matches!(method, "GET" | "POST");
    }
    if path == "/api/v1/auth/state" || path == "/api/v1/auth/token" {
        return method == "GET";
    }
    let segments: Vec<&str> = path.split('/').filter(|segment| !segment.is_empty()).collect();
    if segments.len() >= 4 && segments[..3] == ["api", "v1", "workers"] {
        return !segments[3].contains('%') && principal.allows_worker(segments[3]);
    }
    if (segments.len() >= 4 && segments[..3] == ["api", "v1", "missions"]) || path == "/api/v1/terminal" {
        let workers = query_values(parts, "worker");
        return workers.len() == 1 && principal.allows_worker(&workers[0]);
    }
    false
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
        if let Principal::Token {
            name,
            scopes: granted,
            allowed_cidrs,
            ..
        } = &principal
        {
            if !allowed_cidrs.is_empty() {
                let remote = resolved_client_address(parts, st);
                let address = remote.parse::<std::net::IpAddr>().ok();
                if address.is_none_or(|address| !crate::client_ip::address_matches_any(address, allowed_cidrs)) {
                    let request_id = parts
                        .headers
                        .get("idempotency-key")
                        .or_else(|| parts.headers.get("x-request-id"))
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or("-");
                    let detail = format!("method={} path={} request_id={}", parts.method, parts.uri.path(), clean(request_id));
                    st.db
                        .audit_checked(&format!("token:{name}"), "authorization.denied.address", &detail, &remote)?;
                    return Err(err(
                        StatusCode::FORBIDDEN,
                        "token-address-denied",
                        "request address is outside this token's allowed networks",
                    ));
                }
            }
            let path = parts.uri.path();
            let route = ROUTES
                .iter()
                .find(|route| route_matches(route.0, path) && route.1.contains(&parts.method.as_str()));
            let Some((_, _, access, required)) = route else {
                return Err(err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "route has no authorization policy"));
            };
            if *access == Access::Session {
                return Err(err(StatusCode::FORBIDDEN, "session-required", "a browser session is required"));
            }
            let required = if *required == "terminal:conditional" {
                let mode = parts.uri.query().unwrap_or_default().split('&').find_map(|pair| {
                    let (key, value) = pair.split_once('=')?;
                    (key == "mode").then_some(value)
                });
                if mode == Some("write") {
                    scopes::TERMINAL_WRITE
                } else {
                    scopes::TERMINAL_VIEW
                }
            } else {
                *required
            };
            if !scopes::grants(granted, required) {
                let remote = resolved_client_address(parts, st);
                let request_id = parts
                    .headers
                    .get("idempotency-key")
                    .or_else(|| parts.headers.get("x-request-id"))
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("-");
                let detail = format!(
                    "method={} path={} granted={} required={} request_id={}",
                    parts.method,
                    parts.uri.path(),
                    granted.join(","),
                    required,
                    clean(request_id)
                );
                st.db.audit_checked(&format!("token:{name}"), "authorization.denied", &detail, &remote)?;
                return Err(err(StatusCode::FORBIDDEN, "insufficient-scope", format!("token requires scope {required}")));
            }
        }
        if matches!(principal, Principal::Token { .. }) && !worker_bound_token_request_allowed(parts, &principal) {
            let name = match &principal {
                Principal::Token { name, .. } => name.as_str(),
                Principal::Session { .. } => unreachable!(),
            };
            let remote = resolved_client_address(parts, st);
            let request_id = parts
                .headers
                .get("idempotency-key")
                .or_else(|| parts.headers.get("x-request-id"))
                .and_then(|value| value.to_str().ok())
                .unwrap_or("-");
            let detail = format!("method={} path={} request_id={}", parts.method, parts.uri.path(), clean(request_id));
            st.db.audit_checked(&format!("token:{name}"), "authorization.denied.worker", &detail, &remote)?;
            return Err(err(
                StatusCode::FORBIDDEN,
                "token-worker-denied",
                "request is outside this token's allowed workers",
            ));
        }
        let remote = resolved_client_address(parts, st);
        if let Principal::Token { name, scopes: granted, .. } = &principal {
            let request_id = parts
                .headers
                .get("idempotency-key")
                .or_else(|| parts.headers.get("x-request-id"))
                .and_then(|value| value.to_str().ok())
                .unwrap_or("-");
            let detail = format!(
                "method={} path={} scopes={} request_id={}",
                parts.method,
                parts.uri.path(),
                granted.join(","),
                clean(request_id)
            );
            st.db.audit_checked(&format!("token:{name}"), "token.use", &detail, &remote)?;
            st.db.api_token_record_use(name, &remote)?;
            if !matches!(parts.method, Method::GET | Method::HEAD | Method::OPTIONS) {
                let address_key = client_key(&remote);
                let keys = [
                    (format!("token:{name}"), st.cfg.mutation_limit_per_token),
                    (format!("address:{address_key}"), st.cfg.mutation_limit_per_address),
                    (auth::GLOBAL_MUTATION_BUDGET_KEY.to_string(), st.cfg.mutation_limit_global),
                ];
                if let Some(retry_after) = st.mutation_limiter.check(&keys) {
                    let mut error = err(
                        StatusCode::TOO_MANY_REQUESTS,
                        "mutation-rate-limit",
                        "mutation rate limit exceeded; retry after the indicated delay",
                    );
                    error.retry_after = Some(retry_after);
                    return Err(error);
                }
            }
        }
        if matches!(&principal, Principal::Session { .. }) && !matches!(parts.method, Method::GET | Method::HEAD | Method::OPTIONS) {
            check_address_mutation_limit(st, &remote)?;
        }
        Ok(Authed(principal, remote))
    }
}

pub struct Remote(pub String);
impl FromRequestParts<St> for Remote {
    type Rejection = std::convert::Infallible;
    async fn from_request_parts(parts: &mut Parts, state: &St) -> Result<Self, Self::Rejection> {
        Ok(Remote(resolved_client_address(parts, state)))
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

async fn hash_recovery_blocking(st: &AppState, code: String) -> String {
    let _slot = st.argon_slots.acquire().await.expect("semaphore open");
    tokio::task::spawn_blocking(move || auth::hash_recovery_code(&code)).await.expect("hash task")
}

async fn verify_recovery_blocking(st: &AppState, code: String, phc: String) -> bool {
    let _slot = st.argon_slots.acquire().await.expect("semaphore open");
    tokio::task::spawn_blocking(move || auth::verify_recovery_code(&code, &phc))
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

async fn auth_state(State(st): State<St>, headers: HeaderMap, Remote(ip): Remote) -> ApiResult {
    let admin = st.db.admin_get()?;
    let setup_required = admin.is_none();
    let configured_origin = match (st.cfg.public_origin.clone(), admin.as_ref()) {
        (Some(origin), _) => Some(origin),
        (None, Some((username, _))) => st.db.passkey_origin(username)?,
        (None, None) => None,
    };
    let request_origin = headers
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
        .or_else(|| headers.get(header::HOST).and_then(|v| v.to_str().ok()).map(|host| format!("https://{host}")))
        .and_then(|origin| crate::passkeys::canonical_origin(&origin).ok());
    let passkey_sign_in_available = if let Some((username, _)) = &admin {
        configured_origin.is_some()
            && configured_origin == request_origin
            && crate::passkeys::verifier(request_origin.as_deref().unwrap_or_default()).is_ok()
            && st.db.passkey_count(username)? > 0
    } else {
        false
    };
    let mut body = json!({
        "setup_required": setup_required,
        "authenticated": false,
        "passkey_sign_in_available": passkey_sign_in_available,
        "passkey_canonical_origin": configured_origin,
        "password_policy": {"min_length": auth::MIN_PASSWORD_LEN, "max_length": auth::MAX_PASSWORD_LEN},
        "session_policy": {
            "remembered": {"idle_secs": crate::db::session_policy(true).0, "absolute_secs": crate::db::session_policy(true).1},
            "default": {"idle_secs": crate::db::session_policy(false).0, "absolute_secs": crate::db::session_policy(false).1},
        },
    });
    match authenticate(&st, &headers) {
        Ok(Principal::Session {
            csrf,
            remember,
            expires_at,
            idle_secs,
            ..
        }) => {
            body["authenticated"] = json!(true);
            body["csrf_token"] = json!(csrf);
            body["username"] = json!(st.db.admin_get()?.map(|(u, _)| u));
            body["session"] = json!({"remember": remember, "expires_at": expires_at, "idle_timeout_secs": idle_secs});
        }
        Ok(Principal::Token {
            name,
            scopes: granted,
            allowed_cidrs,
            ..
        }) => {
            if !allowed_cidrs.is_empty()
                && !ip
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|address| crate::client_ip::address_matches_any(address, &allowed_cidrs))
            {
                st.db.audit_checked(
                    &format!("token:{name}"),
                    "authorization.denied.address",
                    "method=GET path=/api/v1/auth/state request_id=-",
                    &ip,
                )?;
                return Err(err(
                    StatusCode::FORBIDDEN,
                    "token-address-denied",
                    "request address is outside this token's allowed networks",
                ));
            }
            let request_id = headers
                .get("idempotency-key")
                .or_else(|| headers.get("x-request-id"))
                .and_then(|v| v.to_str().ok())
                .unwrap_or("-");
            let detail = format!(
                "method=GET path=/api/v1/auth/state scopes={} request_id={}",
                granted.join(","),
                clean(request_id)
            );
            st.db.audit_checked(&format!("token:{name}"), "token.use", &detail, &ip)?;
            st.db.api_token_record_use(&name, &ip)?;
            body["authenticated"] = json!(true);
        }
        Err(_) => {}
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
    let mut attempt = match st.limiter.acquire(&[format!("ip:{addr}")], known_addr) {
        Ok(attempt) => attempt,
        Err(retry) => {
            security_notification(&st, "admin", "repeated_sign_in_failures");
            return Err(limited(retry));
        }
    };
    let body = match read_body(&st, req).await {
        Ok(b) => b,
        Err(e) => {
            attempt.release();
            return Err(e);
        }
    };
    let parsed = parse_body(&body.bytes).and_then(|m| {
        no_extra(&m, &["username", "password", "remember", "totp_code", "recovery_code"])?;
        let remember = match m.get("remember") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(b)) => *b,
            _ => return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "remember must be true or false")),
        };
        let totp = m.get("totp_code").and_then(Value::as_str).map(str::to_owned);
        let recovery = m.get("recovery_code").and_then(Value::as_str).map(str::to_owned);
        if totp.is_some() && recovery.is_some() {
            return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "provide one MFA code"));
        }
        if totp.as_ref().is_some_and(|v| v.len() > 16) || recovery.as_ref().is_some_and(|v| v.len() > 64) {
            return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "MFA code is too long"));
        }
        Ok((
            str_field(&m, "username")?.to_string(),
            str_field(&m, "password")?.to_string(),
            remember,
            totp,
            recovery,
        ))
    });
    drop(body);
    let (username, password, remember, totp_code, recovery_code) = match parsed {
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
    let principal = st.db.admin_principal_id()?.unwrap_or_else(|| "admin".into());
    let totp = st.db.totp_get(&principal)?;
    let totp_enabled = totp.as_ref().is_some_and(|(_, enabled, _)| *enabled);
    if password_login_blocked(st.cfg.exposure, totp_enabled) {
        attempt.fail();
        st.db
            .audit(&clean(&username), "login.failure", "Internet exposure requires TOTP for password sign-in", &ip);
        return Err(err(
            StatusCode::UNAUTHORIZED,
            "invalid-credentials",
            "password sign-in requires a second factor in Internet mode; use a passkey or enable TOTP",
        ));
    }
    let mut used_recovery_code = false;
    if let Some((secret, enabled, _)) = totp {
        if enabled {
            let mut mfa_ok = false;
            if let Some(code) = totp_code.as_deref() {
                if let Some(step) = auth::matching_totp_step(&secret, code, crate::now()) {
                    mfa_ok = st.db.totp_step_consume(&principal, step)?;
                }
            } else if let Some(code) = recovery_code.as_deref() {
                let hashes = st.db.recovery_code_hashes(&principal)?;
                let mut matched = None;
                // Check the whole bounded set so the matching code's position is not exposed.
                for hash in hashes {
                    if verify_recovery_blocking(&st, code.to_string(), hash.clone()).await && matched.is_none() {
                        matched = Some(hash);
                    }
                }
                if let Some(hash) = matched {
                    mfa_ok = st.db.recovery_code_consume(&principal, &hash)?;
                    used_recovery_code = mfa_ok;
                }
            }
            if !mfa_ok {
                attempt.fail();
                st.db.audit(&clean(&username), "login.failure", "invalid credentials", &ip);
                return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
            }
        }
    } else if totp_code.is_some() || recovery_code.is_some() {
        attempt.fail();
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "invalid username or password"));
    }
    attempt.succeed();
    st.db.known_addr_remember(&addr)?;
    if !known_addr {
        security_notification(&st, &username, "new_sign_in_address");
    }
    remember_login_device(&st, &username, &headers)?;
    let (token, csrf) = (auth::random_token(), auth::random_token());
    st.db.session_create_for(&principal, &auth::sha256_hex(&token), &csrf, remember)?;
    st.db
        .audit(&username, "login.success", if used_recovery_code { "recovery code" } else { "" }, &ip);
    if used_recovery_code {
        security_notification(&st, &principal, "recovery_code_used");
    }
    let mut r = json_ok(StatusCode::OK, json!({"csrf_token": csrf}));
    r.headers_mut().insert(
        header::SET_COOKIE,
        session_cookie(&token, remember.then_some(crate::db::SESSION_ABSOLUTE_SECS as u64))
            .parse()
            .expect("cookie"),
    );
    Ok(r)
}

fn password_login_blocked(exposure: crate::config::Exposure, totp_enabled: bool) -> bool {
    exposure == crate::config::Exposure::Internet && !totp_enabled
}

fn webauthn_origin(st: &AppState, headers: &HeaderMap) -> Result<String, ApiError> {
    if !origin_ok(headers, &st.cfg) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "bad-origin",
            "Origin header missing or not the coordinator's origin",
        ));
    }
    let origin = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()).unwrap_or("");
    let canonical = crate::passkeys::canonical_origin(origin)
        .map_err(|_| err(StatusCode::NOT_IMPLEMENTED, "passkeys-unavailable", "passkeys require a trusted HTTPS DNS name"))?;
    let principal = st.db.admin_get()?.map(|(username, _)| username);
    let stored = match principal.as_deref() {
        Some(username) => st.db.passkey_origin(username)?,
        None => None,
    };
    let expected = st.cfg.public_origin.as_deref().or(stored.as_deref());
    if expected.is_none() {
        if let Some(username) = principal.as_deref() {
            if st.db.passkey_count(username)? > 0 {
                return Err(err(
                    StatusCode::CONFLICT,
                    "canonical-origin-required",
                    "existing passkeys need an explicitly configured canonical origin before they can be used",
                ));
            }
        }
    }
    if expected.is_some_and(|expected| expected != canonical) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "canonical-origin-required",
            "passkeys are available only at the coordinator's canonical origin",
        ));
    }
    crate::passkeys::verifier(&canonical).map_err(|_| err(StatusCode::NOT_IMPLEMENTED, "passkeys-unavailable", "passkeys require a trusted HTTPS DNS name"))?;
    Ok(canonical)
}

fn passkey_ticket(headers: &HeaderMap) -> Result<String, ApiError> {
    cookie_value_named(headers, "__Host-bridge_passkey").ok_or_else(unauthorized)
}

fn cookie_value_named(headers: &HeaderMap, name: &str) -> Option<String> {
    let prefix = format!("{name}=");
    for h in headers.get_all(header::COOKIE) {
        for part in h.to_str().ok()?.split(';') {
            if let Some(v) = part.trim().strip_prefix(prefix.as_str()) {
                return (!v.is_empty()).then(|| v.to_string());
            }
        }
    }
    None
}

fn passkey_ticket_cookie(token: &str) -> String {
    format!("__Host-bridge_passkey={token}; Path=/; Max-Age=300; Secure; HttpOnly; SameSite=Strict")
}

fn valid_challenge_id(id: &str) -> bool {
    id.len() == 64 && id.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RegisterStartBody {
    name: String,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RegisterFinishBody {
    challenge_id: String,
    credential: webauthn_rs::prelude::RegisterPublicKeyCredential,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct AssertFinishBody {
    challenge_id: String,
    credential: webauthn_rs::prelude::PublicKeyCredential,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PasskeyReenrollBody {
    current_password: String,
    new_origin: String,
    confirm: bool,
}

async fn passkey_register_start(State(st): State<St>, Authed(p, ip): Authed, headers: HeaderMap, body: Buffered) -> ApiResult {
    let Principal::Session { token_hash, .. } = p else {
        return Err(err(
            StatusCode::FORBIDDEN,
            "session-required",
            "passkey registration requires a browser session",
        ));
    };
    let origin = webauthn_origin(&st, &headers)?;
    let m = parse_body(&body.bytes)?;
    let body: RegisterStartBody = serde_json::from_value(Value::Object(m)).map_err(|_| {
        err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "name is required and unknown fields are not allowed",
        )
    })?;
    if body.name.trim().is_empty() || body.name.len() > 64 || body.name.chars().any(char::is_control) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "passkey name must be 1-64 printable characters",
        ));
    }
    let Some((username, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let wa = crate::passkeys::verifier(&origin)
        .map_err(|_| err(StatusCode::NOT_IMPLEMENTED, "passkeys-unavailable", "passkeys require a trusted HTTPS DNS name"))?;
    let (options, state) = crate::passkeys::begin_registration(&wa, &username, &username, &body.name)
        .map_err(|_| err(StatusCode::BAD_REQUEST, "webauthn-error", "could not start passkey registration"))?;
    let challenge =
        crate::passkeys::new_challenge(crate::now()).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not generate challenge"))?;
    let stored = crate::passkeys::StoredCeremony {
        origin,
        credential_name: Some(body.name),
        state,
    };
    let row = crate::db::WebauthnChallengeRow {
        principal_id: username.clone(),
        binding: token_hash,
        kind: "registration".into(),
        state: serde_json::to_string(&stored).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not store ceremony"))?,
        expires_at: challenge.expires_at,
    };
    st.db.webauthn_challenge_put(&challenge.id, &row)?;
    st.db
        .audit(&username, "passkey.registration.start", &format!("challenge={}", challenge.id), &ip);
    Ok(json_ok(StatusCode::OK, json!({"challenge_id":challenge.id,"options":options})))
}

async fn passkey_register_finish(State(st): State<St>, Authed(p, ip): Authed, headers: HeaderMap, body: Buffered) -> ApiResult {
    let Principal::Session { token_hash, .. } = p else {
        return Err(err(
            StatusCode::FORBIDDEN,
            "session-required",
            "passkey registration requires a browser session",
        ));
    };
    let m = parse_body(&body.bytes)?;
    let body: RegisterFinishBody =
        serde_json::from_value(Value::Object(m)).map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "challenge_id and credential are required"))?;
    if !valid_challenge_id(&body.challenge_id) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "challenge_id must be 64 lowercase hexadecimal characters",
        ));
    }
    let Some((username, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let Some(row) = st.db.webauthn_challenge_take(&body.challenge_id, &username, &token_hash, "registration")? else {
        return Err(err(
            StatusCode::GONE,
            "challenge-expired",
            "challenge is expired, consumed or bound to another session",
        ));
    };
    let stored: crate::passkeys::StoredCeremony =
        serde_json::from_str(&row.state).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "stored ceremony is invalid"))?;
    if headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) != Some(stored.origin.as_str()) {
        return Err(err(StatusCode::FORBIDDEN, "bad-origin", "ceremony origin changed"));
    }
    let wa = crate::passkeys::verifier(&stored.origin).map_err(|_| err(StatusCode::BAD_REQUEST, "webauthn-error", "invalid ceremony origin"))?;
    let credential = crate::passkeys::finish_registration(&wa, &stored.state, &body.credential)
        .map_err(|_| err(StatusCode::BAD_REQUEST, "webauthn-error", "passkey registration could not be verified"))?;
    use base64::Engine as _;
    let credential_id = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(credential.cred_id());
    let transports = serde_json::to_string(&body.credential.response.transports).unwrap_or_else(|_| "[]".into());
    let passkey_json = serde_json::to_string(&credential).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not store passkey"))?;
    let row = crate::db::PasskeyCredentialRow {
        credential_id,
        principal_id: username.clone(),
        credential: passkey_json,
        user_name: stored.credential_name.unwrap_or_else(|| "Passkey".into()),
        created_at: crate::now(),
        last_used: None,
        aaguid: crate::passkeys::aaguid_from_attestation_object(body.credential.response.attestation_object.as_ref()),
        transports,
        backup_eligible: false,
        backed_up: false,
        sign_count: 0,
    };
    match st.db.passkey_put_at_origin(&row, &stored.origin) {
        Ok(true) => {}
        Ok(false) => {
            st.db.audit(&username, "passkey.registration.denied", "canonical origin is already fixed", &ip);
            return Err(err(
                StatusCode::CONFLICT,
                "canonical-origin-locked",
                "passkeys are bound to the first registered origin; remove all passkeys and explicitly re-enrol to change it",
            ));
        }
        Err(rusqlite::Error::SqliteFailure(ref code, _))
            if code.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_PRIMARYKEY || code.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_UNIQUE =>
        {
            return Err(err(StatusCode::CONFLICT, "credential-exists", "this passkey is already registered"));
        }
        Err(e) => return Err(e.into()),
    }
    if st.cfg.public_origin.is_none() {
        if let Err(error) = crate::config::persist_public_origin(&st.data_dir, &stored.origin) {
            st.db.audit(&username, "passkey.origin-persist-failed", &clean(&error), &ip);
            eprintln!("could not persist the canonical passkey origin: {}", clean(&error));
        }
    }
    st.db
        .audit(&username, "passkey.registration.success", &format!("credential_id={}", row.credential_id), &ip);
    Ok(json_ok(
        StatusCode::CREATED,
        json!({"ok":true,"credential_id":row.credential_id,"name":row.user_name}),
    ))
}

async fn passkey_reenroll(State(st): State<St>, Authed(p, ip): Authed, headers: HeaderMap, body: Buffered) -> ApiResult {
    let Principal::Session { .. } = p else {
        return Err(err(
            StatusCode::FORBIDDEN,
            "session-required",
            "passkey re-enrolment requires a browser session",
        ));
    };
    let m = parse_body(&body.bytes)?;
    let body: PasskeyReenrollBody = serde_json::from_value(Value::Object(m)).map_err(|_| {
        err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "current_password, new_origin and confirm are required",
        )
    })?;
    if !body.confirm {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "confirmation-required",
            "confirm that all passkeys will be removed",
        ));
    }
    let request_origin = headers.get(header::ORIGIN).and_then(|value| value.to_str().ok()).unwrap_or("");
    if !origin_ok(&headers, &st.cfg) || body.new_origin != request_origin {
        return Err(err(
            StatusCode::FORBIDDEN,
            "bad-origin",
            "new_origin must be the exact allowed HTTPS request origin",
        ));
    }
    let origin = crate::passkeys::canonical_origin(&body.new_origin)
        .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-origin", "passkeys require a canonical HTTPS DNS origin"))?;
    if origin != body.new_origin {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-origin", "new_origin must be canonical"));
    }
    if body.current_password.len() > auth::MAX_PASSWORD_LEN {
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "current password is incorrect"));
    }
    let Some((username, password_hash)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    if !verify_blocking(&st, body.current_password, password_hash).await {
        st.db.audit(&username, "passkey.reenrol.failure", "current password did not verify", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "current password is incorrect"));
    }
    st.db.passkey_rebind_origin(&username, &origin)?;
    st.db.audit(&username, "passkey.origin.reenrolled", &clean(&origin), &ip);
    security_notification(&st, &username, "passkey_origin_reenrolled");
    Ok(json_ok(StatusCode::OK, json!({"ok":true,"origin":origin,"registration_required":true})))
}

async fn totp_enroll(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    if !matches!(p, Principal::Session { .. }) {
        return Err(err(StatusCode::FORBIDDEN, "session-required", "TOTP enrollment requires a browser session"));
    }
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["current_password"])?;
    let current = str_field(&m, "current_password")?;
    let Some((username, password_hash)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let principal = st.db.admin_principal_id()?.unwrap_or_else(|| "admin".into());
    if current.len() > auth::MAX_PASSWORD_LEN || !verify_blocking(&st, current.to_string(), password_hash).await {
        st.db.audit(&username, "totp.enrollment.failure", "current password did not verify", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "current password is incorrect"));
    }
    if st.db.totp_get(&principal)?.is_some_and(|(_, enabled, _)| enabled) {
        return Err(err(StatusCode::CONFLICT, "totp-already-enabled", "disable TOTP before enrolling a replacement"));
    }
    let secret = auth::new_totp_secret();
    st.db.totp_pending_set(&principal, &secret)?;
    st.db.audit(&username, "totp.enrollment.started", "", &ip);
    let label = format!("LLM%20Bridge:{}", username.replace('@', "%40"));
    let uri = format!("otpauth://totp/{label}?secret={secret}&issuer=LLM%20Bridge&algorithm=SHA1&digits=6&period=30");
    Ok(json_ok(StatusCode::OK, json!({"secret":secret,"otpauth_uri":uri,"confirmed":false})))
}

async fn totp_confirm(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    if !matches!(p, Principal::Session { .. }) {
        return Err(err(StatusCode::FORBIDDEN, "session-required", "TOTP enrollment requires a browser session"));
    }
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["code"])?;
    let code = str_field(&m, "code")?;
    let Some((username, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let principal = st.db.admin_principal_id()?.unwrap_or_else(|| "admin".into());
    let Some((secret, enabled, _)) = st.db.totp_get(&principal)? else {
        return Err(err(StatusCode::CONFLICT, "totp-enrollment-missing", "start TOTP enrollment first"));
    };
    if enabled {
        return Err(err(StatusCode::CONFLICT, "totp-already-enabled", "TOTP is already enabled"));
    }
    let Some(step) = auth::matching_totp_step(&secret, code, crate::now()) else {
        st.db.audit(&username, "totp.enrollment.failure", "invalid verification code", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-mfa-code", "invalid verification code"));
    };
    let codes: Vec<String> = (0..10).map(|_| auth::new_recovery_code()).collect();
    let mut hashes = Vec::with_capacity(codes.len());
    for code in &codes {
        hashes.push(hash_recovery_blocking(&st, code.clone()).await);
    }
    st.db.totp_enable_with_recovery_codes(&principal, &hashes, step)?;
    st.db.audit(&username, "totp.enabled", "10 recovery codes generated", &ip);
    security_notification(&st, &principal, "totp_enabled");
    Ok(json_ok(StatusCode::OK, json!({"ok":true,"recovery_codes":codes})))
}

async fn totp_disable(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    if !matches!(p, Principal::Session { .. }) {
        return Err(err(StatusCode::FORBIDDEN, "session-required", "TOTP management requires a browser session"));
    }
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["current_password", "code"])?;
    let current = str_field(&m, "current_password")?;
    let code = str_field(&m, "code")?;
    let Some((username, password_hash)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let principal = st.db.admin_principal_id()?.unwrap_or_else(|| "admin".into());
    let Some((secret, enabled, _)) = st.db.totp_get(&principal)? else {
        return Err(err(StatusCode::CONFLICT, "totp-not-enabled", "TOTP is not enabled"));
    };
    if current.len() > auth::MAX_PASSWORD_LEN || !verify_blocking(&st, current.to_string(), password_hash).await {
        st.db.audit(&username, "totp.disable.failure", "current password did not verify", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-credentials", "current password is incorrect"));
    }
    let Some(step) = enabled.then(|| auth::matching_totp_step(&secret, code, crate::now())).flatten() else {
        st.db.audit(&username, "totp.disable.failure", "invalid verification code", &ip);
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-mfa-code", "invalid verification code"));
    };
    if !st.db.totp_step_consume(&principal, step)? {
        return Err(err(StatusCode::UNAUTHORIZED, "invalid-mfa-code", "invalid verification code"));
    }
    st.db.totp_disable(&principal)?;
    st.db.audit(&username, "totp.disabled", "recovery codes deleted", &ip);
    security_notification(&st, &principal, "totp_disabled");
    Ok(json_ok(StatusCode::OK, json!({"ok":true})))
}

async fn list_passkeys(State(st): State<St>, _session: Authed) -> ApiResult {
    let Some((principal, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let passkeys = st
        .db
        .passkeys_for_principal(&principal)?
        .into_iter()
        .map(|row| {
            json!({
                "id": row.credential_id,
                "name": row.user_name,
                "provider_aaguid": row.aaguid,
                "created_at": timestamp(Some(row.created_at)),
                "last_used": timestamp(row.last_used),
                "synced": row.backup_eligible && row.backed_up,
                "backup_eligible": row.backup_eligible,
            })
        })
        .collect::<Vec<_>>();
    Ok(json_ok(StatusCode::OK, json!({"passkeys":passkeys})))
}

async fn passkey_assert_start(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, body: Buffered) -> ApiResult {
    check_public_origin(&st, &headers)?;
    check_address_mutation_limit(&st, &ip)?;
    let origin = webauthn_origin(&st, &headers)?;
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &[])?;
    let Some((username, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let records = st.db.passkeys_for_principal(&username)?;
    let credentials = records
        .iter()
        .map(|row| serde_json::from_str(&row.credential).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "stored passkey is invalid")))
        .collect::<Result<Vec<webauthn_rs::prelude::Passkey>, _>>()?;
    if credentials.is_empty() {
        return Err(err(StatusCode::NOT_FOUND, "unknown-credential", "no passkeys are registered"));
    }
    let wa = crate::passkeys::verifier(&origin)
        .map_err(|_| err(StatusCode::NOT_IMPLEMENTED, "passkeys-unavailable", "passkeys require a trusted HTTPS DNS name"))?;
    let (options, state) = crate::passkeys::begin_authentication(&wa, &credentials)
        .map_err(|_| err(StatusCode::BAD_REQUEST, "webauthn-error", "could not start passkey sign-in"))?;
    let challenge =
        crate::passkeys::new_challenge(crate::now()).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not generate challenge"))?;
    let ticket = auth::random_token();
    let stored = crate::passkeys::StoredCeremony {
        origin,
        credential_name: None,
        state,
    };
    let row = crate::db::WebauthnChallengeRow {
        principal_id: username.clone(),
        binding: auth::sha256_hex(&ticket),
        kind: "authentication".into(),
        state: serde_json::to_string(&stored).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not store ceremony"))?,
        expires_at: challenge.expires_at,
    };
    st.db.webauthn_challenge_put(&challenge.id, &row)?;
    st.db
        .audit("anonymous", "passkey.authentication.start", &format!("challenge={}", challenge.id), &ip);
    let mut r = json_ok(StatusCode::OK, json!({"challenge_id":challenge.id,"options":options}));
    r.headers_mut()
        .insert(header::SET_COOKIE, passkey_ticket_cookie(&ticket).parse().expect("static cookie"));
    Ok(r)
}

async fn passkey_assert_finish(State(st): State<St>, Remote(ip): Remote, headers: HeaderMap, body: Buffered) -> ApiResult {
    check_address_mutation_limit(&st, &ip)?;
    let origin = webauthn_origin(&st, &headers)?;
    let ticket = passkey_ticket(&headers)?;
    let m = parse_body(&body.bytes)?;
    let body: AssertFinishBody =
        serde_json::from_value(Value::Object(m)).map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "challenge_id and credential are required"))?;
    if !valid_challenge_id(&body.challenge_id) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "challenge_id must be 64 lowercase hexadecimal characters",
        ));
    }
    let Some((username, _)) = st.db.admin_get()? else {
        return Err(unauthorized());
    };
    let Some(row) = st
        .db
        .webauthn_challenge_take(&body.challenge_id, &username, &auth::sha256_hex(&ticket), "authentication")?
    else {
        return Err(err(
            StatusCode::GONE,
            "challenge-expired",
            "challenge is expired, consumed or bound to another browser",
        ));
    };
    let stored: crate::passkeys::StoredCeremony =
        serde_json::from_str(&row.state).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "stored ceremony is invalid"))?;
    if origin != stored.origin {
        return Err(err(StatusCode::FORBIDDEN, "bad-origin", "ceremony origin changed"));
    }
    let wa = crate::passkeys::verifier(&stored.origin).map_err(|_| err(StatusCode::BAD_REQUEST, "webauthn-error", "invalid ceremony origin"))?;
    let mut records = st.db.passkeys_for_principal(&username)?;
    let credentials = records
        .iter()
        .map(|row| serde_json::from_str(&row.credential).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "stored passkey is invalid")))
        .collect::<Result<Vec<webauthn_rs::prelude::Passkey>, _>>()?;
    let result = crate::passkeys::finish_authentication(&wa, &stored.state, &body.credential, &credentials)
        .map_err(|_| err(StatusCode::UNAUTHORIZED, "webauthn-error", "passkey assertion could not be verified"))?;
    if !result.user_verified() {
        return Err(err(StatusCode::UNAUTHORIZED, "user-verification-required", "passkey did not verify the user"));
    }
    let Some(index) = records.iter().position(|r| {
        base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(&r.credential_id)
            .is_ok_and(|id| id == result.cred_id().as_slice())
    }) else {
        return Err(err(StatusCode::UNAUTHORIZED, "unknown-credential", "credential is not registered"));
    };
    let credential: webauthn_rs::prelude::Passkey =
        serde_json::from_str(&records[index].credential).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "stored passkey is invalid"))?;
    let mut updated = credential;
    updated.update_credential(&result);
    records[index].credential = serde_json::to_string(&updated).map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "could not update passkey"))?;
    records[index].last_used = Some(crate::now());
    records[index].backup_eligible = result.backup_eligible();
    records[index].backed_up = result.backup_state();
    records[index].sign_count = result.counter();
    st.db.passkey_update(&records[index])?;
    remember_login_device(&st, &username, &headers)?;
    let (token, csrf) = (auth::random_token(), auth::random_token());
    st.db.session_create_for(
        &st.db.admin_principal_id()?.unwrap_or_else(|| "admin".into()),
        &auth::sha256_hex(&token),
        &csrf,
        false,
    )?;
    st.db.audit(
        &username,
        "passkey.authentication.success",
        &format!("credential_id={}", records[index].credential_id),
        &ip,
    );
    let mut r = json_ok(StatusCode::OK, json!({"csrf_token":csrf}));
    r.headers_mut()
        .insert(header::SET_COOKIE, session_cookie(&token, None).parse().expect("session cookie"));
    r.headers_mut().append(
        header::SET_COOKIE,
        "__Host-bridge_passkey=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
            .parse()
            .expect("static cookie"),
    );
    Ok(r)
}

/// Session mutations and public passkey ceremonies have no token-specific budget. Share the
/// coordinator's bounded per-address mutation budget so either path stays bounded.
fn check_address_mutation_limit(st: &St, remote: &str) -> Result<(), ApiError> {
    let address = remote
        .parse::<std::net::IpAddr>()
        .map(|ip| auth::addr_key(&ip))
        .unwrap_or_else(|_| "unknown".to_string());
    if let Some(retry_after) = st.mutation_limiter.check(&[
        (format!("address:{address}"), st.cfg.mutation_limit_per_address),
        (auth::GLOBAL_MUTATION_BUDGET_KEY.to_string(), st.cfg.mutation_limit_global),
    ]) {
        let mut error = err(
            StatusCode::TOO_MANY_REQUESTS,
            "mutation-rate-limit",
            "mutation rate limit exceeded; retry after the indicated delay",
        );
        error.retry_after = Some(retry_after);
        return Err(error);
    }
    Ok(())
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

async fn list_sessions(State(st): State<St>, Authed(p, _): Authed) -> ApiResult {
    let Principal::Session { principal_id, token_hash, .. } = p else {
        return Err(unauthorized());
    };
    Ok(json_ok(
        StatusCode::OK,
        json!({"sessions": st.db.session_list_for(&principal_id, &token_hash)?}),
    ))
}

async fn revoke_session(State(st): State<St>, Authed(p, ip): Authed, Path(id): Path<String>) -> ApiResult {
    let actor = p.actor();
    let Principal::Session { principal_id, token_hash, .. } = p else {
        return Err(unauthorized());
    };
    if !(id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit())
        || (32..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'))
    {
        return Err(err(StatusCode::NOT_FOUND, "not-found", "session not found"));
    }
    let revoked_hash = st.db.session_revoke_for(&principal_id, &id)?;
    let Some(revoked_hash) = revoked_hash else {
        return Err(err(StatusCode::NOT_FOUND, "not-found", "session not found"));
    };
    st.db.audit(&actor, "session.revoke", &format!("session {id}"), &ip);
    let mut response = json_ok(StatusCode::OK, json!({"ok":true}));
    if revoked_hash == token_hash {
        response
            .headers_mut()
            .insert(header::SET_COOKIE, session_cookie("", Some(0)).parse().expect("cookie"));
    }
    Ok(response)
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

fn timestamp(epoch: Option<i64>) -> Option<String> {
    let epoch = epoch?;
    if !(0..=253_402_300_799).contains(&epoch) {
        return None;
    }
    let days = epoch.div_euclid(86_400);
    let seconds = epoch.rem_euclid(86_400);
    let (year, month, day) = crate::certs::civil_from_days(days);
    Some(format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        seconds / 3600,
        seconds % 3600 / 60,
        seconds % 60
    ))
}

async fn list_tokens(State(st): State<St>, _a: Authed) -> ApiResult {
    let tokens: Vec<Value> = st
        .db
        .api_tokens_list()?
        .into_iter()
        .map(|t| {
            json!({
                "name": t.name,
                "id": t.token_id,
                "scopes": t.scopes,
                "created_at": timestamp(Some(t.created_at)),
                "last_used": timestamp(t.last_used),
                "last_address": t.last_address,
                "expires_at": timestamp(t.expires_at),
                "allowed_cidrs": t.allowed_cidrs,
                "allowed_workers": t.allowed_workers
            })
        })
        .collect();
    Ok(json_ok(StatusCode::OK, json!({"tokens": tokens})))
}

async fn audit_resource(State(st): State<St>, _admin: Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    if q.keys().any(|key| !matches!(key.as_str(), "limit" | "cursor")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let limit = q.get("limit").map_or(Ok(100_i64), |value| {
        value
            .parse::<i64>()
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "limit must be an integer between 1 and 500"))
    })?;
    if !(1..=500).contains(&limit) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "limit must be between 1 and 500"));
    }
    let filters = format!("limit={limit}");
    let offset = match q.get("cursor") {
        Some(cursor) => crate::cursor::read(&st.cursor_key, "audit", &filters, cursor).map_err(|_| {
            err(
                StatusCode::BAD_REQUEST,
                "invalid-cursor",
                "cursor is invalid or belongs to different audit filters",
            )
        })?,
        None => 0,
    };
    let mut entries = st.db.audit_list_page(limit + 1, offset.min(crate::db::MAX_AUDIT_ROWS as usize) as i64)?;
    let has_more = entries.len() > limit as usize;
    if has_more {
        entries.pop();
    }
    let next_cursor = has_more.then(|| crate::cursor::issue(&st.cursor_key, "audit", &filters, offset.saturating_add(entries.len())));
    Ok(json_ok(
        StatusCode::OK,
        json!({"entries":entries,"chain_valid":st.db.audit_chain_valid()?,"next_cursor":next_cursor}),
    ))
}

async fn create_token(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["name", "scopes", "expires_days", "allowed_cidrs", "allowed_workers"])?;
    let name = str_field(&m, "name")?;
    if !valid_name(name) {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-name",
            "token name must be lowercase letters, digits and '-' (at most 63)",
        ));
    }
    let scopes = match m.get("scopes") {
        None => vec![scopes::READ.to_string()],
        Some(Value::Array(values)) if !values.is_empty() && values.len() <= scopes::ALL.len() => {
            let mut parsed = Vec::with_capacity(values.len());
            for value in values {
                let scope = value
                    .as_str()
                    .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "scopes must be strings"))?;
                if !scopes::valid(scope) || parsed.iter().any(|known| known == scope) {
                    return Err(err(StatusCode::BAD_REQUEST, "invalid-scope", "scope is unknown or repeated"));
                }
                parsed.push(scope.to_string());
            }
            parsed
        }
        Some(_) => {
            return Err(err(
                StatusCode::BAD_REQUEST,
                "invalid-request",
                "scopes must be a non-empty array of known scopes",
            ))
        }
    };
    let days = match m.get("expires_days") {
        None => 90,
        Some(value) => value
            .as_u64()
            .filter(|days| *days == 0 || *days <= st.cfg.max_token_lifetime_days as u64)
            .ok_or_else(|| {
                err(
                    StatusCode::BAD_REQUEST,
                    "invalid-request",
                    format!("expires_days must be 0 or no more than {}", st.cfg.max_token_lifetime_days),
                )
            })?,
    };
    let allowed_cidrs = match m.get("allowed_cidrs") {
        None => Vec::new(),
        Some(Value::Array(values)) if values.len() <= 32 => {
            let mut cidrs = Vec::with_capacity(values.len());
            for value in values {
                let cidr = value
                    .as_str()
                    .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "allowed_cidrs must contain CIDR strings"))?;
                if cidr.len() > 64 || !crate::client_ip::valid_cidr(cidr) || cidrs.iter().any(|existing| existing == cidr) {
                    return Err(err(
                        StatusCode::BAD_REQUEST,
                        "invalid-request",
                        "allowed_cidrs must contain at most 32 unique valid CIDRs",
                    ));
                }
                cidrs.push(cidr.to_string());
            }
            cidrs
        }
        Some(_) => {
            return Err(err(
                StatusCode::BAD_REQUEST,
                "invalid-request",
                "allowed_cidrs must be an array of at most 32 CIDRs",
            ))
        }
    };
    let allowed_workers = match m.get("allowed_workers") {
        None => Vec::new(),
        Some(Value::Array(values)) if values.len() <= 32 => {
            let existing: HashSet<String> = st.db.workers_list()?.into_iter().map(|worker| worker.name).collect();
            let mut names = Vec::with_capacity(values.len());
            for value in values {
                let worker = value
                    .as_str()
                    .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "allowed_workers must contain worker names"))?;
                if !valid_name(worker) || names.iter().any(|existing| existing == worker) || !existing.contains(worker) {
                    return Err(err(
                        StatusCode::BAD_REQUEST,
                        "invalid-request",
                        "allowed_workers must contain at most 32 unique names of enrolled workers",
                    ));
                }
                names.push(worker.to_string());
            }
            names
        }
        Some(_) => {
            return Err(err(
                StatusCode::BAD_REQUEST,
                "invalid-request",
                "allowed_workers must be an array of at most 32 worker names",
            ))
        }
    };
    validate_token_expiry(days, st.cfg.exposure).map_err(|message| err(StatusCode::BAD_REQUEST, "non-expiring-token-forbidden", message))?;
    let expires_at = (days > 0).then(|| crate::now() + (days as i64) * 86_400);
    let token_id = auth::sha256_hex(&auth::random_token())[..16].to_string();
    let token_secret = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(auth::random_token())
        .map(hex::encode)
        .expect("URL-safe token generator output decodes");
    let secret = format!("lbr_pat_{token_id}_{token_secret}");
    if !st.db.token_create_identified_with_restrictions(crate::db::ApiTokenCreate {
        name,
        token_id: &token_id,
        secret_hash: &auth::sha256_hex(&token_secret),
        principal_id: p.principal_id(),
        expires_at,
        scopes: &scopes,
        allowed_cidrs: &allowed_cidrs,
        allowed_workers: &allowed_workers,
    })? {
        return Err(err(StatusCode::CONFLICT, "token-exists", "a token with this name already exists"));
    }
    let detail = format!(
        "name={name} scopes={} expires_at={}",
        scopes.join(","),
        expires_at.map_or_else(|| "never".into(), |e| e.to_string())
    );
    st.db.audit(&p.actor(), "token.create", &detail, &ip);
    let principal = st.db.admin_get()?.map(|(name, _)| name).unwrap_or_else(|| p.actor());
    security_notification(&st, &principal, "api_token_created");
    Ok(json_ok(
        StatusCode::CREATED,
        json!({
            "token": {"name": name, "id": token_id, "scopes": scopes, "created_at": timestamp(Some(crate::now())), "last_used": Value::Null, "last_address": Value::Null, "expires_at": timestamp(expires_at), "allowed_cidrs": allowed_cidrs, "allowed_workers": allowed_workers},
            "secret": secret
        }),
    ))
}

fn security_notification(st: &St, principal_id: &str, category: &str) {
    let id = match st.db.security_notification_add(principal_id, category) {
        Ok(Some(id)) => id,
        Ok(None) => return,
        Err(_) => {
            eprintln!("security notification could not be persisted");
            return;
        }
    };
    let Some(endpoint) = st.cfg.security_webhook_url.clone() else { return };
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(client) => client,
        Err(_) => {
            eprintln!("security notification webhook client could not be created");
            return;
        }
    };
    let event = json!({"id":id,"event":category,"occurred_at":timestamp(Some(crate::now()))});
    tokio::spawn(async move {
        match client.post(endpoint).json(&event).send().await {
            Ok(response) if response.status().is_success() => {}
            Ok(response) => eprintln!("security notification webhook returned HTTP {}", response.status().as_u16()),
            Err(_) => eprintln!("security notification webhook delivery failed"),
        }
    });
}

/// User-Agent is only a coarse device hint. Bound it, reject control bytes, and let the database
/// retain a salted hash only. Missing or malformed values simply disable this notification signal.
fn remember_login_device(st: &St, principal_id: &str, headers: &HeaderMap) -> Result<(), ApiError> {
    let Some(value) = headers.get(header::USER_AGENT).and_then(|value| value.to_str().ok()) else {
        return Ok(());
    };
    if value.is_empty() || value.len() > 512 || value.bytes().any(|byte| byte.is_ascii_control()) {
        return Ok(());
    }
    if st.db.known_login_device_remember(principal_id, value)? {
        security_notification(st, principal_id, "new_sign_in_device");
    }
    Ok(())
}

fn validate_token_expiry(days: u64, exposure: crate::config::Exposure) -> Result<(), &'static str> {
    if days == 0 && exposure == crate::config::Exposure::Internet {
        Err("Internet exposure requires every API token to expire")
    } else {
        Ok(())
    }
}

async fn list_security_notifications(State(st): State<St>, Authed(p, _): Authed) -> ApiResult {
    let principal = st.db.admin_get()?.map(|(name, _)| name).unwrap_or_else(|| p.actor());
    Ok(json_ok(
        StatusCode::OK,
        json!({"notifications": st.db.security_notifications_list(&principal)?}),
    ))
}

async fn dismiss_security_notification(State(st): State<St>, Authed(p, ip): Authed, Path(id): Path<String>) -> ApiResult {
    let principal = st.db.admin_get()?.map(|(name, _)| name).unwrap_or_else(|| p.actor());
    if !st.db.security_notification_dismiss(&principal, &id)? {
        return Err(err(StatusCode::NOT_FOUND, "unknown-notification", "no such active security notification"));
    }
    st.db.audit(&p.actor(), "security.notification.dismiss", &clean(&id), &ip);
    Ok(json_ok(StatusCode::OK, json!({"ok":true})))
}

async fn revoke_token(State(st): State<St>, Authed(p, ip): Authed, Path(name): Path<String>) -> ApiResult {
    if !st.db.token_revoke(&name)? {
        return Err(err(StatusCode::NOT_FOUND, "unknown-token", "no such token"));
    }
    st.db.audit(&p.actor(), "token.revoke", &clean(&name), &ip);
    Ok(json_ok(StatusCode::OK, json!({"ok": true})))
}

fn worker_json(st: &AppState, w: &crate::db::WorkerRow) -> Value {
    // A connected worker has been heard from within the last ping interval; the stored value is
    // persisted only every few minutes (ws_worker.rs LAST_SEEN_EVERY_SECS) to spare storage.
    let online = st.hub.online(&w.name);
    let last_seen = if online { Some(crate::now()) } else { w.last_seen };
    let policy = w.worker_policy.as_ref().and_then(Value::as_object).map(|value| {
        json!({
            "allow_control": value.get("allow_control").and_then(Value::as_bool).unwrap_or(false),
            "allow_terminal_view": value.get("allow_terminal_view").and_then(Value::as_bool).unwrap_or(false),
            "allow_create": value.get("allow_create").and_then(Value::as_bool).unwrap_or(false),
            "allow_root_count": value.get("allow_root").and_then(Value::as_array).map_or(0, Vec::len),
            "allow_power": value.get("allow_power").and_then(Value::as_bool).unwrap_or(false),
            "allow_restore": value.get("allow_restore").and_then(Value::as_bool).unwrap_or(false),
        })
    });
    json!({"name": w.name, "id": w.worker_id, "state": if online { "online" } else { "offline" },
           "host_name": w.host_name, "remote_address": if online { w.remote_address.clone() } else { None },
           "last_seen": last_seen, "generation": w.generation, "event_cursor": w.event_cursor,
           "versions": {"software": w.software_version, "protocol": w.protocol_versions},
           "mutual_tls": w.client_cert_fingerprint.is_some(),
           "platform": w.platform, "capabilities": w.capabilities, "policy": policy})
}

async fn list_workers(State(st): State<St>, Authed(principal, _): Authed) -> ApiResult {
    let workers: Vec<Value> = st
        .db
        .workers_list()?
        .iter()
        .filter(|worker| principal.allows_worker(&worker.name))
        .map(|w| worker_json(&st, w))
        .collect();
    Ok(json_ok(StatusCode::OK, json!({"workers": workers})))
}

async fn get_worker(State(st): State<St>, Path(name): Path<String>, _a: Authed) -> ApiResult {
    let Some(worker) = st.db.worker_get(&name)? else {
        return Err(err(StatusCode::NOT_FOUND, "not-found", "worker was not found"));
    };
    Ok(json_ok(StatusCode::OK, worker_json(&st, &worker)))
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
    let identity = if st.cfg.worker_mtls {
        Some(crate::certs::issue_worker_client_identity(&st.data_dir, name).map_err(|_| {
            err(
                StatusCode::INTERNAL_SERVER_ERROR,
                "worker-certificate-error",
                "failed to issue worker client certificate",
            )
        })?)
    } else {
        None
    };
    let added = if let Some(identity) = &identity {
        st.db
            .worker_add_with_client_certificate(name, &auth::sha256_hex(&token), &identity.fingerprint_sha256)?
    } else {
        st.db.worker_add(name, &auth::sha256_hex(&token))?
    };
    if !added {
        return Err(err(StatusCode::CONFLICT, "worker-exists", "a worker with this name exists"));
    }
    st.db.audit(&p.actor(), "worker.add", name, &ip);
    let mut response = json!({"name": name, "token": token, "mutual_tls": identity.is_some()});
    if let Some(identity) = identity {
        response["client_certificate"] = json!(identity.cert_pem);
        response["client_private_key"] = json!(identity.key_pem);
    }
    Ok(json_ok(StatusCode::CREATED, response))
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
/// `limit` (default 100, maximum 500) and an authenticated, encrypted opaque `cursor`. Legacy `offset` remains
/// accepted. The reply carries `total`, `next_cursor` and status facets.
async fn list_missions(State(st): State<St>, Authed(principal, _): Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    let bad = |m: &str| err(StatusCode::BAD_REQUEST, "invalid-request", m.to_string());
    if q.get("worker").is_some_and(|worker| !valid_name(worker)) {
        return Err(bad("worker must be a valid worker name"));
    }
    if q.get("worker").is_some_and(|worker| !principal.allows_worker(worker)) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "token-worker-denied",
            "request is outside this token's allowed workers",
        ));
    }
    if let Some(key) = q
        .keys()
        .find(|key| !matches!(key.as_str(), "q" | "worker" | "status" | "sort" | "order" | "limit" | "offset" | "cursor"))
    {
        return Err(bad(&format!("unknown query parameter {key}")));
    }
    let num = |k: &str| -> Result<Option<usize>, ApiError> {
        q.get(k)
            .map(|v| v.parse::<usize>().map_err(|_| bad(&format!("{k} must be a non-negative integer"))))
            .transpose()
    };
    for key in ["q", "status"] {
        if q.get(key).is_some_and(|value| value.chars().count() > 256) {
            return Err(bad(&format!("{key} must be at most 256 characters")));
        }
    }
    let limit = num("limit")?.unwrap_or(100);
    if !(1..=500).contains(&limit) {
        return Err(bad("limit must be between 1 and 500"));
    }
    let legacy_offset = num("offset")?;
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
    let mut statuses: Vec<String> = q
        .get("status")
        .map(|v| v.split(',').filter(|s| !s.is_empty()).map(str::to_uppercase).collect())
        .unwrap_or_default();
    statuses.sort();
    statuses.dedup();
    let filters = serde_json::to_string(&json!({
        "q": needle,
        "worker": q.get("worker"),
        "allowed_workers": principal.worker_restrictions(),
        "status": statuses,
        "sort": sort.unwrap_or("worker"),
        "descending": descending,
    }))
    .expect("mission list filters serialize");
    let offset = match (q.get("cursor"), legacy_offset) {
        // Cursor pagination wins when a client also sends the deprecated
        // offset parameter. This keeps generated clients and scanners from
        // turning two independently optional query parameters into a 400.
        (Some(cursor), _) => crate::cursor::read(&st.cursor_key, "missions", &filters, cursor)
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-cursor", "cursor is invalid or belongs to a different query"))?,
        (None, Some(offset)) => offset,
        (None, None) => 0,
    };
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
            principal.allows_worker(worker)
                && q.get("worker").is_none_or(|w| w == worker)
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
    let missions: Vec<Value> = rows
        .into_iter()
        .skip(offset)
        .take(limit)
        .map(|(worker, stale, info)| json!({"worker": worker, "stale": stale, "mission": info}))
        .collect();
    let next_cursor = (offset.saturating_add(missions.len()) < total)
        .then(|| crate::cursor::issue(&st.cursor_key, "missions", &filters, offset.saturating_add(missions.len())));
    Ok(json_ok(
        StatusCode::OK,
        json!({"missions": missions, "total": total, "offset": offset, "next_cursor": next_cursor, "statuses": by_status}),
    ))
}

struct MissionReadRequest {
    headers: HeaderMap,
    mission_id: String,
    worker: Option<String>,
    resource: &'static str,
    query: HashMap<String, String>,
}

/// Read a worker-owned mission resource through the same durable typed-command path as the
/// low-level endpoint. The route adapter selects a fixed read operation; callers cannot choose
/// an arbitrary command or pass fields outside that operation's parameter set.
async fn mission_read(st: St, principal: Principal, ip: String, request: MissionReadRequest) -> ApiResult {
    let MissionReadRequest {
        headers,
        mission_id,
        worker,
        resource,
        query,
    } = request;
    let worker = worker.ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    let (operation, allowed, position_key, default_limit): (&str, &[&str], Option<&str>, u64) = match resource {
        "mission" => ("mission.status", &[], None, 0),
        "tasks" => ("task.list", &["limit", "cursor", "offset"], Some("offset"), 50),
        "events" => ("mission.events", &["limit", "cursor", "after"], Some("after"), 50),
        "operations" => ("session.list", &["limit", "cursor", "offset"], Some("offset"), 100),
        _ => return Err(err(StatusCode::NOT_FOUND, "not-found", "unknown mission resource")),
    };
    if let Some(key) = query
        .keys()
        .find(|key| !matches!(key.as_str(), "worker" | "wait_ms") && !allowed.contains(&key.as_str()))
    {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", format!("unknown query parameter {key}")));
    }
    let limit = query.get("limit").map_or(Ok(default_limit), |value| {
        value
            .parse::<u64>()
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "limit must be a positive integer"))
    })?;
    if position_key.is_some() && !(1..=1000).contains(&limit) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "limit must be between 1 and 1000"));
    }
    let legacy_key = match resource {
        "tasks" | "operations" => Some("offset"),
        "events" => Some("after"),
        _ => None,
    };
    let filters = format!("worker={worker};mission={mission_id};resource={resource};limit={limit}");
    let wait_ms = query.get("wait_ms").map_or(Ok(0), |value| {
        value
            .parse::<u64>()
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer"))
    })?;
    if wait_ms > MAX_WAIT_MS {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be at most 30000"));
    }
    let mut params = serde_json::Map::new();
    for key in allowed {
        if *key == "cursor" || *key == "limit" {
            continue;
        }
        if query.contains_key("cursor") && Some(*key) == legacy_key {
            continue;
        }
        if let Some(value) = query.get(*key) {
            let number = value
                .parse::<u64>()
                .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", format!("{key} must be a non-negative integer")))?;
            if (*key == "limit" && !(1..=1000).contains(&number)) || number > i64::MAX as u64 {
                return Err(err(StatusCode::BAD_REQUEST, "invalid-request", format!("{key} is outside its allowed range")));
            }
            params.insert((*key).to_string(), json!(number));
        }
    }
    if let Some(position_key) = position_key {
        let position = match query.get("cursor") {
            Some(cursor) => crate::cursor::read(&st.cursor_key, &format!("missions/{resource}"), &filters, cursor)
                .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-cursor", "cursor is invalid or belongs to a different list"))?,
            None => legacy_key
                .and_then(|key| query.get(key))
                .map(|value| {
                    value
                        .parse::<usize>()
                        .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "pagination position must be a non-negative integer"))
                })
                .transpose()?
                .unwrap_or(0),
        };
        params.insert(position_key.to_string(), json!(position));
    }
    if position_key.is_some() {
        params.insert("limit".into(), json!(limit));
    }
    if resource == "operations" {
        params.insert("mission".into(), json!(mission_id));
    }
    let command = json!({
        "target": worker,
        "wait_ms": wait_ms,
        "request": {
            "schema_version": protocol::COMMAND_SCHEMA_VERSION,
            "operation": operation,
            "params": params,
            "mission_id": mission_id,
            "target": worker,
            "request_id": format!("resource-{}", auth::random_token())
        }
    });
    let bytes = Bytes::from(serde_json::to_vec(&command).expect("command JSON serializes"));
    post_command(State(st), Authed(principal, ip), headers, Buffered { bytes, _permit: None }).await
}

async fn mission_tasks(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    mission_read(
        st,
        p,
        ip,
        MissionReadRequest {
            headers,
            mission_id: id,
            worker: q.get("worker").cloned(),
            resource: "tasks",
            query: q,
        },
    )
    .await
}

async fn mission_status_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    mission_read(
        st,
        p,
        ip,
        MissionReadRequest {
            headers,
            mission_id: id,
            worker: q.get("worker").cloned(),
            resource: "mission",
            query: q,
        },
    )
    .await
}

async fn create_mission_resource(State(st): State<St>, Authed(p, ip): Authed, headers: HeaderMap, body: Buffered) -> ApiResult {
    let mut params = parse_body(&body.bytes)?;
    let worker = str_field(&params, "worker")?.to_string();
    if !p.allows_worker(&worker) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "token-worker-denied",
            "request is outside this token's allowed workers",
        ));
    }
    let local_only = [
        "auto_continue",
        "command_template",
        "verifier_arg",
        "verifier_argv",
        "verifier_file",
        "verifier_files",
        "verifier_timeout",
        "include_config",
        "log_max_bytes",
        "log_rotations",
        "log_retained_runs",
        "capacity_refresh_seconds",
    ];
    let unsupported: Vec<&str> = local_only.iter().copied().filter(|name| params.contains_key(*name)).collect();
    if !unsupported.is_empty() {
        return Err(err(
            StatusCode::UNPROCESSABLE_ENTITY,
            "unsupported-remote-option",
            format!("mission.create does not support remote option(s): {}", unsupported.join(", ")),
        ));
    }
    no_extra(
        &params,
        &[
            "worker",
            "prompt",
            "cwd",
            "name",
            "acceptance",
            "providers",
            "routes",
            "headless",
            "native_tui",
            "teams",
            "token_spend",
            "trust_folder",
            "unsupported_options",
            "wait_ms",
        ],
    )?;
    let wait_ms = resource_wait_ms(&params)?;
    params.remove("worker");
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: None,
            operation: "mission.create".into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn worker_restore_resource(State(st): State<St>, Authed(p, ip): Authed, Path(worker): Path<String>, headers: HeaderMap, body: Buffered) -> ApiResult {
    let mut params = parse_body(&body.bytes)?;
    no_extra(&params, &["input", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&params)?;
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: None,
            operation: "worker.restore".into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn mission_settings_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    method: Method,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|key| !matches!(key.as_str(), "worker" | "wait_ms")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut params = if method == Method::GET { Map::new() } else { parse_body(&body.bytes)? };
    if method == Method::GET {
        no_extra(&params, &[])?;
    } else {
        no_extra(
            &params,
            &["auto_continue", "teams", "token_spend", "suspend_when_rate_limited", "colour", "wait_ms"],
        )?;
    }
    let wait_ms = q.get("wait_ms").and_then(|v| v.parse::<u64>().ok().filter(|n| *n <= MAX_WAIT_MS)).unwrap_or(0);
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: if method == Method::GET {
                "mission.settings.get"
            } else {
                "mission.settings.patch"
            }
            .into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn mission_rotation_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    method: Method,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|key| !matches!(key.as_str(), "worker" | "wait_ms")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut params = if method == Method::GET { Map::new() } else { parse_body(&body.bytes)? };
    if method == Method::GET {
        no_extra(&params, &[])?;
    } else {
        no_extra(&params, &["set", "routes", "providers", "wait_ms"])?;
    }
    let wait_ms = q.get("wait_ms").and_then(|v| v.parse::<u64>().ok()).filter(|n| *n <= MAX_WAIT_MS).unwrap_or(0);
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: if method == Method::GET {
                "mission.rotation.get"
            } else {
                "mission.rotation.put"
            }
            .into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn mission_proposals_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    method: Method,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    let mut params = if method == Method::GET {
        let mut values = Map::new();
        for key in ["status", "limit", "after"] {
            if let Some(value) = q.get(key) {
                values.insert(
                    key.into(),
                    json!(if key == "status" {
                        Value::String(value.clone())
                    } else {
                        Value::Number(value.parse::<u64>().unwrap_or(0).into())
                    }),
                );
            }
        }
        values
    } else {
        parse_body(&body.bytes)?
    };
    if method == Method::GET {
        if q.keys().any(|k| !matches!(k.as_str(), "worker" | "wait_ms" | "status" | "limit" | "after")) {
            return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
        }
    } else {
        no_extra(
            &params,
            &["source_operation_id", "prompt", "title", "priority", "depends_on", "acceptance", "wait_ms"],
        )?;
    }
    let wait_ms = if method == Method::GET {
        q.get("wait_ms").and_then(|v| v.parse::<u64>().ok()).unwrap_or(0)
    } else {
        resource_wait_ms(&params)?
    };
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: if method == Method::GET { "proposal.list" } else { "proposal.create" }.into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn mission_agents_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|key| !matches!(key.as_str(), "worker" | "wait_ms")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let wait_ms = q.get("wait_ms").map_or(Ok(0), |v| {
        v.parse::<u64>()
            .ok()
            .filter(|n| *n <= MAX_WAIT_MS)
            .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer of at most 30000"))
    })?;
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: "mission.agents".into(),
            params: Map::new(),
            wait_ms,
        },
    )
    .await
}

async fn mission_steer_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut params = parse_body(&body.bytes)?;
    no_extra(&params, &["kind", "task", "task_id", "route", "prompt", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&params)?;
    params.remove("wait_ms");
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: "mission.steer".into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn mission_finish_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut params = parse_body(&body.bytes)?;
    no_extra(&params, &["wait_ms"])?;
    let wait_ms = resource_wait_ms(&params)?;
    params.clear();
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: "mission.finish".into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn proposal_decision_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, number, action)): Path<(String, u32, String)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?
        .clone();
    if q.keys().any(|k| !matches!(k.as_str(), "worker" | "wait_ms")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut params = parse_body(&body.bytes)?;
    no_extra(&params, &["reason", "wait_ms"])?;
    params.insert("task_number".into(), json!(number));
    let wait_ms = resource_wait_ms(&params)?;
    params.remove("wait_ms");
    let operation = match action.as_str() {
        "accept" => "proposal.accept",
        "reject" => "proposal.reject",
        _ => return Err(err(StatusCode::NOT_FOUND, "not-found", "unknown proposal action")),
    };
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: Some(id),
            operation: operation.into(),
            params,
            wait_ms,
        },
    )
    .await
}

struct WorkerResourceRequest {
    worker: String,
    action: String,
    q: HashMap<String, String>,
    headers: HeaderMap,
    body: Buffered,
}

async fn worker_resource(st: St, p: Principal, ip: String, request: WorkerResourceRequest) -> ApiResult {
    let WorkerResourceRequest {
        worker,
        action,
        q,
        headers,
        body,
    } = request;
    let mut params = Map::new();
    let mut body_wait_ms = 0;
    let operation = match action.as_str() {
        "doctor" => {
            if let Some(mission) = q.get("mission") {
                params.insert("mission".into(), json!(mission));
            }
            "worker.doctor"
        }
        "audit" => {
            if let Some(limit) = q.get("limit") {
                params.insert("limit".into(), json!(limit.parse::<u64>().unwrap_or(50)));
            }
            "worker.audit"
        }
        "agents" => {
            if let Some(mission) = q.get("mission") {
                params.insert("mission".into(), json!(mission));
            }
            "mission.agents"
        }
        "gc" => {
            params = parse_body(&body.bytes)?;
            no_extra(&params, &["wait_ms"])?;
            body_wait_ms = resource_wait_ms(&params)?;
            params.remove("wait_ms");
            "worker.gc"
        }
        _ => return Err(err(StatusCode::NOT_FOUND, "not-found", "unknown worker resource")),
    };
    let wait_ms = q.get("wait_ms").map_or(Ok(body_wait_ms), |v| {
        v.parse::<u64>()
            .ok()
            .filter(|n| *n <= MAX_WAIT_MS)
            .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer of at most 30000"))
    })?;
    let allowed_query = if action == "audit" {
        &["limit", "wait_ms"][..]
    } else if matches!(action.as_str(), "doctor" | "agents") {
        &["mission", "wait_ms"][..]
    } else {
        &[][..]
    };
    if q.keys().any(|k| !allowed_query.contains(&k.as_str())) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker,
            mission_id: None,
            operation: operation.into(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn worker_doctor_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(worker): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    worker_resource(
        st,
        p,
        ip,
        WorkerResourceRequest {
            worker,
            action: "doctor".into(),
            q,
            headers,
            body: Buffered {
                bytes: Bytes::new(),
                _permit: None,
            },
        },
    )
    .await
}
async fn worker_agents_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(worker): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    worker_resource(
        st,
        p,
        ip,
        WorkerResourceRequest {
            worker,
            action: "agents".into(),
            q,
            headers,
            body: Buffered {
                bytes: Bytes::new(),
                _permit: None,
            },
        },
    )
    .await
}
async fn worker_audit_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(worker): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    worker_resource(
        st,
        p,
        ip,
        WorkerResourceRequest {
            worker,
            action: "audit".into(),
            q,
            headers,
            body: Buffered {
                bytes: Bytes::new(),
                _permit: None,
            },
        },
    )
    .await
}
async fn worker_gc_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(worker): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    worker_resource(
        st,
        p,
        ip,
        WorkerResourceRequest {
            worker,
            action: "gc".into(),
            q,
            headers,
            body,
        },
    )
    .await
}

async fn mission_events(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    mission_read(
        st,
        p,
        ip,
        MissionReadRequest {
            headers,
            mission_id: id,
            worker: q.get("worker").cloned(),
            resource: "events",
            query: q,
        },
    )
    .await
}

async fn mission_operations(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    mission_read(
        st,
        p,
        ip,
        MissionReadRequest {
            headers,
            mission_id: id,
            worker: q.get("worker").cloned(),
            resource: "operations",
            query: q,
        },
    )
    .await
}

async fn mission_task_show(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, task_number)): Path<(String, u64)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| !matches!(key.as_str(), "worker" | "wait_ms")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let wait_ms = q.get("wait_ms").map_or(Ok(0), |value| {
        value
            .parse::<u64>()
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer"))
    })?;
    if wait_ms > MAX_WAIT_MS {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be at most 30000"));
    }
    let mut params = Map::new();
    params.insert("task_number".into(), json!(task_number));
    let response = resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: "task.show".into(),
            params,
            wait_ms,
        },
    )
    .await?;
    if response.status() != StatusCode::OK {
        return Ok(response);
    }
    let (mut parts, body) = response.into_parts();
    let bytes = axum::body::to_bytes(body, MAX_BODY)
        .await
        .map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "internal error"))?;
    if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
        if let Some(revision) = value.pointer("/result/result/revision").and_then(Value::as_u64) {
            parts
                .headers
                .insert(header::ETAG, format!("\"{revision}\"").parse().expect("numeric entity tag"));
        }
    }
    Ok(Response::from_parts(parts, axum::body::Body::from(bytes)))
}

async fn list_events(State(st): State<St>, _a: Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    if q.keys().any(|key| !matches!(key.as_str(), "worker" | "after" | "limit")) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
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

async fn post_command(State(st): State<St>, Authed(p, ip): Authed, headers: HeaderMap, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["target", "request", "wait_ms"])?;
    let target = str_field(&m, "target")?;
    let Some(Value::Object(request)) = m.get("request") else {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "request must be a JSON object"));
    };
    let mut request = request.clone();
    if let Some(key) = headers.get("idempotency-key") {
        let key = key
            .to_str()
            .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "Idempotency-Key must be ASCII"))?;
        if !valid_id(key) {
            return Err(err(
                StatusCode::BAD_REQUEST,
                "invalid-request",
                "Idempotency-Key must be 1-128 characters of letters, digits, '.', '_' or '-'",
            ));
        }
        request.insert("request_id".into(), json!(key));
    }
    let wait_ms = match m.get("wait_ms") {
        None | Some(Value::Null) => 0,
        Some(v) => v
            .as_u64()
            .filter(|w| *w <= MAX_WAIT_MS)
            .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer of at most 30000"))?,
    };
    protocol::check_command_request(&request).map_err(|e| err(StatusCode::BAD_REQUEST, e.code, e.message))?;
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
        InsertOutcome::RequestTooLarge => {
            return Err(err(
                StatusCode::PAYLOAD_TOO_LARGE,
                "request-too-large",
                "command request exceeds the 64 KiB limit",
            ))
        }
        InsertOutcome::Existing(row) => row,
        InsertOutcome::New(row) => {
            if mutating {
                let detail = format!(
                    "worker={target} operation={operation} request_id={request_id} mission={}",
                    clean(request.get("mission_id").and_then(Value::as_str).unwrap_or(""))
                );
                st.db.audit(&p.actor(), "command.submit", &detail, &ip);
            }
            st.hub.publish(json!({"type": "command", "command": row.notification_json()}));
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
    if row.state == "done"
        && request
            .get("params")
            .and_then(Value::as_object)
            .is_some_and(|params| params.contains_key("expected_revision"))
        && row.result.as_ref().and_then(|result| result.pointer("/error/code")).and_then(Value::as_str) == Some("conflict")
    {
        return Err(err(
            StatusCode::PRECONDITION_FAILED,
            "stale-revision",
            "the task changed since the supplied revision; fetch it again and retry",
        ));
    }
    if row.state == "done" && row.result.as_ref().and_then(|result| result.get("outcome")).and_then(Value::as_str) == Some("uncertain") {
        let mut response = err(
            StatusCode::CONFLICT,
            "uncertain-outcome",
            "the worker could not determine whether this operation completed; inspect the command before retrying",
        )
        .into_response();
        response.headers_mut().insert(
            header::LOCATION,
            format!("/api/v1/commands/{request_id}").parse().expect("validated request id"),
        );
        return Ok(response);
    }
    let status = if row.state == "done" { StatusCode::OK } else { StatusCode::ACCEPTED };
    let value = command_response_json(&st, &row);
    let mut response = json_ok(status, value);
    if status == StatusCode::ACCEPTED {
        response.headers_mut().insert(
            header::LOCATION,
            format!("/api/v1/commands/{request_id}").parse().expect("validated request id"),
        );
    }
    Ok(response)
}

fn add_next_page_cursor(st: &St, row: &CommandRow, response: &mut Value) {
    let Some(request) = row.request.as_object() else { return };
    let Some(params) = request.get("params").and_then(Value::as_object) else {
        return;
    };
    let Some(result) = response.pointer_mut("/result/result").and_then(Value::as_object_mut) else {
        return;
    };
    let (resource, limit_default, next_position) = match row.operation.as_str() {
        "task.list" => {
            let offset = result.get("offset").and_then(Value::as_u64).unwrap_or(0);
            let len = result.get("tasks").and_then(Value::as_array).map_or(0, Vec::len) as u64;
            let total = result.get("total").and_then(Value::as_u64).unwrap_or(offset);
            ("tasks", 50, (offset.saturating_add(len) < total).then_some(offset.saturating_add(len)))
        }
        "mission.events" => {
            let events = result.get("events").and_then(Value::as_array);
            let limit = params.get("limit").and_then(Value::as_u64).unwrap_or(50);
            let last = events.and_then(|rows| rows.last()).and_then(|event| event.get("id")).and_then(Value::as_u64);
            ("events", 50, events.filter(|rows| rows.len() as u64 == limit).and(last))
        }
        "session.list" => {
            let offset = result.get("offset").and_then(Value::as_u64).unwrap_or(0);
            let len = result.get("sessions").and_then(Value::as_array).map_or(0, Vec::len) as u64;
            let total = result.get("total").and_then(Value::as_u64).unwrap_or(offset);
            ("operations", 100, (offset.saturating_add(len) < total).then_some(offset.saturating_add(len)))
        }
        _ => return,
    };
    let limit = params.get("limit").and_then(Value::as_u64).unwrap_or(limit_default);
    let worker = row.worker.as_str();
    let mission = request
        .get("mission_id")
        .and_then(Value::as_str)
        .or_else(|| params.get("mission").and_then(Value::as_str))
        .unwrap_or("");
    let filters = format!("worker={worker};mission={mission};resource={resource};limit={limit}");
    let next_cursor = next_position
        .and_then(|next| usize::try_from(next).ok())
        .map(|next| crate::cursor::issue(&st.cursor_key, &format!("missions/{resource}"), &filters, next));
    result.insert("next_cursor".into(), json!(next_cursor));
}

fn command_response_json(st: &St, row: &CommandRow) -> Value {
    let mut value = row.to_json();
    redact_private_api_fields(&mut value);
    value["created_at"] = json!(timestamp(Some(row.created_at)).expect("command timestamps are present"));
    value["updated_at"] = json!(timestamp(Some(row.updated_at)).expect("command timestamps are present"));
    add_next_page_cursor(st, row, &mut value);
    value
}

/// Command requests and worker results are durable internal records, not public API schemas.
/// Remove known private fields recursively before exposing them through the generic command
/// resource. Resource-specific endpoints remain the preferred, explicitly projected interface.
fn redact_private_api_fields(value: &mut Value) {
    const PRIVATE_FIELDS: &[&str] = &[
        "cwd",
        "repo_identity",
        "objective",
        "config",
        "environment",
        "env",
        "terminal",
        "terminal_content",
        "terminal_screen",
        "terminal_output",
        "screen",
        "transcript",
        "output",
        "raw_output",
        "stdout",
        "stderr",
        "secret",
        "api_key",
        "provider_api_key",
        "provider_key",
        "provider_credentials",
        "token",
        "access_token",
        "refresh_token",
        "token_hash",
        "password",
        "credential",
        "credentials",
        "authorization",
        "cookie",
    ];
    match value {
        Value::Object(fields) => {
            fields.retain(|key, _| !PRIVATE_FIELDS.iter().any(|private| key.eq_ignore_ascii_case(private)));
            for child in fields.values_mut() {
                redact_private_api_fields(child);
            }
        }
        Value::Array(values) => {
            for child in values {
                redact_private_api_fields(child);
            }
        }
        _ => {}
    }
}

async fn get_command(State(st): State<St>, _a: Authed, Path(id): Path<String>) -> ApiResult {
    match st.db.command_get(&id)? {
        Some(row) => Ok(json_ok(StatusCode::OK, command_response_json(&st, &row))),
        None => Err(err(StatusCode::NOT_FOUND, "unknown-command", "no such request_id")),
    }
}

struct ResourceCommand {
    headers: HeaderMap,
    worker: String,
    mission_id: Option<String>,
    operation: String,
    params: Map<String, Value>,
    wait_ms: u64,
}

async fn resource_command(st: St, principal: Principal, ip: String, command: ResourceCommand) -> ApiResult {
    let headers = command.headers;
    let envelope = json!({
        "target": command.worker,
        "wait_ms": command.wait_ms,
        "request": {
            "schema_version": protocol::COMMAND_SCHEMA_VERSION,
            "operation": command.operation,
            "params": command.params,
            "mission_id": command.mission_id,
            "target": command.worker,
            "request_id": format!("resource-{}", auth::random_token())
        }
    });
    let bytes = Bytes::from(serde_json::to_vec(&envelope).expect("command JSON serializes"));
    post_command(State(st), Authed(principal, ip), headers, Buffered { bytes, _permit: None }).await
}

fn resource_wait_ms(m: &Map<String, Value>) -> Result<u64, ApiError> {
    match m.get("wait_ms") {
        None => Ok(0),
        Some(value) => value
            .as_u64()
            .filter(|wait| *wait <= MAX_WAIT_MS)
            .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "wait_ms must be an integer of at most 30000")),
    }
}

fn apply_if_match(headers: &HeaderMap, params: &mut Map<String, Value>) -> Result<(), ApiError> {
    let Some(value) = headers.get(header::IF_MATCH) else { return Ok(()) };
    let raw = value
        .to_str()
        .map_err(|_| err(StatusCode::BAD_REQUEST, "invalid-request", "If-Match must be an entity tag"))?;
    let revision = raw
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value <= i64::MAX as u64)
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "If-Match must be a quoted task revision"))?;
    if params.get("expected_revision").is_some_and(|value| value.as_u64() != Some(revision)) {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "If-Match and expected_revision disagree"));
    }
    params.insert("expected_revision".into(), json!(revision));
    Ok(())
}

async fn mission_action(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, action)): Path<(String, String)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let m = parse_body(&body.bytes)?;
    let allowed = if action == "start" {
        &["headless", "native_tui", "providers", "routes", "wait_ms"][..]
    } else if action == "stop" {
        &["force", "wait_ms"][..]
    } else {
        &["wait_ms"][..]
    };
    no_extra(&m, allowed)?;
    let mut params = m.clone();
    params.remove("wait_ms");
    let operation = match action.as_str() {
        "start" => "mission.start",
        "resume" => "mission.resume",
        "pause" => "mission.pause",
        "stop" => "mission.stop",
        "cancel" => "mission.cancel",
        "continue" => "mission.continue",
        "suspend-until-credits" => "mission.suspend_until_credits",
        _ => return Err(err(StatusCode::NOT_FOUND, "not-found", "unknown mission action")),
    };
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: operation.to_string(),
            params,
            wait_ms: resource_wait_ms(&m)?,
        },
    )
    .await
}

async fn suspend_until_credits_resource(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let params = parse_body(&body.bytes)?;
    no_extra(&params, &["wait_ms"])?;
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: "mission.suspend_until_credits".into(),
            params: Map::new(),
            wait_ms: resource_wait_ms(&params)?,
        },
    )
    .await
}

async fn add_mission_task(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path(id): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut m = parse_body(&body.bytes)?;
    no_extra(&m, &["title", "prompt", "priority", "order", "depends_on", "acceptance", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&m)?;
    m.remove("wait_ms");
    if let Some(numbers) = m.remove("depends_on") {
        m.insert("depends_on_numbers".into(), numbers);
    }
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: "task.add".to_string(),
            params: m,
            wait_ms,
        },
    )
    .await
}

async fn amend_mission_task(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, task_number)): Path<(String, u64)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut m = parse_body(&body.bytes)?;
    no_extra(&m, &["prompt", "priority", "order", "depends_on", "expected_revision", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&m)?;
    m.remove("wait_ms");
    apply_if_match(&headers, &mut m)?;
    let changes = ["prompt", "priority", "order", "depends_on"]
        .into_iter()
        .filter(|key| m.contains_key(*key))
        .collect::<Vec<_>>();
    if changes.len() != 1 {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "invalid-request",
            "PATCH must set exactly one of prompt, priority, order or depends_on",
        ));
    }
    let field = changes[0];
    let operation = match field {
        "prompt" => "task.amend",
        "priority" => "task.priority",
        "order" => "task.reorder",
        _ => "task.dependency",
    };
    let mut params = Map::new();
    params.insert("task_number".into(), json!(task_number));
    if field == "depends_on" {
        if let Some(number) = m.remove("depends_on") {
            m.insert("depends_on_number".into(), number);
        }
    }
    params.append(&mut m);
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: operation.to_string(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn verify_mission_task(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, task_number)): Path<(String, u64)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut m = parse_body(&body.bytes)?;
    no_extra(&m, &["approved", "evidence", "reason", "expected_revision", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&m)?;
    m.remove("wait_ms");
    apply_if_match(&headers, &mut m)?;
    let approved = match m.remove("approved") {
        Some(Value::Bool(approved)) => approved,
        _ => return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "approved must be true or false")),
    };
    let operation = if approved {
        m.insert("approved".into(), Value::Bool(true));
        "task.verify"
    } else {
        if !m.contains_key("reason") {
            return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "reason is required when rejecting a task"));
        }
        "task.reject"
    };
    if approved {
        m.remove("reason");
    } else {
        m.remove("evidence");
    }
    let mut params = Map::new();
    params.insert("task_number".into(), json!(task_number));
    params.append(&mut m);
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: operation.to_string(),
            params,
            wait_ms,
        },
    )
    .await
}

async fn task_action(
    State(st): State<St>,
    Authed(p, ip): Authed,
    Path((id, task_number, action)): Path<(String, u64, String)>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Buffered,
) -> ApiResult {
    let worker = q
        .get("worker")
        .ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid-request", "worker query parameter is required"))?;
    if q.keys().any(|key| key != "worker") {
        return Err(err(StatusCode::BAD_REQUEST, "invalid-request", "unknown query parameter"));
    }
    let mut m = parse_body(&body.bytes)?;
    no_extra(&m, &["expected_revision", "wait_ms"])?;
    let wait_ms = resource_wait_ms(&m)?;
    m.remove("wait_ms");
    apply_if_match(&headers, &mut m)?;
    let operation = match action.as_str() {
        "pause" => "task.pause",
        "resume" => "task.resume",
        "cancel" => "task.cancel",
        "retry" => "task.retry",
        _ => return Err(err(StatusCode::NOT_FOUND, "not-found", "unknown task action")),
    };
    m.insert("task_number".into(), json!(task_number));
    resource_command(
        st,
        p,
        ip,
        ResourceCommand {
            headers,
            worker: worker.clone(),
            mission_id: Some(id),
            operation: operation.to_string(),
            params: m,
            wait_ms,
        },
    )
    .await
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
    if uri.path() == "/api" || uri.path().starts_with("/api/") {
        return err(StatusCode::NOT_FOUND, "not-found", "no API route matches this path").into_response();
    }
    crate::web::serve(st.web_dir.clone(), method, uri, headers).await
}

async fn security_headers(mut r: Response) -> Response {
    if r.status() == StatusCode::METHOD_NOT_ALLOWED {
        let allow = r.headers().get(header::ALLOW).cloned();
        r = err(StatusCode::METHOD_NOT_ALLOWED, "method-not-allowed", "method is not allowed for this route").into_response();
        if let Some(allow) = allow {
            r.headers_mut().insert(header::ALLOW, allow);
        }
    }
    // Axum's built-in path extractors reject malformed numeric route segments
    // before handlers run and return plain text. Normalize those API failures
    // to the documented RFC 9457 shape, without reflecting parser details.
    if r.status() == StatusCode::BAD_REQUEST
        && r.headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value.starts_with("text/plain"))
    {
        r = err(StatusCode::BAD_REQUEST, "invalid-request", "a request path or parameter has an invalid format").into_response();
    }
    let h = r.headers_mut();
    // The static handler sets its own Content-Security-Policy and cache policy.
    for (k, v) in [
        ("cache-control", "no-store"),
        ("content-security-policy", "default-src 'none'; form-action 'none'; frame-ancestors 'none'"),
    ] {
        if !h.contains_key(k) {
            h.insert(k, v.parse().expect("static header"));
        }
    }
    for (k, v) in [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("strict-transport-security", "max-age=63072000"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cross-origin-resource-policy", "same-origin"),
        ("cross-origin-embedder-policy", "require-corp"),
        (
            "permissions-policy",
            "accelerometer=(), autoplay=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), usb=()",
        ),
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
    /// A browser session cookie only; bearer tokens cannot satisfy this route.
    Session,
    /// A worker token as bearer (worker WebSocket and worker terminal attach).
    WorkerToken,
}

/// Every route [`router`] serves with the methods it accepts and its access class.
/// `tests/routes.rs` checks this table against the `.route(...)` lines of `router` and then
/// requests each entry without credentials, so a route added to one but not the other fails.
pub const ROUTES: &[(&str, &[&str], Access, &str)] = &[
    ("/healthz", &["GET"], Access::Public, ""),
    ("/api/v1/auth/state", &["GET"], Access::Public, ""),
    ("/api/v1/auth/setup", &["POST"], Access::Public, ""),
    ("/api/v1/auth/login", &["POST"], Access::Public, ""),
    ("/api/v1/auth/totp/enroll", &["POST"], Access::Session, ""),
    ("/api/v1/auth/totp/confirm", &["POST"], Access::Session, ""),
    ("/api/v1/auth/totp/disable", &["POST"], Access::Session, ""),
    ("/api/v1/auth/passkeys/registration/start", &["POST"], Access::Session, ""),
    ("/api/v1/auth/passkeys/registration/finish", &["POST"], Access::Session, ""),
    ("/api/v1/auth/passkeys", &["GET"], Access::Session, ""),
    ("/api/v1/auth/sessions", &["GET"], Access::Session, ""),
    ("/api/v1/auth/sessions/{id}", &["DELETE"], Access::Session, ""),
    ("/api/v1/auth/passkeys/re-enroll", &["POST"], Access::Session, ""),
    ("/api/v1/auth/passkeys/assertion/start", &["POST"], Access::Public, ""),
    ("/api/v1/auth/passkeys/assertion/finish", &["POST"], Access::Public, ""),
    ("/api/v1/auth/logout", &["POST"], Access::Authed, "session"),
    ("/api/v1/auth/password", &["POST"], Access::Authed, "session"),
    ("/api/v1/workers", &["GET", "POST"], Access::Authed, "admin"),
    ("/api/v1/workers/{name}", &["GET", "DELETE"], Access::Authed, "admin"),
    ("/api/v1/workers/{name}/restore", &["POST"], Access::Authed, "admin"),
    ("/api/v1/workers/{name}/doctor", &["GET"], Access::Authed, "read"),
    ("/api/v1/workers/{name}/agents", &["GET"], Access::Authed, "read"),
    ("/api/v1/workers/{name}/audit", &["GET"], Access::Authed, "admin"),
    ("/api/v1/workers/{name}/gc", &["POST"], Access::Authed, "operate"),
    ("/api/v1/tokens", &["GET", "POST"], Access::Authed, "admin"),
    ("/api/v1/audit", &["GET"], Access::Authed, "admin"),
    ("/api/v1/tokens/{name}", &["DELETE"], Access::Authed, "admin"),
    ("/api/v1/security/notifications", &["GET"], Access::Authed, "admin"),
    ("/api/v1/security/notifications/{id}", &["DELETE"], Access::Authed, "admin"),
    ("/api/v1/missions", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/events", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/operations", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/agents", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/settings", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/settings", &["PATCH"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/rotation", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/rotation", &["PUT"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/proposals", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/proposals", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/proposals/{n}/{action}", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/suspend-until-credits", &["POST"], Access::Authed, "admin"),
    ("/api/v1/missions/{id}/steer", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/finish", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/{action}", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/tasks", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/tasks", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/tasks/{n}", &["GET"], Access::Authed, "read"),
    ("/api/v1/missions/{id}/tasks/{n}", &["PATCH"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/tasks/{n}/verify", &["POST"], Access::Authed, "operate"),
    ("/api/v1/missions/{id}/tasks/{n}/{action}", &["POST"], Access::Authed, "operate"),
    ("/api/v1/commands", &["POST"], Access::Authed, "operate"),
    ("/api/v1/commands/{id}", &["GET"], Access::Authed, "read"),
    ("/api/v1/events", &["GET"], Access::Authed, "read"),
    ("/api/v1/stream", &["GET"], Access::Authed, "read"),
    ("/api/v1/diagnostics", &["GET"], Access::Authed, "admin"),
    ("/api/v1/openapi.json", &["GET"], Access::Public, ""),
    ("/api/docs", &["GET"], Access::Public, ""),
    ("/api/v1/worker/connect", &["GET"], Access::WorkerToken, "worker"),
    ("/api/v1/terminal", &["GET"], Access::Authed, "terminal:conditional"),
    ("/api/v1/worker/terminal/{session_id}", &["GET"], Access::WorkerToken, "worker"),
];

#[cfg(test)]
mod problem_tests {
    use super::*;

    #[test]
    fn internet_mode_never_allows_password_only_sign_in() {
        assert!(password_login_blocked(crate::config::Exposure::Internet, false));
        assert!(!password_login_blocked(crate::config::Exposure::Internet, true));
        assert!(!password_login_blocked(crate::config::Exposure::Lan, false));
        assert!(!password_login_blocked(crate::config::Exposure::Loopback, false));
    }

    #[tokio::test]
    async fn internal_problem_is_generic_and_has_a_correlation_id() {
        let response = err(StatusCode::INTERNAL_SERVER_ERROR, "database", "/private/path: secret detail").into_response();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(response.headers()[header::CONTENT_TYPE], "application/problem+json");
        let id = response
            .headers()
            .get("x-correlation-id")
            .expect("correlation header")
            .to_str()
            .unwrap()
            .to_string();
        let body = axum::body::to_bytes(response.into_body(), 4096).await.unwrap();
        let problem: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(problem["type"], "https://llm-tools.dev/problems/internal");
        assert_eq!(problem["detail"], "internal server error");
        assert_eq!(problem["correlation_id"], id);
        assert!(!body.windows(b"/private/path".len()).any(|part| part == b"/private/path"));
    }

    #[test]
    fn internet_exposure_never_allows_minting_a_non_expiring_token() {
        assert!(validate_token_expiry(90, crate::config::Exposure::Internet).is_ok());
        assert!(validate_token_expiry(0, crate::config::Exposure::Lan).is_ok());
        assert!(validate_token_expiry(0, crate::config::Exposure::Internet).is_err());
    }

    #[test]
    fn json_body_limits_bound_depth_strings_arrays_and_objects() {
        let mut nested = json!({});
        for _ in 0..MAX_JSON_DEPTH {
            nested = json!({"x": nested});
        }
        assert!(check_json_limits(&nested, 0).is_ok());
        nested = json!({"x": nested});
        assert_eq!(check_json_limits(&nested, 0).unwrap_err().status, StatusCode::BAD_REQUEST);
        assert!(check_json_limits(&json!("x".repeat(MAX_JSON_STRING_BYTES)), 0).is_ok());
        assert_eq!(
            check_json_limits(&json!("x".repeat(MAX_JSON_STRING_BYTES + 1)), 0).unwrap_err().status,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            check_json_limits(&json!(vec![Value::Null; MAX_JSON_ARRAY_ITEMS + 1]), 0).unwrap_err().status,
            StatusCode::BAD_REQUEST
        );
        let too_many_fields = Value::Object((0..=MAX_JSON_OBJECT_FIELDS).map(|i| (i.to_string(), Value::Null)).collect());
        assert_eq!(check_json_limits(&too_many_fields, 0).unwrap_err().status, StatusCode::BAD_REQUEST);
    }

    #[test]
    fn fetch_metadata_allows_only_same_origin_requests_and_console_document_navigation() {
        let api_fetch = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "cross-site".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "empty".parse().unwrap()),
        ]);
        assert!(!fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &api_fetch));

        let cross_site_document = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "cross-site".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "navigate".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "document".parse().unwrap()),
        ]);
        assert!(fetch_metadata_allowed(&Method::GET, "/missions/w1/m1", &cross_site_document));
        assert!(!fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &cross_site_document));
        assert!(!fetch_metadata_allowed(&Method::GET, "/healthz", &cross_site_document));
        assert!(!fetch_metadata_allowed(&Method::POST, "/", &cross_site_document));

        let same_origin = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "same-origin".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "empty".parse().unwrap()),
        ]);
        assert!(fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &same_origin));
        let same_origin_module = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "same-origin".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "script".parse().unwrap()),
        ]);
        assert!(fetch_metadata_allowed(&Method::GET, "/chunk-terminal.js", &same_origin_module));
        let same_origin_style = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "same-origin".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "no-cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "style".parse().unwrap()),
        ]);
        assert!(fetch_metadata_allowed(&Method::GET, "/app.css", &same_origin_style));
        assert!(fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &HeaderMap::new()));
        let malformed_same_origin = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "same-origin".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "document".parse().unwrap()),
        ]);
        assert!(!fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &malformed_same_origin));
        let same_site = HeaderMap::from_iter([
            ("sec-fetch-site".parse().unwrap(), "same-site".parse().unwrap()),
            ("sec-fetch-mode".parse().unwrap(), "cors".parse().unwrap()),
            ("sec-fetch-dest".parse().unwrap(), "empty".parse().unwrap()),
        ]);
        assert!(!fetch_metadata_allowed(&Method::GET, "/api/v1/tokens", &same_site));
    }

    #[test]
    fn public_json_timestamp_normalization_is_recursive_and_bounded() {
        let mut value = json!({
            "session":{"expires_at":0},
            "worker":{"last_seen":1},
            "created_at":i64::MAX,
            "unrelated_number":123
        });
        normalize_api_timestamps(&mut value);
        assert_eq!(value["session"]["expires_at"], "1970-01-01T00:00:00Z");
        assert_eq!(value["worker"]["last_seen"], "1970-01-01T00:00:01Z");
        assert!(value["created_at"].is_null(), "out-of-range epochs are never formatted");
        assert_eq!(value["unrelated_number"], 123);
    }

    #[tokio::test]
    async fn api_errors_use_stable_rfc9457_problem_details() {
        let response = err(StatusCode::CONFLICT, "request-id-conflict", "request id was reused").into_response();
        assert_eq!(response.headers()[header::CONTENT_TYPE], "application/problem+json");
        let bytes = axum::body::to_bytes(response.into_body(), 1024).await.expect("body");
        let body: Value = serde_json::from_slice(&bytes).expect("JSON problem");
        assert_eq!(body["type"], "https://llm-tools.dev/problems/request-id-conflict");
        assert_eq!(body["status"], 409);
        assert_eq!(body["detail"], "request id was reused");
        assert_eq!(body["error"]["code"], "request-id-conflict");
    }

    #[test]
    fn listener_groups_expose_only_their_route_families() {
        use crate::config::ListenerGroup::*;
        assert!(listener_group_allows(Combined, "/healthz"));
        assert!(listener_group_allows(Combined, "/api/v1/worker/connect"));
        assert!(listener_group_allows(ConsoleRest, "/api/v1/missions"));
        assert!(!listener_group_allows(ConsoleRest, "/healthz"));
        assert!(!listener_group_allows(ConsoleRest, "/api/v1/worker/connect"));
        assert!(listener_group_allows(WorkerChannel, "/api/v1/worker/connect"));
        assert!(listener_group_allows(WorkerChannel, "/api/v1/worker/terminal/abc"));
        assert!(!listener_group_allows(WorkerChannel, "/api/v1/workers"));
        assert!(listener_group_allows(Health, "/healthz"));
        assert!(!listener_group_allows(Health, "/api/v1/health"));
    }
}

fn route_matches(template: &str, path: &str) -> bool {
    let mut expected = template.split('/');
    let mut actual = path.split('/');
    loop {
        match (expected.next(), actual.next()) {
            (None, None) => return true,
            (Some(want), Some(got)) if want.starts_with('{') && want.ends_with('}') && !got.is_empty() => {}
            (Some(want), Some(got)) if want == got => {}
            _ => return false,
        }
    }
}

pub fn router(st: St) -> Router {
    build_router(st, None)
}

pub fn router_for_group(st: St, group: crate::config::ListenerGroup) -> Router {
    build_router(st, Some(group))
}

fn build_router(st: St, group: Option<crate::config::ListenerGroup>) -> Router {
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/api/v1/auth/state", get(auth_state))
        .route("/api/v1/auth/setup", post(setup).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/login", post(login).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/totp/enroll", post(totp_enroll).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/totp/confirm", post(totp_confirm).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/totp/disable", post(totp_disable).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/passkeys", get(list_passkeys))
        .route("/api/v1/auth/sessions", get(list_sessions))
        .route("/api/v1/auth/sessions/{id}", delete(revoke_session))
        .route(
            "/api/v1/auth/passkeys/registration/start",
            post(passkey_register_start).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)),
        )
        .route("/api/v1/auth/passkeys/registration/finish", post(passkey_register_finish))
        .route(
            "/api/v1/auth/passkeys/re-enroll",
            post(passkey_reenroll).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)),
        )
        .route(
            "/api/v1/auth/passkeys/assertion/start",
            post(passkey_assert_start).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)),
        )
        .route("/api/v1/auth/passkeys/assertion/finish", post(passkey_assert_finish))
        .route("/api/v1/auth/logout", post(logout).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/password", post(change_password).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/workers", get(list_workers).post(add_worker))
        .route("/api/v1/workers/{name}", get(get_worker).delete(revoke_worker))
        .route("/api/v1/workers/{name}/restore", post(worker_restore_resource))
        .route("/api/v1/workers/{name}/doctor", get(worker_doctor_resource))
        .route("/api/v1/workers/{name}/agents", get(worker_agents_resource))
        .route("/api/v1/workers/{name}/audit", get(worker_audit_resource))
        .route("/api/v1/workers/{name}/gc", post(worker_gc_resource))
        .route("/api/v1/tokens", get(list_tokens).post(create_token))
        .route("/api/v1/audit", get(audit_resource))
        .route("/api/v1/tokens/{name}", delete(revoke_token))
        .route("/api/v1/security/notifications", get(list_security_notifications))
        .route("/api/v1/security/notifications/{id}", delete(dismiss_security_notification))
        .route("/api/v1/missions", get(list_missions).post(create_mission_resource))
        .route("/api/v1/missions/{id}", get(mission_status_resource))
        .route("/api/v1/missions/{id}/events", get(mission_events))
        .route("/api/v1/missions/{id}/operations", get(mission_operations))
        .route("/api/v1/missions/{id}/agents", get(mission_agents_resource))
        .route(
            "/api/v1/missions/{id}/settings",
            get(mission_settings_resource).patch(mission_settings_resource),
        )
        .route("/api/v1/missions/{id}/rotation", get(mission_rotation_resource).put(mission_rotation_resource))
        .route(
            "/api/v1/missions/{id}/proposals",
            get(mission_proposals_resource).post(mission_proposals_resource),
        )
        .route("/api/v1/missions/{id}/proposals/{n}/{action}", post(proposal_decision_resource))
        .route("/api/v1/missions/{id}/suspend-until-credits", post(suspend_until_credits_resource))
        .route("/api/v1/missions/{id}/steer", post(mission_steer_resource))
        .route("/api/v1/missions/{id}/finish", post(mission_finish_resource))
        .route("/api/v1/missions/{id}/{action}", post(mission_action))
        .route("/api/v1/missions/{id}/tasks", get(mission_tasks).post(add_mission_task))
        .route("/api/v1/missions/{id}/tasks/{n}", get(mission_task_show).patch(amend_mission_task))
        .route("/api/v1/missions/{id}/tasks/{n}/verify", post(verify_mission_task))
        .route("/api/v1/missions/{id}/tasks/{n}/{action}", post(task_action))
        .route("/api/v1/commands", post(post_command))
        .route("/api/v1/commands/{id}", get(get_command))
        .route("/api/v1/events", get(list_events))
        .route("/api/v1/stream", get(stream))
        .route("/api/v1/diagnostics", get(diagnostics))
        .route("/api/v1/openapi.json", get(crate::openapi::document))
        .route("/api/docs", get(crate::openapi::reference))
        .route("/api/v1/worker/connect", get(crate::ws_worker::connect))
        .route("/api/v1/terminal", get(crate::terminal::browser))
        .route("/api/v1/worker/terminal/{session_id}", get(crate::terminal::worker))
        .fallback(static_files);
    let app = if let Some(group) = group {
        app.layer(axum::middleware::from_fn_with_state(group, listener_group_gate))
    } else {
        app
    };
    app.layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(axum::middleware::from_fn_with_state(st.clone(), reserve_gate))
        .layer(axum::middleware::from_fn(fetch_metadata_guard))
        .layer(axum::middleware::map_response(security_headers))
        .with_state(st)
}

/// The listener groups are enforced before dispatch, including the SPA and API fallbacks.
fn listener_group_allows(group: crate::config::ListenerGroup, path: &str) -> bool {
    let worker_path = path == "/api/v1/worker" || path.starts_with("/api/v1/worker/");
    match group {
        crate::config::ListenerGroup::Combined => true,
        crate::config::ListenerGroup::ConsoleRest => !worker_path && path != "/healthz",
        crate::config::ListenerGroup::WorkerChannel => worker_path,
        crate::config::ListenerGroup::Health => path == "/healthz",
    }
}

async fn listener_group_gate(State(group): State<crate::config::ListenerGroup>, req: Request, next: axum::middleware::Next) -> Response {
    if !listener_group_allows(group, req.uri().path()) {
        return err(StatusCode::NOT_FOUND, "route-not-found", "route is not available on this listener").into_response();
    }
    next.run(req).await
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

fn is_console_document(path: &str) -> bool {
    !path.starts_with("/api") && path != "/healthz" && !path.rsplit('/').next().unwrap_or_default().contains('.')
}

fn fetch_metadata_allowed(method: &Method, path: &str, headers: &HeaderMap) -> bool {
    let Some(site) = headers.get("sec-fetch-site").and_then(|value| value.to_str().ok()) else {
        // Non-browser clients do not send Fetch Metadata; cookie mutations remain protected by
        // the independent exact-Origin and per-session CSRF checks.
        return true;
    };
    let mode = headers.get("sec-fetch-mode").and_then(|value| value.to_str().ok());
    let dest = headers.get("sec-fetch-dest").and_then(|value| value.to_str().ok());
    let top_level_console_navigation = *method == Method::GET && mode == Some("navigate") && dest == Some("document") && is_console_document(path);
    if top_level_console_navigation {
        return true;
    }
    if site != "same-origin" {
        return false;
    }
    match (mode, dest) {
        // Same-origin ES modules use mode=cors and dest=script. Rejecting every
        // destination except `empty` breaks the lazy terminal bundle.
        (Some("cors" | "same-origin"), Some("empty" | "script" | "style" | "font" | "image" | "worker" | "sharedworker")) => true,
        (Some("websocket"), Some("empty")) => true,
        (Some("no-cors"), Some("image" | "script" | "style" | "font")) => true,
        (Some("navigate"), Some("document")) => top_level_console_navigation,
        _ => false,
    }
}

async fn fetch_metadata_guard(req: Request, next: axum::middleware::Next) -> Response {
    let is_bearer_request = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.starts_with("Bearer "));
    let has_session_cookie = !is_bearer_request && cookie_value(req.headers()).is_some();
    if has_session_cookie && !fetch_metadata_allowed(req.method(), req.uri().path(), req.headers()) {
        return err(
            StatusCode::FORBIDDEN,
            "cross-site-request",
            "cookie-authenticated requests must be same-origin; only console document navigation is exempt",
        )
        .into_response();
    }
    next.run(req).await
}
