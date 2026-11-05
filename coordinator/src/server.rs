//! TLS listener, connection supervision and graceful shutdown.

use crate::auth::RateLimiter;
use crate::config::Config;
use crate::db::Db;
use crate::http::{self, AppState};
use crate::hub::Hub;
use hyper::server::conn::http1;
use hyper_util::rt::{TokioIo, TokioTimer};
use hyper_util::service::TowerToHyperService;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::net::TcpListener;
use tokio::sync::{watch, Semaphore};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;

const MAX_CONNECTIONS: usize = 256;
const TLS_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const HEADER_TIMEOUT: Duration = Duration::from_secs(15);
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
        argon_slots: Semaphore::new(2),
        shutdown: shutdown_rx.clone(),
        web_dir,
    });
    let app = http::router(state.clone());
    let task = tokio::spawn(accept_loop(listener, tls, app, db, shutdown_rx));
    Ok(RunningServer {
        addr,
        state,
        shutdown: shutdown_tx,
        task,
    })
}

async fn accept_loop(listener: TcpListener, tls: TlsAcceptor, app: axum::Router, db: Arc<Db>, mut shutdown: watch::Receiver<bool>) {
    let slots = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    let mut conns: JoinSet<()> = JoinSet::new();
    let mut maintenance = tokio::time::interval(Duration::from_secs(3600));
    loop {
        tokio::select! {
            _ = shutdown.changed() => break,
            _ = maintenance.tick() => { if let Err(e) = db.maintenance() { eprintln!("maintenance failed: {e}"); } }
            Some(_) = conns.join_next(), if !conns.is_empty() => {}
            accepted = listener.accept() => {
                let Ok((tcp, peer)) = accepted else { continue };
                let Ok(permit) = slots.clone().try_acquire_owned() else { continue };
                let (tls, app, mut sd) = (tls.clone(), http::with_peer(app.clone(), peer), shutdown.clone());
                conns.spawn(async move {
                    let _permit = permit;
                    let Ok(Ok(stream)) = tokio::time::timeout(TLS_HANDSHAKE_TIMEOUT, tls.accept(tcp)).await else { return };
                    let mut builder = http1::Builder::new();
                    builder.timer(TokioTimer::new()).header_read_timeout(HEADER_TIMEOUT);
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
