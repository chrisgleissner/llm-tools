//! Operator commands: info, health, backup, restore, setup-token and the data directory lock.

use crate::config::Config;
use crate::db::{self, Db};
use crate::{auth, db_path, netinfo, write_replace, SETUP_TOKEN_FILE};
use rusqlite::{backup::Backup, Connection, OpenFlags};
use sha2::{Digest, Sha256};
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const LOCK_FILE: &str = "coordinator.lock";

/// Exclusive advisory lock held for the lifetime of `serve`. The kernel releases it when the
/// process exits, so a crash never leaves a stale lock. `restore` takes the same lock.
pub fn lock_data_dir(dir: &Path) -> Result<std::fs::File, String> {
    use std::os::unix::fs::OpenOptionsExt;
    let f = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(dir.join(LOCK_FILE))
        .map_err(|e| format!("cannot open {}: {e}", dir.join(LOCK_FILE).display()))?;
    f.try_lock()
        .map_err(|_| format!("another bridge-coordinator is using {} (lock file {LOCK_FILE})", dir.display()))?;
    Ok(f)
}

/// SHA-256 of the first certificate in `ca.pem`, as colon-separated upper-case hex.
pub fn ca_fingerprint(dir: &Path) -> Result<String, String> {
    let pem = std::fs::read(dir.join("ca.pem")).map_err(|e| format!("cannot read ca.pem: {e}"))?;
    let der = rustls_pemfile::certs(&mut &*pem)
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
    let Some(Ok(der)) = rustls_pemfile::certs(&mut &*pem).next() else {
        return false;
    };
    let needle: Vec<u8> = match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(a)) => a.octets().to_vec(),
        Ok(IpAddr::V6(a)) => a.octets().to_vec(),
        Err(_) => host.as_bytes().to_vec(),
    };
    der.as_ref().windows(needle.len()).any(|w| w == needle.as_slice())
}

/// HTTPS URLs for the configured bind address, each with whether the certificate covers it.
pub fn urls(dir: &Path, cfg: &Config) -> Result<Vec<(String, bool)>, String> {
    let addr = cfg.bind_addr()?;
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
                "   (not in the server certificate; run `cert renew`)"
            }
        );
    }
    out += &format!("Bind address:      {}\n", cfg.bind);
    out += &format!(
        "LAN access:        {}\n",
        if std::env::var_os(crate::config::ENV_CONTAINER).is_some_and(|v| !v.is_empty()) {
            "decided by the container's published port (BRIDGE_PUBLISH_ADDRESS in compose.yaml)"
        } else if cfg.lan_enabled {
            "enabled (lan_enabled = true)"
        } else {
            "disabled (loopback only)"
        }
    );
    out += &format!("CA certificate:    {}\n", dir.join("ca.pem").display());
    out += &format!("CA SHA-256:        {}\n", ca_fingerprint(dir)?);
    if admin_exists {
        out += "Administrator:     created\n";
    } else {
        out += &format!(
            "Setup token file:  {} (consumed when the administrator is created)\n",
            dir.join(SETUP_TOKEN_FILE).display()
        );
    }
    if !cfg.lan_enabled {
        out += "To serve the LAN, set bind (for example \"0.0.0.0:8443\") and lan_enabled = true in config.toml.\n\
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
    let addr = cfg.bind_addr()?;
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
    for c in rustls_pemfile::certs(&mut &*ca) {
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

/// Copy `src` into a new 0600 file `tmp` with the SQLite online backup API.
fn copy_database(src: &Path, tmp: &Path) -> Result<(), String> {
    use std::os::unix::fs::OpenOptionsExt;
    let _ = std::fs::remove_file(tmp);
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(tmp)
        .map_err(|e| format!("cannot create {}: {e}", tmp.display()))?;
    let result = (|| {
        let from = Connection::open_with_flags(src, OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e| e.to_string())?;
        from.busy_timeout(Duration::from_secs(10)).map_err(|e| e.to_string())?;
        let mut to = Connection::open(tmp).map_err(|e| e.to_string())?;
        {
            let b = Backup::new(&from, &mut to).map_err(|e| e.to_string())?;
            b.run_to_completion(256, Duration::from_millis(5), None).map_err(|e| e.to_string())?;
        }
        // The copy is a self-contained file: no WAL sidecars.
        to.pragma_update(None, "journal_mode", "DELETE").map_err(|e| e.to_string())?;
        Ok::<(), String>(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(tmp);
    }
    result
}

/// Online backup of the coordinator database to `output` (0600, never overwrites).
pub fn backup(dir: &Path, output: &Path) -> Result<(), String> {
    let src = db_path(dir);
    if !src.exists() {
        return Err(format!("no database at {}", src.display()));
    }
    if output.exists() {
        return Err(format!("{} already exists; refusing to overwrite", output.display()));
    }
    let tmp = output.with_file_name(format!(
        ".{}.partial-{}",
        output.file_name().and_then(|n| n.to_str()).unwrap_or("backup"),
        std::process::id()
    ));
    copy_database(&src, &tmp)?;
    if let Err(e) = integrity_ok(&tmp) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    // hard_link fails when the target appeared meanwhile, so a concurrent file is never replaced.
    let linked = std::fs::hard_link(&tmp, output);
    let _ = std::fs::remove_file(&tmp);
    linked.map_err(|e| format!("cannot create {}: {e}", output.display()))
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
    if version < 1 || version > db::SCHEMA_VERSION {
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
    Ok(kept)
}
