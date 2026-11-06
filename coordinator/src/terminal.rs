//! Remote terminal relay (PROTO-009, SEC-006, WEB-007, WEB-008).
//!
//! A browser opens `/api/v1/terminal`; the coordinator asks the worker to open a session
//! (`terminal.open` on the control connection), waits for the worker's own WebSocket on
//! `/api/v1/worker/terminal/<session_id>` and then relays JSON text frames between the two.
//! The coordinator never decides who may write: the worker grants the writer role and reports
//! it in `s` frames.

use crate::auth::sha256_hex;
use crate::http::{err, origin_ok, ApiError, Authed, Principal, St};
use crate::hub::{SendResult, TermReg, TermStatus};
use crate::protocol::{valid_id, valid_name};
use axum::extract::ws::{close_code, CloseFrame, Message as Ws, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::timeout;

/// How long the worker has to attach its terminal socket after `terminal.open`.
pub const ATTACH_TIMEOUT: Duration = Duration::from_secs(10);
/// Maximum bytes queued for one browser viewer before it is closed with 1013.
pub const VIEWER_QUEUE_BYTES: usize = 256 * 1024;
/// Maximum decoded size of one input frame.
pub const MAX_INPUT_BYTES: usize = 64 * 1024;
/// Transport limit for browser frames: larger than a valid input frame so oversize input is dropped, not fatal.
const BROWSER_FRAME_LIMIT: usize = 256 * 1024;
const WORKER_FRAME_LIMIT: usize = 1024 * 1024;
const TO_WORKER_QUEUE: usize = 64;
const CLOSE_GRACE: Duration = Duration::from_secs(2);
const LAG_GRACE: Duration = Duration::from_secs(30);

fn close_frame(code: u16, reason: &str) -> Ws {
    Ws::Close(Some(CloseFrame {
        code,
        reason: reason.chars().take(100).collect::<String>().into(),
    }))
}

fn clean(s: &str) -> String {
    s.chars().filter(|c| !c.is_control()).take(64).collect()
}

fn x_frame(reason: &str) -> String {
    json!({"t": "x", "reason": reason}).to_string()
}

// ---------------------------------------------------------------- browser endpoint

struct Params {
    worker: String,
    mission: String,
    mode: &'static str,
    cols: u64,
    rows: u64,
}

fn parse_params(q: &HashMap<String, String>) -> Result<Params, ApiError> {
    let bad = |m: &str| err(StatusCode::BAD_REQUEST, "invalid-request", m.to_string());
    let worker = q.get("worker").filter(|w| valid_name(w)).ok_or_else(|| bad("worker must be a worker name"))?;
    let mission = q.get("mission").filter(|m| valid_id(m)).ok_or_else(|| bad("mission must be a mission id"))?;
    let mode = match q.get("mode").map(String::as_str) {
        None | Some("view") => "view",
        Some("write") => "write",
        _ => return Err(bad("mode must be view or write")),
    };
    let dim = |k: &str, default: u64| -> Result<u64, ApiError> {
        match q.get(k) {
            None => Ok(default),
            Some(v) => v
                .parse::<u64>()
                .ok()
                .filter(|n| (1..=1000).contains(n))
                .ok_or_else(|| bad(&format!("{k} must be an integer from 1 to 1000"))),
        }
    };
    Ok(Params {
        worker: worker.clone(),
        mission: mission.clone(),
        mode,
        cols: dim("cols", 80)?,
        rows: dim("rows", 24)?,
    })
}

pub async fn browser(
    State(st): State<St>,
    Authed(p, ip): Authed,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    if matches!(p, Principal::Session { .. }) && !origin_ok(&headers, &st.cfg) {
        return Err(err(
            StatusCode::FORBIDDEN,
            "bad-origin",
            "Origin header missing or not the coordinator's origin",
        ));
    }
    let params = parse_params(&q)?;
    let actor = p.actor();
    Ok(ws
        .max_message_size(BROWSER_FRAME_LIMIT)
        .max_frame_size(BROWSER_FRAME_LIMIT)
        .on_upgrade(move |socket| browser_session(st, socket, params, actor, ip))
        .into_response())
}

/// Why a session ended and what to tell the browser.
enum End {
    Browser,
    Worker(String),
    Lagged,
    Shutdown,
    Idle,
}

enum ViewerItem {
    Ping,
    Frame(String, usize),
    Close(u16, String),
}

async fn browser_session(st: St, mut socket: WebSocket, p: Params, actor: String, ip: String) {
    let refuse = |reason: &str| {
        let text = x_frame(reason);
        (text, reason.to_string())
    };
    let mut reg = match st.hub.terminal_register(&p.worker) {
        Ok(r) => r,
        Err(reason) => {
            let (text, reason) = refuse(reason);
            let _ = socket.send(Ws::Text(text.into())).await;
            let _ = socket.send(close_frame(close_code::AGAIN, &reason)).await;
            return;
        }
    };
    st.db.audit(
        &actor,
        "terminal.open",
        &format!("worker={} mission={} mode={} session={}", clean(&p.worker), clean(&p.mission), p.mode, reg.id),
        &ip,
    );
    let open = json!({"session_id": reg.id, "mission_id": p.mission, "mode": p.mode, "cols": p.cols, "rows": p.rows});
    if !matches!(st.hub.send_to_worker(&p.worker, "terminal.open", open), SendResult::Queued) {
        let _ = socket.send(Ws::Text(x_frame("worker offline").into())).await;
        let _ = socket.send(close_frame(close_code::NORMAL, "worker offline")).await;
        return;
    }
    let mut shutdown = st.shutdown.clone();
    // ---- wait for the worker's terminal socket
    let deadline = tokio::time::sleep(ATTACH_TIMEOUT);
    tokio::pin!(deadline);
    let worker_ws = loop {
        tokio::select! {
            attached = &mut reg.attach => match attached {
                Ok(w) => break w,
                Err(_) => return,
            },
            status = reg.status.recv() => match status {
                Some(TermStatus { state, reason, .. }) if state != "opened" => {
                    let reason = if reason.is_empty() { format!("terminal {state}") } else { reason };
                    let _ = socket.send(Ws::Text(x_frame(&reason).into())).await;
                    let _ = socket.send(close_frame(close_code::NORMAL, &reason)).await;
                    return;
                }
                Some(_) => {}
                None => return,
            },
            _ = &mut deadline => {
                let _ = socket.send(Ws::Text(x_frame("the worker did not open the terminal in time").into())).await;
                let _ = socket.send(close_frame(close_code::NORMAL, "worker timeout")).await;
                return;
            }
            incoming = socket.recv() => match incoming {
                Some(Ok(Ws::Close(_))) | None | Some(Err(_)) => {
                    st.hub.send_to_worker(&p.worker, "terminal.close", json!({"session_id": reg.id}));
                    return;
                }
                Some(Ok(_)) => {}
            },
            _ = shutdown.changed() => { let _ = socket.send(close_frame(close_code::AWAY, "shutting down")).await; return }
        }
    };
    let keepalive = (
        Duration::from_secs(st.cfg.ws_ping_interval_secs),
        Duration::from_secs(st.cfg.ws_idle_timeout_secs),
    );
    let end = relay(&mut reg, socket, worker_ws, &mut shutdown, keepalive).await;
    if matches!(end, End::Browser | End::Lagged | End::Shutdown | End::Idle) {
        st.hub.send_to_worker(&p.worker, "terminal.close", json!({"session_id": reg.id}));
    }
}

/// Validate one browser frame; returns the frame to forward to the worker, or `None` to drop it.
pub fn checked_browser_frame(text: &str) -> Option<String> {
    let v: Value = serde_json::from_str(text).ok()?;
    match v.get("t")?.as_str()? {
        "i" => {
            let d = v.get("d")?.as_str()?;
            // Cheap bound before decoding: 64 KiB decodes from at most 87384 base64 characters.
            if d.is_empty() || d.len() > MAX_INPUT_BYTES.div_ceil(3) * 4 {
                return None;
            }
            let bytes = base64::engine::general_purpose::STANDARD.decode(d).ok()?;
            (!bytes.is_empty() && bytes.len() <= MAX_INPUT_BYTES).then(|| json!({"t": "i", "d": d}).to_string())
        }
        "r" => {
            let dim = |k: &str| v.get(k).and_then(Value::as_u64).filter(|n| (1..=1000).contains(n));
            Some(json!({"t": "r", "cols": dim("cols")?, "rows": dim("rows")?}).to_string())
        }
        _ => None,
    }
}

/// Validate one worker frame; returns the frame to forward to the browser, `Err(reason)` when the
/// worker ended the session, or `Ok(None)` to drop it.
pub fn checked_worker_frame(text: &str) -> Result<Option<String>, String> {
    let Ok(v) = serde_json::from_str::<Value>(text) else { return Ok(None) };
    let Some(t) = v.get("t").and_then(Value::as_str) else { return Ok(None) };
    Ok(match t {
        "o" => v.get("d").and_then(Value::as_str).map(|d| json!({"t": "o", "d": d}).to_string()),
        "s" => {
            let dim = |k: &str| v.get(k).and_then(Value::as_u64).filter(|n| (1..=1000).contains(n));
            let mode = v.get("mode").and_then(Value::as_str).filter(|m| matches!(*m, "view" | "write"));
            match (dim("cols"), dim("rows"), mode) {
                (Some(cols), Some(rows), Some(mode)) => {
                    let reason = v.get("reason").and_then(Value::as_str).unwrap_or("");
                    Some(json!({"t": "s", "cols": cols, "rows": rows, "mode": mode, "reason": reason}).to_string())
                }
                _ => None,
            }
        }
        "x" => return Err(v.get("reason").and_then(Value::as_str).unwrap_or("session ended").to_string()),
        _ => None,
    })
}

async fn relay(
    reg: &mut TermReg,
    browser: WebSocket,
    worker: WebSocket,
    shutdown: &mut tokio::sync::watch::Receiver<bool>,
    (ping, idle): (Duration, Duration),
) -> End {
    let (mut b_sink, mut b_stream) = browser.split();
    let (mut w_sink, mut w_stream) = worker.split();
    let queued = Arc::new(AtomicUsize::new(0));
    let (v_tx, mut v_rx) = mpsc::unbounded_channel::<ViewerItem>();
    let (w_tx, mut w_rx) = mpsc::channel::<String>(TO_WORKER_QUEUE);

    // Viewer writer: drains the byte-bounded queue into the browser socket.
    let mut writer = {
        let queued = queued.clone();
        tokio::spawn(async move {
            while let Some(item) = v_rx.recv().await {
                match item {
                    ViewerItem::Frame(text, n) => {
                        let ok = b_sink.send(Ws::Text(text.into())).await.is_ok();
                        queued.fetch_sub(n, Ordering::SeqCst);
                        if !ok {
                            return;
                        }
                    }
                    ViewerItem::Ping => {
                        if b_sink.send(Ws::Ping(Vec::new().into())).await.is_err() {
                            return;
                        }
                    }
                    ViewerItem::Close(code, reason) => {
                        let _ = b_sink.send(close_frame(code, &reason)).await;
                        let _ = b_sink.close().await;
                        return;
                    }
                }
            }
        })
    };
    // Worker writer: input and resize frames; a full queue drops frames instead of stalling the relay.
    let worker_writer = tokio::spawn(async move {
        while let Some(text) = w_rx.recv().await {
            if w_sink.send(Ws::Text(text.into())).await.is_err() {
                return w_sink;
            }
        }
        w_sink
    });

    let push = |text: String| -> bool {
        let n = text.len();
        if queued.fetch_add(n, Ordering::SeqCst) + n > VIEWER_QUEUE_BYTES {
            queued.fetch_sub(n, Ordering::SeqCst);
            return false;
        }
        v_tx.send(ViewerItem::Frame(text, n)).is_ok()
    };

    let mut writer_done = false;
    let mut tick = tokio::time::interval(ping);
    tick.tick().await;
    let mut last_heard = std::time::Instant::now();
    let end = loop {
        tokio::select! {
            incoming = b_stream.next() => {
                if matches!(incoming, Some(Ok(_))) { last_heard = std::time::Instant::now(); }
                match incoming {
                Some(Ok(Ws::Text(t))) => {
                    if let Some(frame) = checked_browser_frame(t.as_str()) {
                        let _ = w_tx.try_send(frame);
                    }
                }
                Some(Ok(Ws::Close(_))) | None | Some(Err(_)) => break End::Browser,
                Some(Ok(_)) => {}
                }
            },
            _ = tick.tick() => {
                if last_heard.elapsed() >= idle { break End::Idle }
                let _ = v_tx.send(ViewerItem::Ping);
            },
            frame = w_stream.next() => match frame {
                Some(Ok(Ws::Text(t))) => match checked_worker_frame(t.as_str()) {
                    Ok(Some(out)) => if !push(out) { break End::Lagged },
                    Ok(None) => {}
                    Err(reason) => break End::Worker(reason),
                },
                Some(Ok(Ws::Close(f))) => break End::Worker(f.map(|f| f.reason.to_string()).filter(|r| !r.is_empty()).unwrap_or_else(|| "worker closed the session".into())),
                None | Some(Err(_)) => break End::Worker("worker connection lost".into()),
                Some(Ok(_)) => {}
            },
            status = reg.status.recv() => if let Some(s) = status {
                if s.state != "opened" {
                    // The worker's own `x` frame normally arrives first; this covers a worker that only reports status.
                    break End::Worker(if s.reason.is_empty() { "terminal closed".into() } else { s.reason });
                }
            },
            _ = shutdown.changed() => break End::Shutdown,
            _ = &mut writer, if !writer_done => { writer_done = true; break End::Browser }
        }
    };
    // ---- teardown
    match &end {
        End::Worker(reason) => {
            let _ = v_tx.send(ViewerItem::Frame(x_frame(reason), 0));
            let _ = v_tx.send(ViewerItem::Close(close_code::NORMAL, reason.clone()));
        }
        End::Lagged => {
            let _ = v_tx.send(ViewerItem::Close(close_code::AGAIN, "viewer too slow".into()));
        }
        End::Shutdown => {
            let _ = v_tx.send(ViewerItem::Close(close_code::AWAY, "shutting down".into()));
        }
        End::Browser => {
            let _ = v_tx.send(ViewerItem::Close(close_code::NORMAL, "closed".into()));
        }
        End::Idle => {
            let _ = v_tx.send(ViewerItem::Close(close_code::AWAY, "idle timeout".into()));
        }
    }
    drop(v_tx);
    drop(w_tx);
    if !writer_done {
        // A lagging browser does not read, so the close frame may stay queued behind buffered data.
        // Give it a long grace in the background, then drop the socket.
        let grace = if matches!(end, End::Lagged) { LAG_GRACE } else { CLOSE_GRACE };
        tokio::spawn(async move {
            if timeout(grace, &mut writer).await.is_err() {
                writer.abort();
            }
        });
    }
    if let Ok(Ok(mut sink)) = timeout(CLOSE_GRACE, worker_writer).await {
        let _ = timeout(CLOSE_GRACE, sink.send(close_frame(close_code::NORMAL, "closed"))).await;
        let _ = timeout(CLOSE_GRACE, sink.close()).await;
    }
    end
}

// ---------------------------------------------------------------- worker endpoint

pub async fn worker(State(st): State<St>, Path(session_id): Path<String>, headers: HeaderMap, ws: WebSocketUpgrade) -> Response {
    let fail = |status: StatusCode, code: &'static str, m: &'static str| err(status, code, m).into_response();
    let token = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "));
    let name = match token.map(|t| st.db.worker_by_token(&sha256_hex(t))) {
        Some(Ok(Some(w))) => w.name,
        Some(Err(e)) => {
            eprintln!("database error: {e}");
            return fail(StatusCode::INTERNAL_SERVER_ERROR, "internal", "internal error");
        }
        _ => return fail(StatusCode::UNAUTHORIZED, "unauthorized", "invalid or revoked worker token"),
    };
    if !valid_id(&session_id) {
        return fail(StatusCode::NOT_FOUND, "unknown-session", "no such terminal session");
    }
    // Unknown id, another worker's session and an already attached session all look the same.
    let Some(attach) = st.hub.terminal_claim(&session_id, &name) else {
        return fail(StatusCode::NOT_FOUND, "unknown-session", "no such terminal session");
    };
    ws.max_message_size(WORKER_FRAME_LIMIT)
        .max_frame_size(WORKER_FRAME_LIMIT)
        .on_upgrade(move |socket| async move {
            let _ = attach.send(socket);
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_frames_are_validated() {
        let b64 = base64::engine::general_purpose::STANDARD;
        let ok = json!({"t": "i", "d": b64.encode("hi")}).to_string();
        assert_eq!(checked_browser_frame(&ok).as_deref(), Some(ok.as_str()));
        for bad in [
            "not json",
            "[]",
            r#"{"t":"zzz"}"#,
            r#"{"t":"i"}"#,
            r#"{"t":"i","d":5}"#,
            r#"{"t":"i","d":""}"#,
            r#"{"t":"i","d":"!!!"}"#,
            r#"{"t":"r","cols":0,"rows":5}"#,
            r#"{"t":"r","cols":1001,"rows":5}"#,
            r#"{"t":"r","cols":"80","rows":5}"#,
        ] {
            assert!(checked_browser_frame(bad).is_none(), "{bad}");
        }
        let big = json!({"t": "i", "d": b64.encode(vec![b'a'; MAX_INPUT_BYTES + 1])}).to_string();
        assert!(checked_browser_frame(&big).is_none());
        let max = json!({"t": "i", "d": b64.encode(vec![b'a'; MAX_INPUT_BYTES])}).to_string();
        assert!(checked_browser_frame(&max).is_some());
    }

    #[test]
    fn worker_frames_are_validated() {
        assert!(matches!(checked_worker_frame(r#"{"t":"o","d":"AA=="}"#), Ok(Some(_))));
        assert!(matches!(checked_worker_frame(r#"{"t":"o"}"#), Ok(None)));
        assert!(matches!(
            checked_worker_frame(r#"{"t":"s","cols":80,"rows":24,"mode":"view","reason":""}"#),
            Ok(Some(_))
        ));
        assert!(matches!(
            checked_worker_frame(r#"{"t":"s","cols":0,"rows":24,"mode":"view","reason":""}"#),
            Ok(None)
        ));
        assert_eq!(checked_worker_frame(r#"{"t":"x","reason":"gone"}"#), Err("gone".to_string()));
    }
}
