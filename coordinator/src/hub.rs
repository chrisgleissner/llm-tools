//! In-memory registry of live worker connections and the `/stream` broadcast channel.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::{broadcast, mpsc, watch};

pub const MAX_WORKER_CONNECTIONS: usize = 64;
pub const OUTBOUND_QUEUE: usize = 256;
pub const STREAM_BUFFER: usize = 256;

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
    pub stream: broadcast::Sender<Arc<str>>,
    next_id: AtomicU64,
    active: AtomicUsize,
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
            stream: broadcast::channel(STREAM_BUFFER).0,
            next_id: AtomicU64::new(1),
            active: AtomicUsize::new(0),
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

    pub fn publish(&self, value: serde_json::Value) {
        let _ = self.stream.send(Arc::from(value.to_string()));
    }
}
