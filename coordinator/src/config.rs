//! `config.toml` in the data directory.

use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::Path;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct Config {
    /// Listen address. Non-loopback needs `lan_enabled = true` (NET-003).
    pub bind: String,
    pub lan_enabled: bool,
    pub max_events_per_worker: u64,
    /// Extra exact Origin values accepted for cookie-authenticated writes
    /// (the default is `https://<Host header>`).
    pub allowed_origins: Vec<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind: "127.0.0.1:8443".into(),
            lan_enabled: false,
            max_events_per_worker: 50_000,
            allowed_origins: vec![],
        }
    }
}

pub const DEFAULT_CONFIG_TEXT: &str = "# bridge-coordinator configuration\n\
# Listen address. A non-loopback address is refused unless lan_enabled = true.\n\
bind = \"127.0.0.1:8443\"\n\
lan_enabled = false\n\
# Stored events per worker; oldest are deleted first.\n\
max_events_per_worker = 50000\n\
# Extra exact Origin values accepted for cookie-authenticated writes.\n\
allowed_origins = []\n";

impl Config {
    pub fn load(dir: &Path) -> Result<Config, String> {
        let path = dir.join("config.toml");
        let text = std::fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e} (run `init` first)", path.display()))?;
        let cfg: Config = toml::from_str(&text).map_err(|e| format!("invalid {}: {e}", path.display()))?;
        if cfg.max_events_per_worker == 0 {
            return Err("max_events_per_worker must be at least 1".into());
        }
        cfg.bind_addr()?;
        Ok(cfg)
    }

    pub fn bind_addr(&self) -> Result<SocketAddr, String> {
        self.bind.parse().map_err(|e| format!("invalid bind address {:?}: {e}", self.bind))
    }

    /// Parse the bind address and refuse a non-loopback one without `lan_enabled`.
    pub fn checked_bind(&self) -> Result<SocketAddr, String> {
        let addr = self.bind_addr()?;
        if !addr.ip().is_loopback() && !self.lan_enabled {
            return Err(format!(
                "refusing to bind {addr}: it is not a loopback address and lan_enabled is not set to true in config.toml"
            ));
        }
        Ok(addr)
    }
}
