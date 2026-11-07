//! Worker WebSocket endpoint `/api/v1/worker/connect` (protocol.md).

use crate::auth::sha256_hex;
use crate::db::{CommandRow, EventBatch, EventsOutcome, HelloOutcome};
use crate::http::{Peer, St};
use crate::hub::{ConnHandle, Outbound, PendingGuard, OUTBOUND_QUEUE};
use crate::protocol::{self, Message, MAX_MESSAGE_BYTES, PROTOCOL_VERSION};
use axum::extract::ws::{close_code, CloseFrame, Message as Ws, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Extension;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::sync::{mpsc, watch};
use tokio::time::{interval, timeout, Instant};

const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const PING_EVERY: Duration = Duration::from_secs(20);
/// How often a connected worker's `last_seen` is persisted. The live state comes from the hub;
/// the stored value matters after a disconnect (written then, unthrottled) or a coordinator crash,
/// where it is at most this stale. Ten minutes keeps an idle worker to six small commits an hour.
const LAST_SEEN_EVERY_SECS: i64 = 600;
const SILENCE_LIMIT: Duration = Duration::from_secs(60);

fn plain(status: StatusCode, text: &'static str) -> Response {
    (
        status,
        axum::Json(json!({"error": {"code": if status == StatusCode::UNAUTHORIZED { "unauthorized" } else { "unavailable" }, "message": text}})),
    )
        .into_response()
}

pub async fn connect(State(st): State<St>, Extension(peer): Extension<Peer>, headers: HeaderMap, ws: WebSocketUpgrade) -> Response {
    let token = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "));
    let worker = match token.map(|t| st.db.worker_by_token(&sha256_hex(t))) {
        Some(Ok(Some(w))) => w,
        Some(Err(e)) => {
            eprintln!("database error: {e}");
            return plain(StatusCode::INTERNAL_SERVER_ERROR, "internal error");
        }
        _ => return plain(StatusCode::UNAUTHORIZED, "invalid or revoked worker token"),
    };
    let Some(guard) = st.hub.try_reserve() else {
        return plain(StatusCode::SERVICE_UNAVAILABLE, "too many worker connections");
    };
    let name = worker.name;
    // One connection per worker token may wait for its hello: a leaked token cannot be used to fill
    // the connection table with silent sockets. The slot is freed when the hello arrives.
    let Some(pending) = st.hub.try_pending(&name) else {
        return plain(StatusCode::TOO_MANY_REQUESTS, "a connection of this worker is already waiting for its hello");
    };
    let remote = peer.0.ip().to_string();
    // Transport limit is twice the protocol limit so an oversized frame can be answered with our own close 1009.
    ws.max_message_size(2 * MAX_MESSAGE_BYTES)
        .max_frame_size(2 * MAX_MESSAGE_BYTES)
        .on_upgrade(move |socket| async move {
            let _guard = guard;
            run(st, name, remote, pending, socket).await;
        })
}

fn close_frame(code: u16, reason: &'static str) -> Ws {
    Ws::Close(Some(CloseFrame { code, reason: reason.into() }))
}

async fn send(st: &St, socket: &mut WebSocket, text: String) -> bool {
    st.hub.count_message("worker_out", &text);
    socket.send(Ws::Text(text.into())).await.is_ok()
}

/// Count an incoming text frame for the diagnostics byte counters.
fn count_in(st: &St, frame: &Option<Result<Ws, axum::Error>>) {
    if let Some(Ok(Ws::Text(t))) = frame {
        st.hub.count_message("worker_in", t.as_str());
    }
}

enum Step {
    Message(Message),
    Reject(String),
    Close(u16, &'static str),
    Skip,
}

fn classify(frame: Option<Result<Ws, axum::Error>>, name: &str, generation: u64) -> Step {
    match frame {
        None | Some(Ok(Ws::Close(_))) => Step::Close(close_code::NORMAL, "closed"),
        Some(Err(_)) => Step::Close(close_code::SIZE, "message too large or protocol error"),
        Some(Ok(Ws::Ping(_) | Ws::Pong(_))) => Step::Skip,
        Some(Ok(Ws::Binary(_))) => Step::Reject(protocol::error_message(
            name,
            generation,
            "invalid-message",
            "binary frames are not supported",
            None,
        )),
        Some(Ok(Ws::Text(t))) => {
            if t.len() > MAX_MESSAGE_BYTES {
                return Step::Close(close_code::SIZE, "message exceeds 1 MiB");
            }
            match protocol::decode(t.as_bytes()) {
                Ok(m) => Step::Message(m),
                Err(e) => {
                    let fatal = e.code == "unsupported-version";
                    // A malformed `error` message is dropped: answering errors with errors can loop.
                    if !fatal && serde_json::from_slice::<Value>(t.as_bytes()).is_ok_and(|v| v["type"] == "error") {
                        return Step::Skip;
                    }
                    let text = protocol::error_message(name, generation, e.code, &e.message, None);
                    if fatal {
                        Step::Reject(format!("\u{0}fatal{text}"))
                    } else {
                        Step::Reject(text)
                    }
                }
            }
        }
    }
}

async fn run(st: St, name: String, remote: String, pending: PendingGuard, mut socket: WebSocket) {
    let mut shutdown = st.shutdown.clone();
    // ---- handshake: wait for a valid hello
    let deadline = Instant::now() + HELLO_TIMEOUT;
    let (hello, caps) = loop {
        let frame = tokio::select! {
            f = timeout(deadline.saturating_duration_since(Instant::now()), socket.recv()) => match f { Ok(f) => f, Err(_) => { let _ = socket.send(close_frame(1008, "hello timeout")).await; return } },
            _ = shutdown.changed() => { let _ = socket.send(close_frame(1001, "shutting down")).await; return }
        };
        count_in(&st, &frame);
        match classify(frame, &name, 0) {
            Step::Close(c, r) => {
                let _ = socket.send(close_frame(c, r)).await;
                return;
            }
            Step::Skip => {}
            Step::Reject(text) => {
                if let Some(t) = text.strip_prefix("\u{0}fatal") {
                    let _ = send(&st, &mut socket, t.to_string()).await;
                    let _ = socket.send(close_frame(1002, "unsupported version")).await;
                    return;
                }
                if !send(&st, &mut socket, text).await {
                    return;
                }
            }
            Step::Message(m) if m.kind == "hello" => match protocol::check_hello(&m) {
                Ok(caps) => break (m, caps),
                Err(e) => {
                    let _ = send(&st, &mut socket, protocol::error_message(&name, 0, e.code, &e.message, Some(&m.id))).await;
                    if e.code == "unsupported-version" {
                        let _ = socket.send(close_frame(1002, "unsupported version")).await;
                        return;
                    }
                }
            },
            Step::Message(m) if m.kind == "error" => {}
            Step::Message(m) => {
                if !send(
                    &st,
                    &mut socket,
                    protocol::error_message(&name, 0, "not-welcomed", &format!("{} before hello", m.kind), Some(&m.id)),
                )
                .await
                {
                    return;
                }
            }
        }
    };
    drop(pending);
    let worker_id = hello.worker_id.clone();
    let (generation, cursor) = match st.db.worker_hello(&name, &worker_id, &hello.payload, &caps, &remote) {
        Ok(HelloOutcome::Ok { generation, cursor }) => (generation, cursor),
        Ok(HelloOutcome::WorkerIdMismatch) => {
            let _ = send(
                &st,
                &mut socket,
                protocol::error_message(
                    &name,
                    0,
                    "worker-id-mismatch",
                    "this worker name is bound to a different worker id; revoke and re-add it to re-enrol",
                    Some(&hello.id),
                ),
            )
            .await;
            let _ = socket.send(close_frame(1008, "worker id mismatch")).await;
            return;
        }
        Ok(HelloOutcome::Unknown) => {
            let _ = socket.send(close_frame(1008, "worker revoked")).await;
            return;
        }
        Err(e) => {
            eprintln!("database error: {e}");
            let _ = socket.send(close_frame(1011, "internal error")).await;
            return;
        }
    };

    // ---- register (closes any older connection) and welcome.
    // Unanswered reads are failed with `worker-offline` first: nobody is waiting for them any more, and
    // replaying them after a reconnect cost megabytes (DOG-021). Doing it before `register` means no
    // read submitted on the new connection can be caught by it.
    expire_reads(&st, &name);
    let conn_id = st.hub.next_conn_id();
    let (tx, mut rx) = mpsc::channel::<Outbound>(OUTBOUND_QUEUE);
    let (close_tx, mut close_rx) = watch::channel(None);
    st.hub.register(
        &name,
        ConnHandle {
            conn_id,
            generation,
            worker_id: worker_id.clone(),
            tx,
            close: close_tx,
        },
    );
    st.hub
        .publish(json!({"type": "worker", "worker": name, "state": "online", "generation": generation}));
    let welcome_id = protocol::new_id();
    let welcome = protocol::encode_with_id(
        &welcome_id,
        "welcome",
        &worker_id,
        generation,
        json!({"generation": generation, "protocol_version": PROTOCOL_VERSION, "event_cursor": cursor}),
    );
    let mut alive = send(&st, &mut socket, welcome).await;

    // ---- redeliver every unfinished write (reads were expired before registering, see above)
    if alive {
        match st.db.commands_resend(&name) {
            Ok(rows) => {
                for row in rows {
                    if !deliver_row(&st, &mut socket, &worker_id, generation, &row).await {
                        alive = false;
                        break;
                    }
                }
            }
            Err(e) => {
                eprintln!("database error: {e}");
                alive = false
            }
        }
    }

    // ---- main loop
    let mut tick = interval(PING_EVERY);
    tick.tick().await;
    let mut last_heard = Instant::now();
    let mut final_close: Option<(u16, &'static str)> = None;
    while alive {
        tokio::select! {
            frame = socket.recv() => {
                last_heard = Instant::now();
                count_in(&st, &frame);
                match classify(frame, &worker_id, generation) {
                    Step::Close(c, r) => { final_close = Some((c, r)); break }
                    Step::Skip => {}
                    Step::Reject(text) => {
                        let text = text.strip_prefix("\u{0}fatal").map(str::to_string).unwrap_or(text);
                        if !send(&st, &mut socket, text).await { break }
                    }
                    Step::Message(m) if is_stale_welcome_reply(&m, &welcome_id) => {
                        st.db.audit(
                            &format!("worker:{name}"),
                            "worker.stale-generation",
                            &format!("worker rejected generation {generation} in welcome {welcome_id} as stale; connection closed"),
                            &remote,
                        );
                        final_close = Some((1008, "worker rejected the welcome generation"));
                        break
                    }
                    Step::Message(m) => match handle(&st, &name, &worker_id, generation, m) {
                        Ok(replies) => for r in replies { if !send(&st, &mut socket, r).await { alive = false; break } },
                        Err(()) => { final_close = Some((1011, "internal error")); break }
                    },
                }
            }
            item = rx.recv() => {
                let Some(item) = item else { final_close = *close_rx.borrow(); break };
                if !send(&st, &mut socket, item.text).await { break }
                if let Some(id) = item.command_id {
                    if let Ok(Some(row)) = st.db.command_sent(&id) { st.hub.publish(json!({"type": "command", "command": row.to_notice()})); }
                }
            }
            _ = close_rx.changed() => {
                final_close = *close_rx.borrow();
                break;
            }
            _ = shutdown.changed() => { final_close = Some((1001, "shutting down")); break }
            _ = tick.tick() => {
                if last_heard.elapsed() > SILENCE_LIMIT { final_close = Some((1001, "silent for 60 s")); break }
                match st.db.worker_get(&name) {
                    Ok(Some(w)) if w.worker_id.as_deref() == Some(worker_id.as_str()) => { let _ = st.db.worker_seen(&name, LAST_SEEN_EVERY_SECS); }
                    Ok(None) => { final_close = Some((1008, "worker revoked")); break }
                    _ => {}
                }
                if !send(&st, &mut socket, protocol::encode("ping", &worker_id, generation, json!({}))).await { break }
            }
        }
    }
    if let Some((code, reason)) = final_close {
        let _ = socket.send(close_frame(code, reason)).await;
    }
    if st.hub.unregister(&name, conn_id) {
        let _ = st.db.worker_disconnected(&name);
        expire_reads(&st, &name);
        st.hub
            .publish(json!({"type": "worker", "worker": name, "state": "offline", "generation": generation}));
    }
}

/// Finish the worker's unanswered read-only commands with a `worker-offline` failure and tell the browsers.
fn expire_reads(st: &St, name: &str) {
    match st.db.commands_expire_reads(name) {
        Ok(rows) => {
            for row in rows {
                st.hub.publish(json!({"type": "command", "command": row.to_notice()}));
            }
        }
        Err(e) => eprintln!("database error: {e}"),
    }
}

async fn deliver_row(st: &St, socket: &mut WebSocket, worker_id: &str, generation: u64, row: &CommandRow) -> bool {
    let mut payload = row.request.clone();
    if let Some(o) = payload.as_object_mut() {
        o.insert("target".into(), json!("local"));
        o.entry("params").or_insert_with(|| json!({}));
        o.entry("mission_id").or_insert(Value::Null);
    }
    if !send(st, socket, protocol::encode("command", worker_id, generation, payload)).await {
        return false;
    }
    if let Ok(Some(r)) = st.db.command_sent(&row.request_id) {
        st.hub.publish(json!({"type": "command", "command": r.to_notice()}));
    }
    true
}

/// The browser-stream change notice for one accepted batch: counts per mission, not the events.
/// A console that wants the details reads `GET /api/v1/events` (stored by the coordinator, so no
/// worker round trip) or the mission list, whose entries carry the replicated summaries.
fn event_notice(worker: &str, cursor: u64, events: &[Value], summaries: &[Value]) -> Value {
    let mut by_mission: std::collections::BTreeMap<String, (u64, u64, bool)> = std::collections::BTreeMap::new();
    for event in events {
        let mission = event.get("mission_id").and_then(Value::as_str).unwrap_or("");
        let id = event.get("id").and_then(Value::as_u64).unwrap_or(0);
        let entry = by_mission.entry(mission.to_string()).or_default();
        entry.0 += 1;
        entry.1 = entry.1.max(id);
    }
    for summary in summaries {
        if let Some(id) = summary.get("id").and_then(Value::as_str) {
            by_mission.entry(id.to_string()).or_default().2 = true;
        }
    }
    let first = events.iter().filter_map(|e| e.get("id").and_then(Value::as_u64)).min();
    let last = events.iter().filter_map(|e| e.get("id").and_then(Value::as_u64)).max();
    let missions: Vec<Value> = by_mission
        .into_iter()
        .map(|(id, (count, last_id, summary))| json!({"id": id, "events": count, "last_event_id": last_id, "summary": summary}))
        .collect();
    json!({"type": "event", "worker": worker, "cursor": cursor, "count": events.len(), "first_event_id": first, "last_event_id": last, "missions": missions})
}

/// The worker's `stale-generation` error in reply to our `welcome`.
fn is_stale_welcome_reply(m: &Message, welcome_id: &str) -> bool {
    m.kind == "error"
        && m.payload.get("code").and_then(Value::as_str) == Some("stale-generation")
        && m.payload.get("in_reply_to").and_then(Value::as_str) == Some(welcome_id)
}

/// Handle one validated message after welcome. `Err(())` means a storage failure.
fn handle(st: &St, name: &str, worker_id: &str, generation: u64, m: Message) -> Result<Vec<String>, ()> {
    let reply = |code: &str, text: &str| Ok(vec![protocol::error_message(worker_id, generation, code, text, Some(&m.id))]);
    // An `error` is never answered with an error, whatever its worker id or generation (no ping-pong).
    if m.kind == "error" {
        return Ok(vec![]);
    }
    if m.worker_id != worker_id {
        return reply("invalid-message", "worker_id differs from the connection's worker id");
    }
    if m.generation != generation {
        return reply("stale-generation", "message generation is not this connection's generation");
    }
    let db = |e: rusqlite::Error| eprintln!("database error: {e}");
    match m.kind.as_str() {
        "ping" => Ok(vec![protocol::encode("pong", worker_id, generation, json!({}))]),
        "pong" => Ok(vec![]),
        "events" => {
            let p = &m.payload;
            let (after, through) = (p["after"].as_u64().unwrap_or(0), p["through"].as_u64().unwrap_or(0));
            let events = p["events"].as_array().cloned().unwrap_or_default();
            let missions = p.get("missions").and_then(Value::as_array).cloned();
            let mission_cursor = p.get("mission_cursor").and_then(Value::as_u64);
            match st
                .db
                .events_store(EventBatch {
                    worker: name,
                    after,
                    through,
                    events: &events,
                    mission_cursor,
                    missions: missions.as_deref(),
                    max: st.cfg.max_events_per_worker,
                })
                .map_err(db)?
            {
                EventsOutcome::Accepted { cursor, stored } => {
                    if stored > 0 || missions.as_ref().is_some_and(|m| !m.is_empty()) {
                        st.hub.publish(event_notice(name, cursor, &events, missions.as_deref().unwrap_or(&[])));
                    }
                    Ok(vec![])
                }
                EventsOutcome::Nack { expected } => Ok(vec![protocol::encode(
                    "events.nack",
                    worker_id,
                    generation,
                    json!({"expected_after": expected}),
                )]),
            }
        }
        "command.ack" => {
            let id = m.payload["request_id"].as_str().unwrap_or_default();
            if let Some(row) = st.db.command_acked(name, id).map_err(db)? {
                st.hub.publish(json!({"type": "command", "command": row.to_notice()}));
            }
            Ok(vec![])
        }
        "command.result" => {
            let id = m.payload["request_id"].as_str().unwrap_or_default();
            if let Some(row) = st.db.command_done(name, id, &Value::Object(m.payload.clone())).map_err(db)? {
                st.hub.publish(json!({"type": "command", "command": row.to_notice()}));
            }
            Ok(vec![])
        }
        "terminal.status" => {
            let p = &m.payload;
            let text = |k: &str| p[k].as_str().unwrap_or_default().to_string();
            st.hub.terminal_status(
                name,
                p["session_id"].as_str().unwrap_or_default(),
                crate::hub::TermStatus {
                    state: text("state"),
                    mode: text("mode"),
                    reason: text("reason"),
                },
            );
            Ok(vec![])
        }
        other => reply("unsupported-message", &format!("the coordinator does not accept {other} here")),
    }
}
