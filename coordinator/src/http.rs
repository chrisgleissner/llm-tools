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
use std::collections::HashMap;
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
        let status = self.status;
        let mut r = (
            status,
            axum::Json(json!({
                "type": format!("https://llm-tools.dev/problems/{}", self.code),
                "title": "Coordinator request failed",
                "status": status.as_u16(),
                "detail": self.message,
                "code": self.code,
                "error": {"code": self.code, "message": self.message}
            })),
        )
            .into_response();
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
        token_hash: String,
        csrf: String,
        remember: bool,
        expires_at: i64,
        idle_secs: i64,
    },
    Token {
        name: String,
        scopes: Vec<String>,
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
                scopes: token.scopes,
            }),
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
    if cfg.allowed_origins.iter().any(|o| o == origin) || cfg.public_origin.as_deref() == Some(origin) {
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
            Principal::Token { name, .. } => format!("token:{name}"),
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
        if let Principal::Token { name, scopes: granted } = &principal {
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
                let remote = parts.extensions.get::<Peer>().map(|peer| peer.0.ip().to_string()).unwrap_or_default();
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
        let remote = parts.extensions.get::<Peer>().map(|p| p.0.ip().to_string()).unwrap_or_default();
        if let Principal::Token { name, scopes: granted } = &principal {
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
            if !matches!(parts.method, Method::GET | Method::HEAD | Method::OPTIONS) {
                let address_key = parts
                    .extensions
                    .get::<Peer>()
                    .map(|peer| auth::addr_key(&peer.0.ip()))
                    .unwrap_or_else(|| "unknown".to_string());
                let keys = [
                    (format!("token:{name}"), st.cfg.mutation_limit_per_token),
                    (format!("address:{address_key}"), st.cfg.mutation_limit_per_address),
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
        let remote = parts.extensions.get::<Peer>().map(|p| p.0.ip());
        let Some(peer) = remote else { return Ok(Remote(String::new())) };
        let forwarded = parts.headers.get("forwarded").and_then(|v| v.to_str().ok());
        let x_forwarded_for = parts.headers.get("x-forwarded-for").and_then(|v| v.to_str().ok());
        let client = crate::client_ip::resolve(peer, forwarded, x_forwarded_for, &state.cfg.trusted_proxies);
        Ok(Remote(client.to_string()))
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
        aaguid: None,
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
    let (token, csrf) = (auth::random_token(), auth::random_token());
    st.db.session_create_with(&auth::sha256_hex(&token), &csrf, false)?;
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
    if let Some(retry_after) = st.mutation_limiter.check(&[(format!("address:{address}"), st.cfg.mutation_limit_per_address)]) {
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
                "expires_at": timestamp(t.expires_at)
            })
        })
        .collect();
    Ok(json_ok(StatusCode::OK, json!({"tokens": tokens})))
}

async fn create_token(State(st): State<St>, Authed(p, ip): Authed, body: Buffered) -> ApiResult {
    let m = parse_body(&body.bytes)?;
    no_extra(&m, &["name", "scopes", "expires_days"])?;
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
    let expires_at = (days > 0).then(|| crate::now() + (days as i64) * 86_400);
    let token_id = auth::sha256_hex(&auth::random_token())[..16].to_string();
    let token_secret = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(auth::random_token())
        .map(hex::encode)
        .expect("URL-safe token generator output decodes");
    let secret = format!("lbr_pat_{token_id}_{token_secret}");
    if !st
        .db
        .token_create_identified(name, &token_id, &auth::sha256_hex(&token_secret), expires_at, &scopes)?
    {
        return Err(err(StatusCode::CONFLICT, "token-exists", "a token with this name already exists"));
    }
    let detail = format!(
        "name={name} scopes={} expires_at={}",
        scopes.join(","),
        expires_at.map_or_else(|| "never".into(), |e| e.to_string())
    );
    st.db.audit(&p.actor(), "token.create", &detail, &ip);
    Ok(json_ok(
        StatusCode::CREATED,
        json!({
            "token": {"name": name, "id": token_id, "scopes": scopes, "created_at": timestamp(Some(crate::now())), "last_used": Value::Null, "expires_at": timestamp(expires_at)},
            "secret": secret
        }),
    ))
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
           "platform": w.platform, "capabilities": w.capabilities, "policy": policy})
}

async fn list_workers(State(st): State<St>, _a: Authed) -> ApiResult {
    let workers: Vec<Value> = st.db.workers_list()?.iter().map(|w| worker_json(&st, w)).collect();
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
/// `limit` (default 100, maximum 500) and an authenticated, encrypted opaque `cursor`. Legacy `offset` remains
/// accepted. The reply carries `total`, `next_cursor` and status facets.
async fn list_missions(State(st): State<St>, _a: Authed, Query(q): Query<HashMap<String, String>>) -> ApiResult {
    let bad = |m: &str| err(StatusCode::BAD_REQUEST, "invalid-request", m.to_string());
    if q.get("worker").is_some_and(|worker| !valid_name(worker)) {
        return Err(bad("worker must be a valid worker name"));
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
    let status = if row.state == "done" { StatusCode::OK } else { StatusCode::ACCEPTED };
    let mut value = row.to_json();
    if status == StatusCode::OK {
        add_next_page_cursor(&st, &row, &mut value);
    }
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

async fn get_command(State(st): State<St>, _a: Authed, Path(id): Path<String>) -> ApiResult {
    match st.db.command_get(&id)? {
        Some(r) => {
            let mut value = r.to_json();
            add_next_page_cursor(&st, &r, &mut value);
            Ok(json_ok(StatusCode::OK, value))
        }
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
    ("/api/v1/auth/passkeys/registration/start", &["POST"], Access::Session, ""),
    ("/api/v1/auth/passkeys/registration/finish", &["POST"], Access::Session, ""),
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
    ("/api/v1/tokens/{name}", &["DELETE"], Access::Authed, "admin"),
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
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/v1/auth/state", get(auth_state))
        .route("/api/v1/auth/setup", post(setup).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route("/api/v1/auth/login", post(login).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)))
        .route(
            "/api/v1/auth/passkeys/registration/start",
            post(passkey_register_start).layer(DefaultBodyLimit::max(AUTH_BODY_MAX)),
        )
        .route("/api/v1/auth/passkeys/registration/finish", post(passkey_register_finish))
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
        .route("/api/v1/tokens/{name}", delete(revoke_token))
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
        .fallback(static_files)
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(axum::middleware::from_fn_with_state(st.clone(), reserve_gate))
        .layer(axum::middleware::from_fn(fetch_metadata_guard))
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
