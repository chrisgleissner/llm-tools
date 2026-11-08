//! Operator commands: info, health, backup, restore, setup-token and the data directory lock.

use crate::config::Config;
use crate::db::{self, Db};
use crate::{auth, db_path, netinfo, write_replace, SETUP_TOKEN_FILE};
use rusqlite::{backup::Backup, Connection, OpenFlags};
use rustls::pki_types::pem::PemObject;
use sha2::{Digest, Sha256};
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const LOCK_FILE: &str = "coordinator.lock";

/// Exclusive advisory lock held for the lifetime of `serve`. The kernel releases it when the
/// process exits, so a crash never leaves a stale lock. `restore` takes the same lock.
pub fn lock_data_dir(dir: &Path) -> Result<std::fs::File, String> {
    let f = crate::private_options(0o600)
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(LOCK_FILE))
        .map_err(|e| format!("cannot open {}: {e}", dir.join(LOCK_FILE).display()))?;
    // A `flock` lock belongs to the open file description, and a child process forked by the
    // holder shares that description until it calls exec (which closes the O_CLOEXEC lock
    // file). A process that spawns children while it drops the lock, such as the test harness
    // or a coordinator that is shutting down, therefore keeps the lock for a moment after the
    // drop. Retrying for a bounded time covers that window; a real second coordinator still
    // holds the lock after the deadline and is reported.
    let deadline = std::time::Instant::now() + LOCK_WAIT;
    loop {
        match f.try_lock() {
            Ok(()) => return Ok(f),
            Err(_) if std::time::Instant::now() < deadline => std::thread::sleep(Duration::from_millis(5)),
            Err(_) => return Err(format!("another bridge-coordinator is using {} (lock file {LOCK_FILE})", dir.display())),
        }
    }
}

/// How long [`lock_data_dir`] waits for a lock that a just-forked child may still share.
pub const LOCK_WAIT: Duration = Duration::from_secs(1);

/// SHA-256 of the first certificate in `ca.pem`, as colon-separated upper-case hex.
pub fn ca_fingerprint(dir: &Path) -> Result<String, String> {
    let pem = std::fs::read(dir.join("ca.pem")).map_err(|e| format!("cannot read ca.pem: {e}"))?;
    let der = rustls::pki_types::CertificateDer::pem_slice_iter(&pem)
        .next()
        .ok_or("ca.pem contains no certificate")?
        .map_err(|e| e.to_string())?;
    let h = Sha256::digest(der.as_ref());
    Ok(h.iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(":"))
}

/// True when `host` (a DNS name or IP literal) appears in the DER of the server certificate.
/// A byte search over the DER is enough to catch a certificate that predates a rename or an
/// address change; it is not a validation (clients do that).
fn cert_covers(dir: &Path, host: &str) -> bool {
    let Ok(pem) = std::fs::read(dir.join("server.pem")) else { return false };
    let Some(Ok(der)) = rustls::pki_types::CertificateDer::pem_slice_iter(&pem).next() else {
        return false;
    };
    let needle: Vec<u8> = match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(a)) => a.octets().to_vec(),
        Ok(IpAddr::V6(a)) => a.octets().to_vec(),
        Err(_) => host.as_bytes().to_vec(),
    };
    der.as_ref().windows(needle.len()).any(|w| w == needle.as_slice())
}

/// True when the container image set `BRIDGE_COORDINATOR_CONTAINER`.
pub fn in_container() -> bool {
    std::env::var_os(crate::config::ENV_CONTAINER).is_some_and(|v| !v.is_empty())
}

/// HTTPS URLs for the configured bind address, each with whether the certificate covers it.
pub fn urls(dir: &Path, cfg: &Config) -> Result<Vec<(String, bool)>, String> {
    let listeners = cfg.listener_binds()?;
    let Some((_, addr)) = listeners
        .iter()
        .find(|(group, _)| matches!(group, crate::config::ListenerGroup::Combined | crate::config::ListenerGroup::ConsoleRest))
    else {
        return Ok(vec![]);
    };
    let port = std::env::var(crate::config::ENV_PUBLIC_PORT)
        .ok()
        .and_then(|p| p.trim().parse::<u16>().ok())
        .unwrap_or(addr.port());
    let fmt = |h: &str| {
        if h.contains(':') {
            format!("https://[{h}]:{port}/")
        } else {
            format!("https://{h}:{port}/")
        }
    };
    let hosts: Vec<String> = if let Some(s) = netinfo::public_sans() {
        s.hostnames.into_iter().chain(s.ips.iter().map(|i| i.to_string())).collect()
    } else if in_container() {
        // Names discovered inside a container (a random host name that changes with every
        // `docker compose run`, a bridge-network address) are not reachable names.
        vec!["localhost".into()]
    } else if addr.ip().is_unspecified() {
        let s = netinfo::discovered_sans();
        s.hostnames.into_iter().chain(s.ips.iter().map(|i| i.to_string())).collect()
    } else if addr.ip().is_loopback() {
        vec!["localhost".into()]
    } else {
        vec![addr.ip().to_string()]
    };
    Ok(hosts.iter().map(|h| (fmt(h), cert_covers(dir, h) || h == "localhost")).collect())
}

pub const TRUST_STEPS: &str = "\
Trust the CA once on every browser device (import it as a certification authority;
never click through a certificate warning):
  Chrome/Edge: Settings > Privacy and security > Security > Manage certificates >
               Authorities > Import ca.pem (Windows: Trusted Root Certification
               Authorities; macOS: Keychain Access, set 'Always Trust').
  Firefox:     Settings > Privacy & Security > Certificates > View Certificates >
               Authorities > Import ca.pem, tick 'Trust this CA to identify websites'.
  Android:     Settings > Security > Encryption & credentials > Install a certificate >
               CA certificate (copy ca.pem to the phone first).
Compare the SHA-256 fingerprint shown by the browser with the one printed above.
Details: docs/distributed/install.md";

/// The report printed by `init` and `info`.
pub fn report(dir: &Path, with_trust_steps: bool) -> Result<String, String> {
    let cfg = Config::load(dir)?;
    let mut out = String::new();
    let admin_exists = Db::open(&db_path(dir))
        .map_err(|e| e.to_string())?
        .admin_get()
        .map_err(|e| e.to_string())?
        .is_some();
    out += "Coordinator address(es):\n";
    for (u, covered) in urls(dir, &cfg)? {
        out += &format!(
            "  {u}{}\n",
            if covered {
                ""
            } else {
                "   (not in the server certificate: run `cert renew`; if renew refuses the name, it is\n\
                 \x20    outside the CA's name constraints and needs `cert new-ca`)"
            }
        );
    }
    if in_container() && netinfo::public_sans().is_none() {
        out += "  Other devices need BRIDGE_PUBLIC_NAMES (container/compose.yaml) set before `init`,\n  \
                or `cert new-ca` after setting it.\n";
    }
    let listeners = cfg.listener_binds()?;
    if listeners.len() == 1 && listeners[0].0 == crate::config::ListenerGroup::Combined {
        out += &format!("Bind address:      {}\n", cfg.bind);
    } else {
        out += "Listener groups:\n";
        for (group, address) in &listeners {
            let name = match group {
                crate::config::ListenerGroup::Combined => "combined",
                crate::config::ListenerGroup::ConsoleRest => "console and REST API",
                crate::config::ListenerGroup::WorkerChannel => "worker channel",
                crate::config::ListenerGroup::Health => "health",
            };
            out += &format!("  {name}: {address}\n");
        }
    }
    out += &format!(
        "LAN access:        {}\n",
        if in_container() {
            "decided by the container's published port (BRIDGE_PUBLISH_ADDRESS in compose.yaml)"
        } else if cfg.exposure != crate::config::Exposure::Loopback {
            match cfg.exposure {
                crate::config::Exposure::Lan => "enabled (exposure = \"lan\")",
                crate::config::Exposure::Internet => "enabled (exposure = \"internet\")",
                crate::config::Exposure::Loopback => unreachable!(),
            }
        } else {
            "disabled (loopback only)"
        }
    );
    out += &format!("CA certificate:    {}\n", dir.join("ca.pem").display());
    out += &format!("CA SHA-256:        {}\n", ca_fingerprint(dir)?);
    if !crate::certs::ca_is_constrained(dir) {
        out += "CA name limits:    none (this CA predates name constraints and is trusted for any name;\n\
                \x20                  run `cert new-ca` to replace it, then re-import ca.pem everywhere)\n";
    }
    if admin_exists {
        out += "Administrator:     created\n";
    } else {
        out += &format!(
            "Setup token file:  {} (consumed when the administrator is created)\n",
            dir.join(SETUP_TOKEN_FILE).display()
        );
    }
    if cfg.exposure == crate::config::Exposure::Loopback {
        out += "To serve the LAN, set bind (for example \"0.0.0.0:8443\") and exposure = \"lan\" in config.toml.\n\
                LAN placement is not authentication; restrict the port with a firewall.\n";
    }
    if with_trust_steps {
        out += "\n";
        out += TRUST_STEPS;
        out += "\n";
    }
    Ok(out)
}

// ------------------------------------------------------------------ health

/// HTTPS GET `/healthz` on the configured bind address, verified against the local CA.
pub async fn health(dir: &Path) -> Result<(), String> {
    let cfg = Config::load(dir)?;
    let listeners = cfg.listener_binds()?;
    let addr = listeners
        .iter()
        .find(|(group, _)| matches!(group, crate::config::ListenerGroup::Combined | crate::config::ListenerGroup::Health))
        .map(|(_, address)| *address)
        .ok_or_else(|| "health listener is disabled in listener_groups".to_string())?;
    let ip = if addr.ip().is_unspecified() {
        if addr.is_ipv4() {
            IpAddr::from([127, 0, 0, 1])
        } else {
            IpAddr::from([0u16, 0, 0, 0, 0, 0, 0, 1])
        }
    } else {
        addr.ip()
    };
    let target = SocketAddr::new(ip, addr.port());
    let ca = std::fs::read(dir.join("ca.pem")).map_err(|e| format!("cannot read ca.pem: {e}"))?;
    let mut roots = rustls::RootCertStore::empty();
    for c in rustls::pki_types::CertificateDer::pem_slice_iter(&ca) {
        roots.add(c.map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    }
    let tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots)
        .with_no_client_auth();
    let connector = tokio_rustls::TlsConnector::from(std::sync::Arc::new(tls));
    // The whole check, including the retry under the second name, takes at most 3 s.
    let attempts = async {
        let mut last = String::from("no attempt");
        // A loopback target is verified as `localhost` first (always in the certificate).
        let names = if ip.is_loopback() {
            ["localhost".to_string(), ip.to_string()]
        } else {
            [ip.to_string(), "localhost".to_string()]
        };
        for name in names {
            let Ok(server_name) = rustls::pki_types::ServerName::try_from(name.clone()) else {
                continue;
            };
            match probe(&connector, server_name, target).await {
                Ok(()) => return Ok(()),
                Err(e) => last = format!("{target} (as {name}): {e}"),
            }
        }
        Err(last)
    };
    match tokio::time::timeout(Duration::from_secs(3), attempts).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(last)) => Err(format!("unhealthy: {last}")),
        Err(_) => Err(format!("unhealthy: {target} timed out after 3 s")),
    }
}

async fn probe(connector: &tokio_rustls::TlsConnector, name: rustls::pki_types::ServerName<'static>, target: SocketAddr) -> Result<(), String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let tcp = tokio::net::TcpStream::connect(target).await.map_err(|e| e.to_string())?;
    let mut s = connector.connect(name, tcp).await.map_err(|e| e.to_string())?;
    s.write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf).await; // a missing close_notify after the body is not a failure
    let text = String::from_utf8_lossy(&buf);
    if text.starts_with("HTTP/1.1 200") && text.contains("\"status\":\"ok\"") {
        Ok(())
    } else {
        Err(format!("unexpected response: {}", text.lines().next().unwrap_or("(empty)")))
    }
}

// ------------------------------------------------------------------ setup token

/// Replace the setup token. Only while no administrator exists.
pub fn setup_token(dir: &Path) -> Result<PathBuf, String> {
    if !dir.join("config.toml").exists() {
        return Err(format!("{} is not an initialised data directory (run `init`)", dir.display()));
    }
    let db = Db::open(&db_path(dir)).map_err(|e| e.to_string())?;
    if db.admin_get().map_err(|e| e.to_string())?.is_some() {
        return Err("an administrator already exists; use `admin reset-password` instead".into());
    }
    let path = dir.join(SETUP_TOKEN_FILE);
    write_replace(&path, format!("{}\n", auth::random_token()).as_bytes(), 0o600).map_err(|e| e.to_string())?;
    db.audit("cli", "setup-token.issue", "setup token replaced", "local");
    Ok(path)
}

// ------------------------------------------------------------------ backup and restore

fn integrity_ok(path: &Path) -> Result<i64, String> {
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(|e| e.to_string())?;
    let result: String = c.query_row("PRAGMA integrity_check", [], |r| r.get(0)).map_err(|e| e.to_string())?;
    if result != "ok" {
        return Err(format!("integrity_check failed: {result}"));
    }
    c.query_row("PRAGMA user_version", [], |r| r.get(0)).map_err(|e| e.to_string())
}

/// Copy `src` into a new 0600 file `tmp` with the SQLite online backup API. Used by `restore`, where
/// nothing else writes to either file: the copy needs no journal and no per-step fsync, because the
/// file is synced once at the end and a failed copy is deleted.
fn copy_database(src: &Path, tmp: &Path) -> Result<(), String> {
    let _ = std::fs::remove_file(tmp);
    crate::private_options(0o600)
        .write(true)
        .create_new(true)
        .open(tmp)
        .map_err(|e| format!("cannot create {}: {e}", tmp.display()))?;
    let result = (|| {
        let from = Connection::open_with_flags(src, OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e| e.to_string())?;
        from.busy_timeout(Duration::from_secs(10)).map_err(|e| e.to_string())?;
        let mut to = Connection::open(tmp).map_err(|e| e.to_string())?;
        to.pragma_update(None, "journal_mode", "OFF").map_err(|e| e.to_string())?;
        to.pragma_update(None, "synchronous", "OFF").map_err(|e| e.to_string())?;
        {
            let b = Backup::new(&from, &mut to).map_err(|e| e.to_string())?;
            b.run_to_completion(1024, Duration::ZERO, None).map_err(|e| e.to_string())?;
        }
        // The copy is a self-contained file: no WAL sidecars.
        to.pragma_update(None, "journal_mode", "DELETE").map_err(|e| e.to_string())?;
        drop(to);
        sync_file(tmp)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(tmp);
    }
    result
}

fn sync_file(path: &Path) -> Result<(), String> {
    // Opened for writing without truncation: Windows refuses to flush a handle opened read-only.
    std::fs::OpenOptions::new()
        .write(true)
        .open(path)
        .and_then(|f| f.sync_all())
        .map_err(|e| format!("cannot sync {}: {e}", path.display()))
}

/// Wall-clock budget `backup` accepts without `--force`.
pub const BACKUP_DEFAULT_BUDGET: Duration = Duration::from_secs(15 * 60);
/// Write rate a backup keeps to unless told otherwise. A microSD card in a Pi Zero 2 W sustains about
/// 10 to 20 MiB/s of synced writes; the coordinator's own commits need the rest, and an idle I/O class
/// is ignored by the `mq-deadline` and `none` schedulers that card and NVMe drivers use.
pub const BACKUP_DEFAULT_MAX_BYTES_PER_SEC: u64 = 6 * 1024 * 1024;
/// Dirty data a backup may leave in the page cache. On ext4 an fsync by another process (every
/// coordinator commit) also waits for this data, so the bound is what the commit has to wait for.
/// Output written between two forced flushes of the copy; it keeps the dirty page cache small on a slow
/// card. (A limit of 32 MiB on macOS and Windows was tried: on the hosted macOS ARM64 runner the copy then
/// stopped making progress at 60 of 98 MiB and did not finish within 90 s, so the flush stays small.)
const BACKUP_DIRTY_LIMIT: u64 = 1024 * 1024;
const PROBE_BYTES: usize = 4 * 1024 * 1024;
const MIB: f64 = 1024.0 * 1024.0;

#[derive(Clone, Debug)]
pub struct BackupOptions {
    /// Start although the estimate exceeds `budget`.
    pub force: bool,
    pub budget: Duration,
    /// Cap on the rate at which the copy is written; 0 removes the cap.
    pub max_bytes_per_sec: u64,
    /// Use this write rate for the estimate instead of measuring one in the output directory.
    pub assume_bytes_per_sec: Option<u64>,
    /// Print the preflight figures and progress lines to stderr.
    pub verbose: bool,
}

impl Default for BackupOptions {
    fn default() -> Self {
        BackupOptions {
            force: false,
            budget: BACKUP_DEFAULT_BUDGET,
            max_bytes_per_sec: BACKUP_DEFAULT_MAX_BYTES_PER_SEC,
            assume_bytes_per_sec: None,
            verbose: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct BackupReport {
    /// Database file plus write-ahead log.
    pub source_bytes: u64,
    /// Pages in use (the compacted copy is about this size).
    pub live_bytes: u64,
    pub free_bytes: u64,
    pub output_bytes: u64,
    pub estimate: Duration,
    pub copy: Duration,
    pub check: Duration,
    pub schema_version: i64,
}

impl BackupReport {
    pub fn summary(&self) -> String {
        format!(
            "database {:.1} MiB ({:.1} MiB in use, {:.1} MiB free pages); copy {:.1} MiB written in {:.1} s, integrity_check {:.1} s",
            self.source_bytes as f64 / MIB,
            self.live_bytes as f64 / MIB,
            self.free_bytes as f64 / MIB,
            self.output_bytes as f64 / MIB,
            self.copy.as_secs_f64(),
            self.check.as_secs_f64()
        )
    }
}

struct DbStats {
    page_size: u64,
    page_count: u64,
    freelist: u64,
    wal_bytes: u64,
}

fn db_stats(src: &Path) -> Result<DbStats, String> {
    let c = Connection::open_with_flags(src, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .or_else(|_| Connection::open_with_flags(src, OpenFlags::SQLITE_OPEN_READ_WRITE))
        .map_err(|e| format!("cannot open {}: {e}", src.display()))?;
    let n = |sql: &str| -> Result<u64, String> { c.query_row(sql, [], |r| r.get::<_, i64>(0)).map(|v| v as u64).map_err(|e| e.to_string()) };
    Ok(DbStats {
        page_size: n("PRAGMA page_size")?,
        page_count: n("PRAGMA page_count")?,
        freelist: n("PRAGMA freelist_count")?,
        wal_bytes: std::fs::metadata(format!("{}-wal", src.display())).map(|m| m.len()).unwrap_or(0),
    })
}

/// Estimated wall-clock time of a backup: the copy at the slower of the measured write rate and the
/// rate cap, plus an integrity check that reads the copy back at twice that rate.
pub fn estimate_backup(live_bytes: u64, write_bytes_per_sec: u64, max_bytes_per_sec: u64) -> Duration {
    let write = write_bytes_per_sec.max(1);
    let rate = if max_bytes_per_sec > 0 { write.min(max_bytes_per_sec) } else { write };
    let copy = live_bytes as f64 / rate as f64;
    Duration::from_secs_f64(copy * 1.5)
}

/// Write `PROBE_BYTES` to a scratch file next to the output and fsync it: the rate the device gives
/// a synced sequential write right now. Also proves the directory is writable before any work starts.
fn measure_write_rate(dir: &Path, stem: &str) -> Result<u64, String> {
    use std::io::Write;
    let probe = dir.join(format!(".{stem}.probe-{}", std::process::id()));
    let chunk = vec![0x5a_u8; 64 * 1024];
    let started = std::time::Instant::now();
    let outcome = (|| {
        let mut f = crate::private_options(0o600).write(true).create_new(true).open(&probe)?;
        for _ in 0..PROBE_BYTES / chunk.len() {
            f.write_all(&chunk)?;
        }
        f.sync_all()
    })();
    let _ = std::fs::remove_file(&probe);
    outcome.map_err(|e| format!("cannot write in {}: {e}", dir.display()))?;
    Ok((PROBE_BYTES as f64 / started.elapsed().as_secs_f64().max(0.001)) as u64)
}

fn free_disk_bytes(dir: &Path) -> Option<u64> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(dir.as_os_str().as_bytes()).ok()?;
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        // SAFETY: `path` is a valid NUL-terminated string and `st` is a properly sized, writable statvfs.
        if unsafe { libc::statvfs(path.as_ptr(), &mut st) } != 0 {
            return None;
        }
        Some(st.f_bavail as u64 * st.f_frsize as u64)
    }
    #[cfg(not(unix))]
    {
        let _ = dir;
        None
    }
}

/// `1h05m09s` or `3m07s`.
pub fn format_age(d: Duration) -> String {
    fmt_duration(d)
}

fn fmt_duration(d: Duration) -> String {
    let s = d.as_secs();
    if s >= 3600 {
        format!("{}h{:02}m{:02}s", s / 3600, s / 60 % 60, s % 60)
    } else {
        format!("{}m{:02}s", s / 60, s % 60)
    }
}

/// Lower this process's CPU priority and I/O priority: nice 19 and the idle I/O class on Linux, the
/// nice 19 on macOS, background mode on Windows.
/// Returns a description of what took effect. It is called by the command line, not by [`backup`], so
/// a test or embedding process never changes its own priority (a non-root process cannot raise it again).
pub fn lower_priority() -> String {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: both calls take plain integers; a failure only means the priority is unchanged.
        let nice = unsafe { libc::setpriority(libc::PRIO_PROCESS, 0, 19) } == 0;
        const IOPRIO_WHO_PROCESS: libc::c_long = 1;
        const IOPRIO_CLASS_IDLE: libc::c_long = 3 << 13;
        let io = unsafe { libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0 as libc::c_long, IOPRIO_CLASS_IDLE) } == 0;
        match (nice, io) {
            (true, true) => "nice 19, I/O class idle".into(),
            (true, false) => "nice 19 (I/O class idle not available)".into(),
            (false, true) => "I/O class idle (nice not available)".into(),
            (false, false) => "normal priority (neither nice nor the idle I/O class is available)".into(),
        }
    }
    #[cfg(target_os = "macos")]
    {
        // Only the CPU priority: the Darwin background class (PRIO_DARWIN_BG) also throttles disk I/O of the
        // whole process, and on the hosted macOS ARM64 runner the throttled copy and its integrity check
        // made no progress for over a minute while the coordinator kept writing. The write cap limits the
        // copy's I/O instead.
        // SAFETY: plain integer arguments; a failure only means the priority is unchanged.
        if unsafe { libc::setpriority(libc::PRIO_PROCESS, 0, 19) } == 0 {
            "nice 19 (disk I/O not throttled)".into()
        } else {
            "normal priority (nice is not available)".into()
        }
    }
    #[cfg(windows)]
    {
        // PROCESS_MODE_BACKGROUND_BEGIN lowers CPU, memory and I/O priority of the calling process.
        const PROCESS_MODE_BACKGROUND_BEGIN: u32 = 0x0010_0000;
        #[link(name = "kernel32")]
        extern "system" {
            fn GetCurrentProcess() -> *mut core::ffi::c_void;
            fn SetPriorityClass(process: *mut core::ffi::c_void, class: u32) -> i32;
        }
        // SAFETY: the pseudo handle of the current process is always valid; a failure (return 0) only
        // means the priority is unchanged.
        if unsafe { SetPriorityClass(GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN) } != 0 {
            "background mode (CPU and I/O priority lowered)".into()
        } else {
            "normal priority (background mode is not available)".into()
        }
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
    {
        "normal priority (not supported on this platform)".into()
    }
}

/// Paces and reports `VACUUM INTO`. SQLite calls this every few thousand virtual-machine steps while
/// the statement runs. Every 5 ms of wall time it compares the size of the output with what the rate
/// cap allows and sleeps for the difference, forces the output to disk after every
/// `BACKUP_DIRTY_LIMIT` bytes, and prints a progress line every 5 s.
struct Pace {
    started: std::time::Instant,
    checked: std::time::Instant,
    reported: std::time::Instant,
    out: PathBuf,
    sync: Option<std::fs::File>,
    synced_len: u64,
    max_bytes_per_sec: u64,
    live_bytes: u64,
    verbose: bool,
}

impl Pace {
    fn tick(&mut self) {
        let now = std::time::Instant::now();
        if now.duration_since(self.checked) < Duration::from_millis(5) {
            return;
        }
        let len = std::fs::metadata(&self.out).map(|m| m.len()).unwrap_or(0);
        if len >= self.synced_len + BACKUP_DIRTY_LIMIT {
            if let Some(f) = &self.sync {
                let _ = f.sync_data();
            }
            self.synced_len = len;
        }
        if self.max_bytes_per_sec > 0 {
            let allowed_at = Duration::from_secs_f64(len as f64 / self.max_bytes_per_sec as f64);
            let elapsed = now.duration_since(self.started);
            if allowed_at > elapsed {
                std::thread::sleep((allowed_at - elapsed).min(Duration::from_millis(250)));
            }
        }
        self.checked = std::time::Instant::now();
        if self.verbose && self.checked.duration_since(self.reported) >= Duration::from_secs(5) {
            self.reported = self.checked;
            let done = (len as f64 / self.live_bytes.max(1) as f64).min(0.99);
            let secs = self.checked.duration_since(self.started).as_secs_f64().max(0.001);
            let rate = len as f64 / secs;
            let eta = if len > 0 { (self.live_bytes.saturating_sub(len)) as f64 / rate } else { 0.0 };
            eprintln!(
                "backup: {:>3.0}%  {:.1} of about {:.1} MiB  {:.1} MiB/s  about {} left",
                done * 100.0,
                len as f64 / MIB,
                self.live_bytes as f64 / MIB,
                rate / MIB,
                fmt_duration(Duration::from_secs_f64(eta))
            );
        }
    }
}

/// Write a compacted, consistent copy of `src` to `tmp` (which already exists, empty, mode 0600) with
/// `VACUUM INTO`. The statement reads one snapshot of the database, so concurrent writers neither
/// restart it nor wait for it, and it writes only pages in use, in one pass. The copy has incremental
/// auto-vacuum set, so a database restored from it can give free pages back later.
fn vacuum_into(src: &Path, tmp: &Path, pace: Pace) -> Result<(), String> {
    // Read-write only because SQLite refuses `PRAGMA auto_vacuum` on a read-only handle; the statement
    // itself only reads this database.
    let mut from = Connection::open_with_flags(src, OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e| format!("cannot open {}: {e}", src.display()))?;
    from.busy_timeout(Duration::from_secs(10)).map_err(|e| e.to_string())?;
    // A small cache makes the output pager write as it goes instead of holding megabytes until the end.
    from.pragma_update(None, "cache_size", -1024).map_err(|e| e.to_string())?;
    from.pragma_update(None, "auto_vacuum", 2).map_err(|e| e.to_string())?;
    let mut pace = pace;
    pace.sync = std::fs::File::open(tmp).ok();
    from.progress_handler(
        100,
        Some(move || {
            pace.tick();
            false
        }),
    )
    .map_err(|e| e.to_string())?;
    let target = tmp.to_str().ok_or("the backup path is not valid UTF-8")?;
    from.execute("VACUUM INTO ?1", [target]).map_err(|e| format!("copy failed: {e}"))?;
    Ok(())
}

/// Online backup of the coordinator database to `output` (0600, never overwrites) with default
/// options and no progress output.
pub fn backup(dir: &Path, output: &Path) -> Result<BackupReport, String> {
    backup_with(dir, output, &BackupOptions::default())
}

/// Online backup of the coordinator database to `output` (0600, never overwrites).
///
/// The copy is made by `VACUUM INTO` from a read snapshot, so the serving process is never blocked and
/// the backup is never restarted by its writes; free pages are not copied. The write rate is capped
/// (`max_bytes_per_sec`), dirty data is flushed in small amounts, and progress and an ETA are reported.
/// The run is refused before it starts when the estimate exceeds the budget, unless forced. The copy
/// is verified with `PRAGMA integrity_check` before it appears under its final name.
pub fn backup_with(dir: &Path, output: &Path, opts: &BackupOptions) -> Result<BackupReport, String> {
    let say = |text: String| {
        if opts.verbose {
            eprintln!("{text}");
        }
    };
    let src = db_path(dir);
    if !src.exists() {
        return Err(format!("no database at {}", src.display()));
    }
    if output.exists() {
        return Err(format!("{} already exists; refusing to overwrite", output.display()));
    }
    let parent = match output.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    };
    let stats = db_stats(&src)?;
    let live_bytes = (stats.page_count - stats.freelist.min(stats.page_count)) * stats.page_size;
    let report_base = |output_bytes, estimate, copy, check, schema_version| BackupReport {
        source_bytes: stats.page_count * stats.page_size + stats.wal_bytes,
        live_bytes,
        free_bytes: stats.freelist * stats.page_size,
        output_bytes,
        estimate,
        copy,
        check,
        schema_version,
    };
    let name = output.file_name().and_then(|n| n.to_str()).unwrap_or("backup").to_string();
    let write_rate = match opts.assume_bytes_per_sec {
        Some(r) => r,
        None => measure_write_rate(&parent, &name)?,
    };
    let estimate = estimate_backup(live_bytes, write_rate, opts.max_bytes_per_sec);
    say(format!(
        "backup: database {:.1} MiB in use ({:.1} MiB of free pages are not copied, write-ahead log {:.1} MiB); \
         writes {}at {:.1} MiB/s; estimated {} including the integrity check, budget {}",
        live_bytes as f64 / MIB,
        (stats.freelist * stats.page_size) as f64 / MIB,
        stats.wal_bytes as f64 / MIB,
        if opts.max_bytes_per_sec > 0 { "capped " } else { "uncapped " },
        write_rate.min(if opts.max_bytes_per_sec > 0 { opts.max_bytes_per_sec } else { u64::MAX }) as f64 / MIB,
        fmt_duration(estimate),
        fmt_duration(opts.budget)
    ));
    if let Some(free) = free_disk_bytes(&parent) {
        let need = live_bytes + live_bytes / 20 + 1024 * 1024;
        if free < need {
            return Err(format!(
                "not enough free space in {}: the backup needs about {:.1} MiB and {:.1} MiB are free",
                parent.display(),
                need as f64 / MIB,
                free as f64 / MIB
            ));
        }
    }
    if estimate > opts.budget && !opts.force {
        return Err(format!(
            "the estimated time {} exceeds the budget {}. The database holds {:.1} MiB in use; \
             reduce it first (`bridge-coordinator compact` after stopping the service), raise `--budget-minutes`, \
             give a measured rate with `--assume-mb-s`, or pass `--force`",
            fmt_duration(estimate),
            fmt_duration(opts.budget),
            live_bytes as f64 / MIB
        ));
    }
    let tmp = parent.join(format!(".{name}.partial-{}", std::process::id()));
    let _ = std::fs::remove_file(&tmp);
    crate::private_options(0o600)
        .write(true)
        .create_new(true)
        .open(&tmp)
        .map_err(|e| format!("cannot create {}: {e}", tmp.display()))?;
    let started = std::time::Instant::now();
    let pace = Pace {
        started,
        checked: started,
        reported: started,
        out: tmp.clone(),
        sync: None,
        synced_len: 0,
        max_bytes_per_sec: opts.max_bytes_per_sec,
        live_bytes,
        verbose: opts.verbose,
    };
    let copied = vacuum_into(&src, &tmp, pace).and_then(|()| {
        sync_file(&tmp)?;
        // VACUUM INTO writes the source's journal mode into the header; the copy is a self-contained file.
        let c = Connection::open(&tmp).map_err(|e| e.to_string())?;
        c.pragma_update(None, "journal_mode", "DELETE").map_err(|e| e.to_string())?;
        Ok(())
    });
    if let Err(e) = copied {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    let copy = started.elapsed();
    let output_bytes = std::fs::metadata(&tmp).map(|m| m.len()).unwrap_or(0);
    say(format!(
        "backup: copied {:.1} MiB in {}; verifying with integrity_check",
        output_bytes as f64 / MIB,
        fmt_duration(copy)
    ));
    let check_started = std::time::Instant::now();
    let schema_version = match integrity_ok(&tmp) {
        Ok(v) => v,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return Err(e);
        }
    };
    let check = check_started.elapsed();
    // hard_link fails when the target appeared meanwhile, so a concurrent file is never replaced.
    let linked = std::fs::hard_link(&tmp, output);
    let _ = std::fs::remove_file(&tmp);
    linked.map_err(|e| format!("cannot create {}: {e}", output.display()))?;
    if let Ok(d) = std::fs::File::open(&parent) {
        let _ = d.sync_all();
    }
    Ok(report_base(output_bytes, estimate, copy, check, schema_version))
}

/// Newest finished backup in `dir` (`*.sqlite3`, not a `.partial` file), checked and compared with
/// `max_age`. `full` runs `integrity_check`; otherwise `quick_check`.
pub struct SnapshotStatus {
    pub path: PathBuf,
    pub age: Duration,
    pub bytes: u64,
    pub schema_version: i64,
}

pub fn check_snapshot(dir: &Path, max_age: Duration, full: bool) -> Result<SnapshotStatus, String> {
    let entries = std::fs::read_dir(dir).map_err(|e| format!("cannot read {}: {e}", dir.display()))?;
    let mut newest: Option<(std::time::SystemTime, PathBuf, u64)> = None;
    for entry in entries.flatten() {
        let path = entry.path();
        let file = entry.file_name().to_string_lossy().into_owned();
        if file.starts_with('.') || !file.ends_with(".sqlite3") {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        let Ok(modified) = meta.modified() else { continue };
        if meta.is_file() && newest.as_ref().is_none_or(|(t, _, _)| modified > *t) {
            newest = Some((modified, path, meta.len()));
        }
    }
    let (modified, path, bytes) = newest.ok_or_else(|| format!("no finished backup (*.sqlite3) in {}; run `backup --output FILE`", dir.display()))?;
    let age = std::time::SystemTime::now().duration_since(modified).unwrap_or_default();
    let c = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(|e| format!("{}: {e}", path.display()))?;
    let pragma = if full { "PRAGMA integrity_check" } else { "PRAGMA quick_check" };
    let verdict: String = c.query_row(pragma, [], |r| r.get(0)).map_err(|e| format!("{}: {e}", path.display()))?;
    if verdict != "ok" {
        return Err(format!("{}: {pragma} failed: {verdict}", path.display()));
    }
    let schema_version: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0)).map_err(|e| e.to_string())?;
    if schema_version > db::SCHEMA_VERSION {
        return Err(format!(
            "{} has schema version {schema_version}; this binary supports up to {}",
            path.display(),
            db::SCHEMA_VERSION
        ));
    }
    if age > max_age {
        return Err(format!(
            "the newest backup, {}, is {} old; the limit is {}. Take a new one with `backup --output FILE` before upgrading",
            path.display(),
            fmt_duration(age),
            fmt_duration(max_age)
        ));
    }
    Ok(SnapshotStatus {
        path,
        age,
        bytes,
        schema_version,
    })
}

/// Rebuild the database file compactly with incremental auto-vacuum, the same pass a backup makes.
/// Refuses while a server holds the data directory lock. The previous file is kept as
/// `coordinator.sqlite3.before-compact-<UTC>`. Returns `(kept file, size before, size after)`.
pub fn compact(dir: &Path, opts: &BackupOptions) -> Result<(PathBuf, u64, u64), String> {
    if !dir.join("config.toml").exists() {
        return Err(format!("{} is not an initialised data directory (run `init`)", dir.display()));
    }
    let _lock = lock_data_dir(dir).map_err(|e| format!("{e}; stop the coordinator before compacting"))?;
    let live = db_path(dir);
    if !live.exists() {
        return Err(format!("no database at {}", live.display()));
    }
    let before = std::fs::metadata(&live).map(|m| m.len()).unwrap_or(0);
    let staged_name = dir.join(format!("{}.compact-{}", crate::DB_FILE, std::process::id()));
    let _ = std::fs::remove_file(&staged_name);
    let staged_output = staged_name.clone();
    // Reuse the backup path (budget, pacing, integrity check) writing into the data directory.
    let _ = std::fs::remove_file(&staged_output);
    backup_with(dir, &staged_output, opts)?;
    let kept = dir.join(format!("{}.before-compact-{}", crate::DB_FILE, crate::utc_stamp()));
    if let Ok(c) = Connection::open(&live) {
        let _ = c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
    }
    std::fs::rename(&live, &kept).map_err(|e| format!("cannot keep the old database: {e}"))?;
    for suffix in ["-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{suffix}", live.display()));
    }
    std::fs::rename(&staged_output, &live).map_err(|e| format!("cannot install the compacted database: {e}"))?;
    let after = std::fs::metadata(&live).map(|m| m.len()).unwrap_or(0);
    Ok((kept, before, after))
}

/// Replace the database with `input`. Refuses while a server holds the data directory lock.
/// The previous database is kept as `coordinator.sqlite3.before-restore-<UTC>`.
pub fn restore(dir: &Path, input: &Path) -> Result<PathBuf, String> {
    if !dir.join("config.toml").exists() {
        return Err(format!("{} is not an initialised data directory (run `init`)", dir.display()));
    }
    let _lock = lock_data_dir(dir).map_err(|e| format!("{e}; stop the coordinator before restoring"))?;
    if !input.exists() {
        return Err(format!("{} does not exist", input.display()));
    }
    let version = integrity_ok(input).map_err(|e| format!("{}: {e}", input.display()))?;
    if !(1..=db::SCHEMA_VERSION).contains(&version) {
        return Err(format!(
            "{} has schema version {version}; this binary supports 1..={}",
            input.display(),
            db::SCHEMA_VERSION
        ));
    }
    let live = db_path(dir);
    let staged = dir.join(format!("{}.restore-{}", crate::DB_FILE, std::process::id()));
    copy_database(input, &staged)?;
    let kept = dir.join(format!("{}.before-restore-{}", crate::DB_FILE, crate::utc_stamp()));
    if live.exists() {
        // Fold any WAL content into the main file before it is moved aside.
        if let Ok(c) = Connection::open(&live) {
            let _ = c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
        }
        std::fs::rename(&live, &kept).map_err(|e| format!("cannot keep the old database: {e}"))?;
    }
    for suffix in ["-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{suffix}", live.display()));
    }
    std::fs::rename(&staged, &live).map_err(|e| format!("cannot install the restored database: {e}"))?;
    // A backup can predate a logout, a password change or a revocation. Browser sessions are not
    // carried over; workers and API tokens are (see `restored_credentials`).
    let db = Db::open(&live).map_err(|e| format!("cannot open the restored database: {e}"))?;
    db.sessions_clear().map_err(|e| e.to_string())?;
    db.audit("cli", "restore", "database restored; browser sessions deleted", "local");
    Ok(kept)
}

/// What is active after a restore, with the commands to revoke it. Revocations made after the
/// backup was taken are undone by a restore, so the operator must check this list.
pub fn restored_credentials(dir: &Path) -> Result<String, String> {
    let db = Db::open(&db_path(dir)).map_err(|e| e.to_string())?;
    let workers = db.workers_list().map_err(|e| e.to_string())?;
    let tokens = db.tokens_list().map_err(|e| e.to_string())?;
    let mut out = String::from("Credentials active after the restore (revocations made after the backup are undone):\n");
    if workers.is_empty() {
        out += "  workers: none\n";
    }
    for w in &workers {
        out += &format!("  worker {}: bridge-coordinator worker revoke {}\n", w.name, w.name);
    }
    if tokens.is_empty() {
        out += "  API tokens: none\n";
    }
    for (name, expires) in &tokens {
        let until = expires.map_or("never expires".to_string(), |e| format!("expires at Unix time {e}"));
        out += &format!("  API token {name} ({until}): bridge-coordinator token revoke {name}\n");
    }
    out += "Browser sessions were deleted; every administrator has to log in again.\n";
    Ok(out)
}
