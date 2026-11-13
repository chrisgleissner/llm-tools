//! In-memory registry of live worker connections and the `/stream` broadcast channel.

use axum::extract::ws::WebSocket;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::{broadcast, mpsc, oneshot, watch};

pub const MAX_WORKER_CONNECTIONS: usize = 64;
pub const OUTBOUND_QUEUE: usize = 256;
pub const STREAM_BUFFER: usize = 32;
/// Browser fan-out is advisory. Keep each retained message tiny and never duplicate event/result bodies.
pub const MAX_STREAM_MESSAGE_BYTES: usize = 4 * 1024;
/// Open browser `/stream` sockets. An upgraded WebSocket no longer holds a TCP connection slot, so this is the only
/// bound on them. Measured on x86-64 (docs/distributed/capacity-measurements): about 0.19 MiB per client.
pub const MAX_STREAM_CLIENTS: usize = 128;
/// Open `/stream` sockets of one login session or API token (several tabs of one user).
pub const MAX_STREAM_CLIENTS_PER_SESSION: usize = 16;
pub const MAX_TERMINALS_PER_WORKER: usize = 16;
pub const MAX_TERMINALS: usize = 64;

/// Messages and bytes of one (direction, message type) pair.
type LinkCounts = BTreeMap<(&'static str, String), (u64, u64)>;

/// A `terminal.status` payload relayed from the worker's control connection.
pub struct TermStatus {
    pub state: String,
    pub mode: String,
    pub reason: String,
}

struct TermEntry {
    worker: String,
    attach: Option<oneshot::Sender<WebSocket>>,
    status: mpsc::Sender<TermStatus>,
}

/// A terminal session this coordinator opened. Dropping it frees the session slot.
pub struct TermReg {
    hub: Arc<Hub>,
    pub id: String,
    pub attach: oneshot::Receiver<WebSocket>,
    pub status: mpsc::Receiver<TermStatus>,
}

impl TermReg {
    /// Count a browser input frame dropped because the worker-side queue was full (diagnostics only).
    pub fn dropped_input(&self) {
        self.hub.dropped_terminal_input.fetch_add(1, Ordering::Relaxed);
    }
}

impl Drop for TermReg {
    fn drop(&mut self) {
        self.hub.terms().remove(&self.id);
    }
}

pub type CloseSignal = Option<(u16, &'static str)>;

pub struct Outbound {
    pub text: String,
    pub command_id: Option<String>,
}

pub struct ConnHandle {
    pub conn_id: u64,
    pub generation: u64,
    pub worker_id: String,
    pub tx: mpsc::Sender<Outbound>,
    pub close: watch::Sender<CloseSignal>,
}

pub enum SendResult {
    Queued,
    Offline,
    /// The queue was full; the connection has been told to close.
    Full,
}

pub struct Hub {
    conns: Mutex<HashMap<String, ConnHandle>>,
    terms: Mutex<HashMap<String, TermEntry>>,
    pub stream: broadcast::Sender<Arc<str>>,
    next_id: AtomicU64,
    active: AtomicUsize,
    /// Worker names with a connection that has not sent its hello yet.
    pending: Mutex<HashSet<String>>,
    /// Messages and bytes since start, by direction and message type (diagnostics only; not persisted).
    link: Mutex<LinkCounts>,
    /// Open `/stream` browser sockets.
    stream_clients: AtomicUsize,
    /// Open `/stream` sockets per login session or API token.
    stream_by: Mutex<HashMap<String, usize>>,
    /// Browser terminal input frames dropped since start because the per-session byte budget was full.
    dropped_terminal_input: AtomicU64,
}

/// Counts one open `/stream` socket for diagnostics.
pub struct StreamGuard(Arc<Hub>, String);

impl Drop for StreamGuard {
    fn drop(&mut self) {
        self.0.stream_clients.fetch_sub(1, Ordering::SeqCst);
        let mut by = self.0.stream_by.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(n) = by.get_mut(&self.1) {
            *n -= 1;
            if *n == 0 {
                by.remove(&self.1);
            }
        }
    }
}

/// Marks one worker token as having a connection waiting for its hello (at most one at a time).
pub struct PendingGuard {
    hub: Arc<Hub>,
    name: String,
}

impl Drop for PendingGuard {
    fn drop(&mut self) {
        self.hub.pending.lock().unwrap_or_else(|e| e.into_inner()).remove(&self.name);
    }
}

/// Counts a worker WebSocket (also before hello) against `MAX_WORKER_CONNECTIONS`.
pub struct ConnGuard(Arc<Hub>);

impl Drop for ConnGuard {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

impl Hub {
    pub fn new() -> Arc<Hub> {
        Arc::new(Hub {
            conns: Mutex::new(HashMap::new()),
            terms: Mutex::new(HashMap::new()),
            stream: broadcast::channel(STREAM_BUFFER).0,
            next_id: AtomicU64::new(1),
            active: AtomicUsize::new(0),
            pending: Mutex::new(HashSet::new()),
            link: Mutex::new(BTreeMap::new()),
            stream_clients: AtomicUsize::new(0),
            stream_by: Mutex::new(HashMap::new()),
            dropped_terminal_input: AtomicU64::new(0),
        })
    }

    /// Claim one `/stream` slot for the session or token `key`; `Err` carries the refusal text.
    pub fn try_stream_open(self: &Arc<Self>, key: &str) -> Result<StreamGuard, String> {
        let mut by = self.stream_by.lock().unwrap_or_else(|e| e.into_inner());
        if by.get(key).copied().unwrap_or(0) >= MAX_STREAM_CLIENTS_PER_SESSION {
            return Err(format!(
                "this session already has {MAX_STREAM_CLIENTS_PER_SESSION} browser streams open; close a tab and retry"
            ));
        }
        if self.stream_clients.fetch_add(1, Ordering::SeqCst) >= MAX_STREAM_CLIENTS {
            self.stream_clients.fetch_sub(1, Ordering::SeqCst);
            return Err(format!(
                "the coordinator already serves {MAX_STREAM_CLIENTS} browser streams; close one and retry"
            ));
        }
        *by.entry(key.to_string()).or_default() += 1;
        Ok(StreamGuard(self.clone(), key.to_string()))
    }

    /// Resource counters for `GET /api/v1/diagnostics` (capacity plan F9). All are cheap reads.
    pub fn resource_counts(&self) -> serde_json::Value {
        let (depth_max, depth_total) = self.map().values().fold((0usize, 0usize), |(max, total), h| {
            let depth = OUTBOUND_QUEUE - h.tx.capacity().min(OUTBOUND_QUEUE);
            (max.max(depth), total + depth)
        });
        serde_json::json!({
            "worker_connections": self.active.load(Ordering::SeqCst),
            "outbound_queue_depth_max": depth_max,
            "outbound_queue_depth_total": depth_total,
            "stream_clients": self.stream_clients.load(Ordering::SeqCst),
            "terminal_sessions": self.terminal_count(),
            "terminal_input_frames_dropped": self.dropped_terminal_input.load(Ordering::Relaxed),
        })
    }

    /// Count one message of `text.len()` bytes. `direction` is `worker_in`, `worker_out` or
    /// `browser_out`; the type is read from the message's `type` field.
    pub fn count_message(&self, direction: &'static str, text: &str) {
        #[derive(serde::Deserialize)]
        struct Kind<'a> {
            #[serde(borrow, rename = "type")]
            kind: std::borrow::Cow<'a, str>,
        }
        let kind = serde_json::from_str::<Kind>(text).map_or_else(|_| "unparsed".to_string(), |k| k.kind.into_owned());
        let mut link = self.link.lock().unwrap_or_else(|e| e.into_inner());
        let entry = link.entry((direction, kind)).or_default();
        entry.0 += 1;
        entry.1 += text.len() as u64;
    }

    /// `{direction: {type: {messages, bytes}}}` for `GET /api/v1/diagnostics`.
    pub fn link_bytes(&self) -> serde_json::Value {
        let link = self.link.lock().unwrap_or_else(|e| e.into_inner());
        let mut out = serde_json::Map::new();
        for ((direction, kind), (messages, bytes)) in link.iter() {
            out.entry(direction.to_string())
                .or_insert_with(|| serde_json::json!({}))
                .as_object_mut()
                .map(|o| o.insert(kind.clone(), serde_json::json!({"messages": messages, "bytes": bytes})));
        }
        serde_json::Value::Object(out)
    }

    /// Claim the single pre-hello slot of worker `name`; `None` when one is already waiting.
    pub fn try_pending(self: &Arc<Self>, name: &str) -> Option<PendingGuard> {
        if !self.pending.lock().unwrap_or_else(|e| e.into_inner()).insert(name.to_string()) {
            return None;
        }
        Some(PendingGuard {
            hub: self.clone(),
            name: name.to_string(),
        })
    }

    pub fn try_reserve(self: &Arc<Self>) -> Option<ConnGuard> {
        if self.active.fetch_add(1, Ordering::SeqCst) >= MAX_WORKER_CONNECTIONS {
            self.active.fetch_sub(1, Ordering::SeqCst);
            return None;
        }
        Some(ConnGuard(self.clone()))
    }

    pub fn next_conn_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::SeqCst)
    }

    fn map(&self) -> std::sync::MutexGuard<'_, HashMap<String, ConnHandle>> {
        self.conns.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Register the new connection and tell any older one of the same worker to close.
    pub fn register(&self, name: &str, handle: ConnHandle) {
        if let Some(old) = self.map().insert(name.to_string(), handle) {
            let _ = old.close.send(Some((1000, "superseded by a newer connection")));
        }
    }

    /// Remove the entry only if it still belongs to `conn_id`; true when it did.
    pub fn unregister(&self, name: &str, conn_id: u64) -> bool {
        let mut m = self.map();
        if m.get(name).is_some_and(|h| h.conn_id == conn_id) {
            m.remove(name);
            true
        } else {
            false
        }
    }

    pub fn online(&self, name: &str) -> bool {
        self.map().contains_key(name)
    }

    pub fn connected(&self) -> Vec<String> {
        let mut v: Vec<String> = self.map().keys().cloned().collect();
        v.sort();
        v
    }

    pub fn close(&self, name: &str, code: u16, reason: &'static str) {
        if let Some(h) = self.map().get(name) {
            let _ = h.close.send(Some((code, reason)));
        }
    }

    pub fn close_all(&self, code: u16, reason: &'static str) {
        for h in self.map().values() {
            let _ = h.close.send(Some((code, reason)));
        }
    }

    /// Build a `command` envelope with the live connection's generation and queue it.
    pub fn send_command(&self, name: &str, payload: serde_json::Value, command_id: &str) -> SendResult {
        let m = self.map();
        let Some(h) = m.get(name) else { return SendResult::Offline };
        let text = crate::protocol::encode("command", &h.worker_id, h.generation, payload);
        match h.tx.try_send(Outbound {
            text,
            command_id: Some(command_id.to_string()),
        }) {
            Ok(()) => SendResult::Queued,
            Err(_) => {
                let _ = h.close.send(Some((1013, "outbound queue full")));
                SendResult::Full
            }
        }
    }

    fn terms(&self) -> std::sync::MutexGuard<'_, HashMap<String, TermEntry>> {
        self.terms.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Reserve a terminal session slot for `worker`; `Err` carries the refusal reason.
    pub fn terminal_register(self: &Arc<Self>, worker: &str) -> Result<TermReg, &'static str> {
        let mut terms = self.terms();
        if terms.len() >= MAX_TERMINALS {
            return Err("too many terminal sessions on this coordinator");
        }
        if terms.values().filter(|t| t.worker == worker).count() >= MAX_TERMINALS_PER_WORKER {
            return Err("too many terminal sessions for this worker");
        }
        let id = loop {
            let id = crate::protocol::new_id();
            if !terms.contains_key(&id) {
                break id;
            }
        };
        let (attach_tx, attach) = oneshot::channel();
        let (status_tx, status) = mpsc::channel(16);
        terms.insert(
            id.clone(),
            TermEntry {
                worker: worker.to_string(),
                attach: Some(attach_tx),
                status: status_tx,
            },
        );
        Ok(TermReg {
            hub: self.clone(),
            id,
            attach,
            status,
        })
    }

    /// Claim the attach slot of a session opened for `worker`; only the first claim succeeds.
    pub fn terminal_claim(&self, session_id: &str, worker: &str) -> Option<oneshot::Sender<WebSocket>> {
        let mut terms = self.terms();
        let entry = terms.get_mut(session_id)?;
        if entry.worker != worker {
            return None;
        }
        entry.attach.take()
    }

    /// Relay a worker's `terminal.status` to the session's viewer; ignored for a session of another worker.
    pub fn terminal_status(&self, worker: &str, session_id: &str, status: TermStatus) {
        if let Some(entry) = self.terms().get(session_id) {
            if entry.worker == worker {
                let _ = entry.status.try_send(status);
            }
        }
    }

    pub fn terminal_count(&self) -> usize {
        self.terms().len()
    }

    /// Queue a non-command envelope (for example `terminal.open`) on the worker's control connection.
    pub fn send_to_worker(&self, name: &str, kind: &str, payload: serde_json::Value) -> SendResult {
        let m = self.map();
        let Some(h) = m.get(name) else { return SendResult::Offline };
        let text = crate::protocol::encode(kind, &h.worker_id, h.generation, payload);
        match h.tx.try_send(Outbound { text, command_id: None }) {
            Ok(()) => SendResult::Queued,
            Err(_) => {
                let _ = h.close.send(Some((1013, "outbound queue full")));
                SendResult::Full
            }
        }
    }

    pub fn publish(&self, value: serde_json::Value) {
        let mut value = value;
        // Live notifications tell the SPA what changed; detail is fetched from the bounded API.
        // Retaining an event batch or command result here multiplies its size by the ring and pins
        // it for every browser subscriber, which used to let one large payload consume hundreds
        // of MiB. Keep this defence in depth even if a caller accidentally passes a full row.
        if let Some(object) = value.as_object_mut() {
            match object.get("type").and_then(serde_json::Value::as_str) {
                Some("event") => {
                    // `missions` is the per-mission count list of a change notice (a few dozen bytes each,
                    // capped by the caller); the event bodies and replicated summaries never travel here.
                    object.remove("events");
                }
                Some("command") => {
                    if let Some(command) = object.get_mut("command").and_then(serde_json::Value::as_object_mut) {
                        command.remove("request");
                        command.remove("result");
                    }
                }
                _ => {}
            }
        }
        let text = value.to_string();
        if text.len() <= MAX_STREAM_MESSAGE_BYTES {
            let _ = self.stream.send(Arc::from(text));
        }
    }
}
