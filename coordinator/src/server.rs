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
    pub listeners: Vec<(crate::config::ListenerGroup, SocketAddr)>,
    pub state: Arc<AppState>,
    shutdown: watch::Sender<bool>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
    _data_dir_lock: std::fs::File,
}

impl RunningServer {
    /// Signal shutdown and wait for connections to drain (bounded by 10 s).
    pub async fn stop(self) {
        let RunningServer {
            shutdown,
            tasks,
            _data_dir_lock: data_dir_lock,
            ..
        } = self;
        let _ = shutdown.send(true);
        for task in tasks {
            let _ = task.await;
        }
        // Keep the data-directory lock through listener shutdown, then release it before
        // returning so an immediate restore cannot race with server teardown.
        drop(data_dir_lock);
    }
}

pub async fn start(data_dir: &Path) -> Result<RunningServer, String> {
    start_with(data_dir, None).await
}

/// Like [`start`], with an explicit web directory that overrides `web_dir` in `config.toml`.
pub async fn start_with(data_dir: &Path, web_dir: Option<&Path>) -> Result<RunningServer, String> {
    let cfg = Config::load(data_dir)?;
    let binds = cfg.listener_binds()?;
    let configured = cfg.web_dir.as_ref().map(std::path::PathBuf::from);
    let web_dir = crate::web::resolve_web_dir(web_dir.map(Path::to_path_buf).or(configured).as_deref())?;
    let cert_provider: Box<dyn crate::certs::CertificateProvider> = match (&cfg.tls_cert_file, &cfg.tls_key_file, &cfg.tls_cert_name) {
        (Some(cert), Some(key), Some(name)) => Box::new(crate::certs::FileCertificateProvider {
            cert_path: cert.into(),
            key_path: key.into(),
            exact_name: name.clone(),
        }),
        _ => Box::new(crate::certs::InternalCaProvider),
    };
    let (tls_config, file_cert_resolver) = cert_provider.load(data_dir, &cfg.min_tls)?;
    let worker_mtls = cfg.worker_mtls;
    let min_tls = cfg.min_tls.clone();
    let worker_ca_pem = if worker_mtls {
        Some(std::fs::read(data_dir.join("ca.pem")).map_err(|e| format!("cannot read coordinator CA for worker mTLS: {e}"))?)
    } else {
        None
    };
    let db = Arc::new(Db::open(&crate::db_path(data_dir)).map_err(|e| format!("cannot open database: {e}"))?);
    validate_internet_runtime(&cfg, &db)?;
    if let Some((principal, _)) = db.admin_get().map_err(|e| format!("cannot read coordinator administrator: {e}"))? {
        let passkey_count = db.passkey_count(&principal).map_err(|e| format!("cannot count passkeys: {e}"))?;
        if let Some(configured_origin) = cfg.public_origin.as_deref() {
            let stored_origin = db
                .passkey_origin(&principal)
                .map_err(|e| format!("cannot read canonical passkey origin: {e}"))?;
            match stored_origin {
                Some(stored) if stored != configured_origin => {
                    return Err(format!(
                        "public_origin cannot change while passkeys are enrolled (stored {stored:?}, configured {configured_origin:?}); remove and re-enrol the passkeys first"
                    ));
                }
                None if passkey_count > 0
                    && !db
                        .passkey_origin_bind(&principal, configured_origin)
                        .map_err(|e| format!("cannot bind canonical passkey origin: {e}"))? =>
                {
                    return Err("public_origin changed while binding existing passkeys".into());
                }
                _ => {}
            }
        } else if passkey_count > 0
            && db
                .passkey_origin(&principal)
                .map_err(|e| format!("cannot read canonical passkey origin: {e}"))?
                .is_none()
        {
            return Err(
                "existing passkeys have no recorded canonical origin; configure public_origin explicitly before starting, then re-enrol passkeys if needed"
                    .into(),
            );
        }
    }
    let cursor_key = db.cursor_key().map_err(|e| format!("cannot load pagination cursor key: {e}"))?;
    let mut listeners = Vec::with_capacity(binds.len());
    for (group, bind) in binds {
        let listener = TcpListener::bind(bind).await.map_err(|e| format!("cannot bind {bind}: {e}"))?;
        let addr = listener.local_addr().map_err(|e| e.to_string())?;
        listeners.push((group, addr, listener));
    }
    let addr = listeners
        .iter()
        .find(|(group, _, _)| *group == crate::config::ListenerGroup::ConsoleRest)
        .or_else(|| listeners.first())
        .map(|(_, addr, _)| *addr)
        .ok_or("at least one listener is required")?;
    let public_listeners = listeners.iter().map(|(group, addr, _)| (*group, *addr)).collect();
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    if let Some(resolver) = file_cert_resolver {
        let mut shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(1));
            let mut last_error: Option<String> = None;
            interval.tick().await;
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        match resolver.reload_if_changed() {
                            Err(error) if last_error.as_ref() != Some(&error) => {
                                eprintln!("TLS file certificate reload deferred: {error}");
                                last_error = Some(error);
                            }
                            Ok(_) => last_error = None,
                            _ => {}
                        }
                    }
                    changed = shutdown.changed() => {
                        if changed.is_err() || *shutdown.borrow() { break; }
                    }
                }
            }
        });
    }
    let dummy_hash = tokio::task::spawn_blocking(|| crate::auth::hash_password("dummy password for unknown users"))
        .await
        .map_err(|e| e.to_string())?;
    let state = Arc::new(AppState {
        db: db.clone(),
        hub: Hub::new(),
        cfg,
        data_dir: data_dir.to_path_buf(),
        limiter: RateLimiter::new(5, Duration::from_secs(60), 4096),
        mutation_limiter: crate::auth::MutationLimiter::new(Duration::from_secs(60), 4096),
        started: Instant::now(),
        dummy_hash,
        cursor_key,
        argon_slots: Semaphore::new(2),
        big_bodies: Arc::new(Semaphore::new(http::BIG_BODY_SLOTS)),
        shutdown: shutdown_rx.clone(),
        web_dir,
    });
    let lock = crate::ops::lock_data_dir(data_dir)?;
    let limits = ConnLimits {
        max_connections: state.cfg.max_connections,
        per_ip: state.cfg.max_connections_per_ip,
        reserve: state.cfg.worker_reserve_connections.min(state.cfg.max_connections / 4),
        http_idle: Duration::from_secs(state.cfg.http_idle_timeout_secs),
    };
    let budget = Arc::new(ConnectionBudget::new(&limits));
    let mut tasks = Vec::with_capacity(listeners.len());
    for (index, (group, _, listener)) in listeners.into_iter().enumerate() {
        let app = http::router_for_group(state.clone(), group);
        let listener_tls = if worker_mtls && matches!(group, crate::config::ListenerGroup::WorkerChannel | crate::config::ListenerGroup::Combined) {
            let ca_pem = worker_ca_pem.as_deref().expect("CA is loaded when worker mTLS is enabled");
            let required = group == crate::config::ListenerGroup::WorkerChannel;
            TlsAcceptor::from(Arc::new(crate::certs::server_config_with_client_auth(
                tls_config.cert_resolver.clone(),
                ca_pem,
                &min_tls,
                required,
            )?))
        } else {
            TlsAcceptor::from(tls_config.clone())
        };
        tasks.push(tokio::spawn(accept_loop(ListenerTask {
            listener,
            tls: listener_tls,
            app,
            db: db.clone(),
            shutdown: shutdown_rx.clone(),
            limits,
            budget: budget.clone(),
            maintenance_owner: index == 0,
        })));
    }
    Ok(RunningServer {
        addr,
        listeners: public_listeners,
        state,
        shutdown: shutdown_tx,
        tasks,
        _data_dir_lock: lock,
    })
}

fn validate_internet_runtime(cfg: &Config, db: &Db) -> Result<(), String> {
    if cfg.exposure != crate::config::Exposure::Internet {
        return Ok(());
    }
    let Some((principal, _)) = db.admin_get().map_err(|e| format!("cannot read administrator for Internet exposure: {e}"))? else {
        return Err("internet exposure refused: enroll a phishing-resistant passkey before enabling Internet exposure".into());
    };
    if db
        .passkey_count(&principal)
        .map_err(|e| format!("cannot read passkey enrollment for Internet exposure: {e}"))?
        == 0
    {
        return Err("internet exposure refused: enroll a phishing-resistant passkey before enabling Internet exposure".into());
    }
    if db
        .non_expiring_api_token_count()
        .map_err(|e| format!("cannot check API token expiry for Internet exposure: {e}"))?
        > 0
    {
        return Err("internet exposure refused: revoke or rotate every non-expiring API token".into());
    }
    Ok(())
}

#[cfg(test)]
mod internet_exposure_tests {
    use super::*;
    use crate::db::PasskeyCredentialRow;

    fn temp_db() -> (std::path::PathBuf, Db) {
        let nonce = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let dir = std::env::temp_dir().join(format!("bridge-internet-exposure-{}-{nonce}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = Db::open(&dir.join("coordinator.db")).unwrap();
        (dir, db)
    }

    #[test]
    fn internet_exposure_requires_enrolled_passkey_and_expiring_tokens() {
        let cfg = Config {
            exposure: crate::config::Exposure::Internet,
            ..Config::default()
        };
        let (dir, db) = temp_db();
        db.admin_create("admin", "hash").unwrap();
        assert!(validate_internet_runtime(&cfg, &db).unwrap_err().contains("phishing-resistant passkey"));

        db.passkey_put_at_origin(
            &PasskeyCredentialRow {
                credential_id: "credential".into(),
                principal_id: "admin".into(),
                credential: "stored-passkey".into(),
                user_name: "admin".into(),
                created_at: crate::now(),
                last_used: None,
                aaguid: None,
                transports: "[]".into(),
                backup_eligible: false,
                backed_up: false,
                sign_count: 0,
            },
            "https://bridge.example.test",
        )
        .unwrap();
        assert!(validate_internet_runtime(&cfg, &db).is_ok());

        db.token_create_identified("unbounded", "token-id", "secret-hash", None, &["read".into()])
            .unwrap();
        assert!(validate_internet_runtime(&cfg, &db).unwrap_err().contains("non-expiring API token"));

        db.token_revoke("unbounded").unwrap();
        db.token_create_identified("bounded", "token-id-2", "secret-hash-2", Some(crate::now() + 86_400), &["read".into()])
            .unwrap();
        assert!(validate_internet_runtime(&cfg, &db).is_ok());
        drop(db);
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[derive(Clone, Copy)]
struct ConnLimits {
    max_connections: usize,
    per_ip: usize,
    /// Slots (part of `max_connections`) used only when the general slots are full.
    reserve: usize,
    http_idle: Duration,
}

struct ConnectionBudget {
    slots: Arc<Semaphore>,
    reserve: Arc<Semaphore>,
    per_ip: Arc<IpCounts>,
}

struct ListenerTask {
    listener: TcpListener,
    tls: TlsAcceptor,
    app: axum::Router,
    db: Arc<Db>,
    shutdown: watch::Receiver<bool>,
    limits: ConnLimits,
    budget: Arc<ConnectionBudget>,
    maintenance_owner: bool,
}

impl ConnectionBudget {
    fn new(limits: &ConnLimits) -> Self {
        Self {
            slots: Arc::new(Semaphore::new(limits.max_connections - limits.reserve)),
            reserve: Arc::new(Semaphore::new(limits.reserve)),
            per_ip: Arc::new(IpCounts::default()),
        }
    }
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

async fn accept_loop(task: ListenerTask) {
    let ListenerTask {
        listener,
        tls,
        app,
        db,
        mut shutdown,
        limits,
        budget,
        maintenance_owner,
    } = task;
    let mut conns: JoinSet<()> = JoinSet::new();
    let mut maintenance = tokio::time::interval(Duration::from_secs(3600));
    loop {
        tokio::select! {
            _ = shutdown.changed() => break,
            _ = maintenance.tick(), if maintenance_owner => { if let Err(e) = db.maintenance() { eprintln!("maintenance failed: {e}"); } }
            Some(_) = conns.join_next(), if !conns.is_empty() => {}
            accepted = listener.accept() => {
                let Ok((tcp, peer)) = accepted else { continue };
                // Over a cap: drop the socket at once (the peer sees a close, not a hang).
                let Some(ip_guard) = budget.per_ip.take(crate::auth::addr_key(&peer.ip()), limits.per_ip) else { continue };
                let (permit, tier) = if let Ok(p) = budget.slots.clone().try_acquire_owned() {
                    (p, http::Tier::General)
                } else if let Ok(p) = budget.reserve.clone().try_acquire_owned() {
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
                    let client_fingerprint = stream
                        .get_ref()
                        .1
                        .peer_certificates()
                        .and_then(|certs| certs.first())
                        .map(|cert| {
                            use sha2::Digest;
                            hex::encode(sha2::Sha256::digest(cert.as_ref()))
                        });
                    let app = http::with_worker_certificate(app, client_fingerprint);
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
