//! In-memory registry of live worker connections and the `/stream` broadcast channel.

use axum::extract::ws::WebSocket;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::{broadcast, mpsc, oneshot, watch};

pub const MAX_WORKER_CONNECTIONS: usize = 64;
pub const OUTBOUND_QUEUE: usize = 256;
pub const STREAM_BUFFER: usize = 256;
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
    status: mpsc::UnboundedSender<TermStatus>,
}

/// A terminal session this coordinator opened. Dropping it frees the session slot.
pub struct TermReg {
    hub: Arc<Hub>,
    pub id: String,
    pub attach: oneshot::Receiver<WebSocket>,
    pub status: mpsc::UnboundedReceiver<TermStatus>,
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
        let (status_tx, status) = mpsc::unbounded_channel();
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
                let _ = entry.status.send(status);
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
        let _ = self.stream.send(Arc::from(value.to_string()));
    }
}
