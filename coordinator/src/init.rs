//! `init`: data directory, database, CA, server certificate, config and setup token.

use crate::certs::{self, Sans};
use crate::db::Db;
use crate::{auth, db_path, write_new, SETUP_TOKEN_FILE};

pub fn init(dir: &std::path::Path, sans: &Sans) -> Result<(), String> {
    init_with(dir, sans, None)
}

/// `init` with an explicit listen address. A non-loopback `bind` defaults to LAN exposure.
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
    // Precedence: --bind, then BRIDGE_COORDINATOR_BIND / BRIDGE_COORDINATOR_EXPOSURE, then the
    // built-in defaults. The written config.toml therefore matches the environment `init` ran in.
    // At start-up the environment still overrides config.toml.
    let env_bind = std::env::var(crate::config::ENV_BIND).ok().filter(|v| !v.is_empty());
    let defaults = crate::config::Config::default();
    let chosen = bind.map(str::to_string).or(env_bind).unwrap_or(defaults.bind);
    let addr: std::net::SocketAddr = chosen.parse().map_err(|e| format!("invalid bind address {chosen:?}: {e}"))?;
    let exposure = match std::env::var(crate::config::ENV_EXPOSURE) {
        Ok(v) if !v.is_empty() => crate::config::parse_exposure(crate::config::ENV_EXPOSURE, &v)?,
        _ => match std::env::var(crate::config::ENV_LAN_ENABLED) {
            Ok(v) if !v.is_empty() => {
                if crate::config::parse_bool(crate::config::ENV_LAN_ENABLED, &v)? {
                    crate::config::Exposure::Lan
                } else {
                    crate::config::Exposure::Loopback
                }
            }
            _ if !addr.ip().is_loopback() => crate::config::Exposure::Lan,
            _ => crate::config::Exposure::Loopback,
        },
    };
    let bind = chosen;
    write_new(
        &dir.join("config.toml"),
        crate::config::config_text_for_exposure(&bind, exposure).as_bytes(),
        0o600,
    )
    .map_err(|e| e.to_string())?;
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
