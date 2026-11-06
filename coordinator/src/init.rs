//! `init`: data directory, database, CA, server certificate, config and setup token.

use crate::certs::{self, Sans};
use crate::config::config_text;
use crate::db::Db;
use crate::{auth, db_path, write_new, SETUP_TOKEN_FILE};

pub fn init(dir: &std::path::Path, sans: &Sans) -> Result<(), String> {
    init_with(dir, sans, None)
}

/// `init` with an explicit listen address. A non-loopback `bind` also sets `lan_enabled = true`.
pub fn init_with(dir: &std::path::Path, sans: &Sans, bind: Option<&str>) -> Result<(), String> {
    if ["config.toml", "ca.pem", "ca.key", "server.pem", "server.key", crate::DB_FILE]
        .iter()
        .all(|f| dir.join(f).exists())
    {
        println!("already initialised: {} (nothing changed; `info` shows the details)", dir.display());
        return Ok(());
    }
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    // Best effort: a mounted volume may belong to another user, and init never chowns.
    crate::restrict_permissions(dir, 0o700);
    // Precedence: --bind, then BRIDGE_COORDINATOR_BIND / BRIDGE_COORDINATOR_LAN_ENABLED, then the
    // built-in defaults. The written config.toml therefore matches the environment `init` ran in.
    // At start-up the environment still overrides config.toml.
    let env_bind = std::env::var(crate::config::ENV_BIND).ok().filter(|v| !v.is_empty());
    let defaults = crate::config::Config::default();
    let chosen = bind.map(str::to_string).or(env_bind).unwrap_or(defaults.bind);
    let addr: std::net::SocketAddr = chosen.parse().map_err(|e| format!("invalid bind address {chosen:?}: {e}"))?;
    let lan = match std::env::var(crate::config::ENV_LAN_ENABLED) {
        Ok(v) if !v.is_empty() => crate::config::parse_bool(crate::config::ENV_LAN_ENABLED, &v)?,
        _ => !addr.ip().is_loopback(),
    };
    let bind = chosen;
    write_new(&dir.join("config.toml"), config_text(&bind, lan).as_bytes(), 0o600).map_err(|e| e.to_string())?;
    let db = Db::open(&db_path(dir)).map_err(|e| e.to_string())?;
    certs::init_certs(dir, sans)?;
    // A setup token is only issued while no administrator exists.
    if db.admin_get().map_err(|e| e.to_string())?.is_none() {
        let created = write_new(&dir.join(SETUP_TOKEN_FILE), format!("{}\n", auth::random_token()).as_bytes(), 0o600).map_err(|e| e.to_string())?;
        if !created {
            println!("setup token already present: {}", dir.join(SETUP_TOKEN_FILE).display());
        }
    }
    println!("initialised {}\n", dir.display());
    print!("{}", crate::ops::report(dir, true)?);
    Ok(())
}
