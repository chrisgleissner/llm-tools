//! TLS listener, connection supervision and graceful shutdown.

use crate::auth::RateLimiter;
use crate::config::Config;
use crate::db::Db;
use crate::http::{self, AppState};
use crate::hub::Hub;
use hyper::server::conn::http1;
use hyper_util::rt::{TokioIo, TokioTimer};
use hyper_util::service::TowerToHyperService;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::net::TcpListener;
use tokio::sync::{watch, Semaphore};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;

const TLS_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Handshake and header limit for connections in the worker reserve (see [`http::Tier`]).
const RESERVE_TIMEOUT: Duration = Duration::from_secs(5);
const DRAIN_TIMEOUT: Duration = Duration::from_secs(10);

pub struct RunningServer {
    pub addr: SocketAddr,
    pub state: Arc<AppState>,
    shutdown: watch::Sender<bool>,
    task: tokio::task::JoinHandle<()>,
}

impl RunningServer {
    /// Signal shutdown and wait for connections to drain (bounded by 10 s).
    pub async fn stop(self) {
        let _ = self.shutdown.send(true);
        let _ = self.task.await;
    }
}

pub async fn start(data_dir: &Path) -> Result<RunningServer, String> {
    start_with(data_dir, None).await
}

/// Like [`start`], with an explicit web directory that overrides `web_dir` in `config.toml`.
pub async fn start_with(data_dir: &Path, web_dir: Option<&Path>) -> Result<RunningServer, String> {
    let cfg = Config::load(data_dir)?;
    let bind = cfg.checked_bind()?;
    let configured = cfg.web_dir.as_ref().map(std::path::PathBuf::from);
    let web_dir = crate::web::resolve_web_dir(web_dir.map(Path::to_path_buf).or(configured).as_deref())?;
    let tls = TlsAcceptor::from(crate::certs::load_server_config(data_dir)?);
    let db = Arc::new(Db::open(&crate::db_path(data_dir)).map_err(|e| format!("cannot open database: {e}"))?);
    let cursor_key = db.cursor_key().map_err(|e| format!("cannot load pagination cursor key: {e}"))?;
    let listener = TcpListener::bind(bind).await.map_err(|e| format!("cannot bind {bind}: {e}"))?;
    let addr = listener.local_addr().map_err(|e| e.to_string())?;
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let dummy_hash = tokio::task::spawn_blocking(|| crate::auth::hash_password("dummy password for unknown users"))
        .await
        .map_err(|e| e.to_string())?;
    let state = Arc::new(AppState {
        db: db.clone(),
        hub: Hub::new(),
        cfg,
        data_dir: data_dir.to_path_buf(),
        limiter: RateLimiter::new(5, Duration::from_secs(60), 4096),
        started: Instant::now(),
        dummy_hash,
        cursor_key,
        argon_slots: Semaphore::new(2),
        big_bodies: Arc::new(Semaphore::new(http::BIG_BODY_SLOTS)),
        shutdown: shutdown_rx.clone(),
        web_dir,
    });
    let app = http::router(state.clone());
    let lock = crate::ops::lock_data_dir(data_dir)?;
    let limits = ConnLimits {
        max_connections: state.cfg.max_connections,
        per_ip: state.cfg.max_connections_per_ip,
        reserve: state.cfg.worker_reserve_connections.min(state.cfg.max_connections / 4),
        http_idle: Duration::from_secs(state.cfg.http_idle_timeout_secs),
    };
    let task = tokio::spawn(accept_loop(listener, tls, app, db, shutdown_rx, limits, lock));
    Ok(RunningServer {
        addr,
        state,
        shutdown: shutdown_tx,
        task,
    })
}

#[derive(Clone, Copy)]
struct ConnLimits {
    max_connections: usize,
    per_ip: usize,
    /// Slots (part of `max_connections`) used only when the general slots are full.
    reserve: usize,
    http_idle: Duration,
}

/// Live connection count per client address key (IPv6 by /64).
#[derive(Default)]
struct IpCounts(Mutex<HashMap<String, usize>>);

struct IpGuard {
    counts: Arc<IpCounts>,
    key: String,
}

impl IpCounts {
    fn take(self: &Arc<Self>, key: String, max: usize) -> Option<IpGuard> {
        let mut m = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let n = m.entry(key.clone()).or_insert(0);
        if *n >= max {
            return None;
        }
        *n += 1;
        Some(IpGuard { counts: self.clone(), key })
    }
}

impl Drop for IpGuard {
    fn drop(&mut self) {
        let mut m = self.counts.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(n) = m.get_mut(&self.key) {
            *n -= 1;
            if *n == 0 {
                m.remove(&self.key);
            }
        }
    }
}

async fn accept_loop(
    listener: TcpListener,
    tls: TlsAcceptor,
    app: axum::Router,
    db: Arc<Db>,
    mut shutdown: watch::Receiver<bool>,
    limits: ConnLimits,
    _lock: std::fs::File,
) {
    let slots = Arc::new(Semaphore::new(limits.max_connections - limits.reserve));
    let reserve = Arc::new(Semaphore::new(limits.reserve));
    let per_ip = Arc::new(IpCounts::default());
    let mut conns: JoinSet<()> = JoinSet::new();
    let mut maintenance = tokio::time::interval(Duration::from_secs(3600));
    loop {
        tokio::select! {
            _ = shutdown.changed() => break,
            _ = maintenance.tick() => { if let Err(e) = db.maintenance() { eprintln!("maintenance failed: {e}"); } }
            Some(_) = conns.join_next(), if !conns.is_empty() => {}
            accepted = listener.accept() => {
                let Ok((tcp, peer)) = accepted else { continue };
                // Over a cap: drop the socket at once (the peer sees a close, not a hang).
                let Some(ip_guard) = per_ip.take(crate::auth::addr_key(&peer.ip()), limits.per_ip) else { continue };
                let (permit, tier) = if let Ok(p) = slots.clone().try_acquire_owned() {
                    (p, http::Tier::General)
                } else if let Ok(p) = reserve.clone().try_acquire_owned() {
                    (p, http::Tier::Reserve)
                } else {
                    continue
                };
                let app = http::with_tier(http::with_peer(app.clone(), peer), tier);
                let (tls, mut sd) = (tls.clone(), shutdown.clone());
                let (handshake, header) = if tier == http::Tier::Reserve {
                    (RESERVE_TIMEOUT, limits.http_idle.min(RESERVE_TIMEOUT))
                } else {
                    (TLS_HANDSHAKE_TIMEOUT, limits.http_idle)
                };
                conns.spawn(async move {
                    let _held = (permit, ip_guard);
                    let Ok(Ok(stream)) = tokio::time::timeout(handshake, tls.accept(tcp)).await else { return };
                    let mut builder = http1::Builder::new();
                    builder.timer(TokioTimer::new()).header_read_timeout(header);
                    let conn = builder.serve_connection(TokioIo::new(stream), TowerToHyperService::new(app)).with_upgrades();
                    tokio::pin!(conn);
                    tokio::select! {
                        _ = conn.as_mut() => return,
                        _ = sd.changed() => conn.as_mut().graceful_shutdown(),
                    }
                    let _ = conn.await;
                });
            }
        }
    }
    drop(listener);
    // Worker and stream sockets watch the same shutdown signal and close themselves.
    let drain = async { while conns.join_next().await.is_some() {} };
    if tokio::time::timeout(DRAIN_TIMEOUT, drain).await.is_err() {
        conns.abort_all();
    }
}
