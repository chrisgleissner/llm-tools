//! `config.toml` in the data directory.

use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::Path;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct Config {
    /// Listen address. Non-loopback needs `lan_enabled = true` (NET-003).
    pub bind: String,
    /// Optional route-group listeners. If any is set, only the explicitly listed groups listen.
    pub listener_groups: ListenerGroups,
    pub lan_enabled: bool,
    /// Minimum TLS version. TLS 1.2 is an explicit compatibility opt-in.
    pub min_tls: String,
    pub max_events_per_worker: u64,
    /// Extra exact Origin values accepted for cookie-authenticated writes
    /// (the default is `https://<Host header>`).
    pub allowed_origins: Vec<String>,
    /// Proxy CIDRs allowed to supply the real client address in Forwarded/X-Forwarded-For.
    pub trusted_proxies: Vec<String>,
    /// Directory with the built web interface (`index.html`, `app.js`, ...). Unset: `web` next to
    /// the executable, else `/usr/share/bridge-coordinator/web`.
    pub web_dir: Option<String>,
    /// Optional operator-managed TLS certificate and key. When both are set, the
    /// server watches these files and uses a valid replacement on new handshakes.
    pub tls_cert_file: Option<String>,
    pub tls_key_file: Option<String>,
    /// Exact SNI hostname for the operator-managed identity; all other names use the internal CA.
    pub tls_cert_name: Option<String>,
    /// Canonical HTTPS DNS origin for passkeys. Once passkeys exist, the first registered origin
    /// is persisted in the coordinator database and cannot be changed without re-enrolment.
    pub public_origin: Option<String>,
    /// Total concurrent TCP connections; further connections are closed immediately (NET-010).
    pub max_connections: usize,
    /// Concurrent TCP connections from one client address (IPv6 clients counted by /64).
    pub max_connections_per_ip: usize,
    /// Slots out of `max_connections` that only worker connections with a valid worker token can
    /// use once the other slots are taken (capped at a quarter of `max_connections`).
    pub worker_reserve_connections: usize,
    /// Seconds to receive a complete request body once the handler starts reading it (slow-body limit).
    pub request_body_timeout_secs: u64,
    /// Seconds an HTTP connection may wait for the next request header (keep-alive idle and
    /// slow-header limit).
    pub http_idle_timeout_secs: u64,
    /// Seconds between server pings on browser `/stream` and terminal WebSockets.
    pub ws_ping_interval_secs: u64,
    /// Close a browser WebSocket that has sent nothing (not even a pong) for this many seconds.
    pub ws_idle_timeout_secs: u64,
    /// Authenticated mutation budget per token in a rolling 60-second window.
    pub mutation_limit_per_token: usize,
    /// Authenticated mutation budget per client address in a rolling 60-second window.
    pub mutation_limit_per_address: usize,
    /// Maximum lifetime for a newly-created API token, in days (0/no expiry remains LAN-only).
    pub max_token_lifetime_days: u32,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct ListenerGroups {
    pub console_rest: Option<String>,
    pub worker_channel: Option<String>,
    pub health: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListenerGroup {
    Combined,
    ConsoleRest,
    WorkerChannel,
    Health,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind: "127.0.0.1:8443".into(),
            listener_groups: ListenerGroups::default(),
            lan_enabled: false,
            min_tls: "1.3".into(),
            max_events_per_worker: 50_000,
            allowed_origins: vec![],
            trusted_proxies: vec![],
            web_dir: None,
            tls_cert_file: None,
            tls_key_file: None,
            tls_cert_name: None,
            public_origin: None,
            max_connections: 256,
            max_connections_per_ip: 32,
            worker_reserve_connections: 16,
            request_body_timeout_secs: 30,
            http_idle_timeout_secs: 30,
            ws_ping_interval_secs: 30,
            ws_idle_timeout_secs: 90,
            mutation_limit_per_token: 30,
            mutation_limit_per_address: 120,
            max_token_lifetime_days: 3650,
        }
    }
}

/// Data directory used when neither `--data-dir` nor `BRIDGE_COORDINATOR_DATA_DIR` is given
/// (the systemd unit's `StateDirectory`).
pub const DEFAULT_DATA_DIR: &str = "/var/lib/bridge-coordinator";
pub const ENV_DATA_DIR: &str = "BRIDGE_COORDINATOR_DATA_DIR";
pub const ENV_BIND: &str = "BRIDGE_COORDINATOR_BIND";
pub const ENV_LAN_ENABLED: &str = "BRIDGE_COORDINATOR_LAN_ENABLED";
/// Comma-separated host names and addresses browsers and workers use to reach the coordinator.
/// When set, they replace discovery for the certificate (`init`, `cert renew`) and for the URLs
/// `info` prints. Needed in a container, where the discovered names are container-internal.
pub const ENV_PUBLIC_NAMES: &str = "BRIDGE_COORDINATOR_PUBLIC_NAMES";
/// Port in the printed URLs when it differs from the bind port (a published container port).
pub const ENV_PUBLIC_PORT: &str = "BRIDGE_COORDINATOR_PUBLIC_PORT";
/// Set by the container image: network exposure is decided by the published port, not `lan_enabled`.
pub const ENV_CONTAINER: &str = "BRIDGE_COORDINATOR_CONTAINER";

/// `config.toml` text written by `init`, generated from [`Config::default`] so the file and the
/// built-in defaults cannot differ. A non-loopback `bind` also sets `lan_enabled = true` at the
/// call site (the operator asked for it explicitly).
pub fn config_text(bind: &str, lan_enabled: bool) -> String {
    let d = Config::default();
    format!(
        "# bridge-coordinator configuration. Every key has a built-in default; the environment\n\
    # variables BRIDGE_COORDINATOR_BIND and BRIDGE_COORDINATOR_LAN_ENABLED override bind and\n\
    # lan_enabled at start-up.\n\
    # Listen address. A non-loopback address is refused unless lan_enabled = true.\n\
    bind = \"{bind}\"\n\
    lan_enabled = {lan_enabled}\n\
    # Optional split listeners. If any value is set, only listed groups are exposed;\n\
    # unlisted groups are disabled. With all three omitted, `bind` serves every group.\n\
    # [listener_groups]\n\
    # console_rest = \"127.0.0.1:8443\"\n\
    # worker_channel = \"127.0.0.1:8444\"\n\
    # health = \"127.0.0.1:8445\"\n\
    # Minimum TLS version. Keep 1.3 unless an older client requires TLS 1.2.\n\
    min_tls = \"{}\"\n\
    # Stored events per worker; oldest are deleted first.\n\
    max_events_per_worker = {}\n\
    # Extra exact Origin values accepted for cookie-authenticated writes.\n\
    allowed_origins = []\n\
    trusted_proxies = []\n\
    # Canonical HTTPS DNS origin for passkeys; omit to bind it at first registration.\n\
    # public_origin = \"https://bridge.example.test\"\n\
    # Directory with the built web interface; default: web next to the executable, then\n\
    # /usr/share/bridge-coordinator/web.\n\
    # web_dir = \"/path/to/web\"\n\
    # Total concurrent TCP connections; excess connections are closed immediately.\n\
    max_connections = {}\n\
    # Concurrent connections from one client address (IPv6 clients by /64).\n\
    max_connections_per_ip = {}\n\
    # Connection slots kept for workers with a valid token when the others are full.\n\
    worker_reserve_connections = {}\n\
    # Seconds to receive a complete request body (slow-body limit).\n\
    request_body_timeout_secs = {}\n\
    # Seconds to wait for the next request header on an idle HTTP connection.\n\
    http_idle_timeout_secs = {}\n\
    # Browser WebSockets (/stream, /terminal): server ping interval and inactivity close.\n\
    ws_ping_interval_secs = {}\n\
    ws_idle_timeout_secs = {}\n\
    # Authenticated mutation budget per token and client address per rolling 60 seconds.\n\
    mutation_limit_per_token = {}\n\
    mutation_limit_per_address = {}\n\
    # Maximum lifetime for newly created API tokens, in days (up to ten years).\n\
    max_token_lifetime_days = {}\n",
        d.min_tls,
        d.max_events_per_worker,
        d.max_connections,
        d.max_connections_per_ip,
        d.worker_reserve_connections,
        d.request_body_timeout_secs,
        d.http_idle_timeout_secs,
        d.ws_ping_interval_secs,
        d.ws_idle_timeout_secs,
        d.mutation_limit_per_token,
        d.mutation_limit_per_address,
        d.max_token_lifetime_days
    )
}

/// Persist the first completed passkey registration's canonical origin in `config.toml` when the
/// operator did not preconfigure one. The database is still authoritative during the live
/// process; this keeps the effective RP ID visible and stable across upgrades and restores.
pub fn persist_public_origin(dir: &Path, origin: &str) -> Result<(), String> {
    let canonical = crate::passkeys::canonical_origin(origin)?;
    if canonical != origin {
        return Err("passkey origin is not canonical".into());
    }
    let path = dir.join("config.toml");
    let text = std::fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let value: toml::Value = toml::from_str(&text).map_err(|e| format!("invalid {}: {e}", path.display()))?;
    if let Some(existing) = value.get("public_origin").and_then(toml::Value::as_str) {
        return if existing == origin {
            Ok(())
        } else {
            Err("configured public_origin does not match the registered passkey origin".into())
        };
    }
    let mut updated = text;
    if !updated.ends_with('\n') {
        updated.push('\n');
    }
    updated.push_str(&format!(
        "\n# Fixed by the first passkey registration. Re-enrol passkeys before changing this RP ID.\npublic_origin = \"{origin}\"\n"
    ));
    crate::write_replace(&path, updated.as_bytes(), 0o600).map_err(|e| format!("cannot persist public_origin: {e}"))
}

/// Parse a boolean environment value (`1/0`, `true/false`, `yes/no`, `on/off`).
pub fn parse_bool(name: &str, v: &str) -> Result<bool, String> {
    match v.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(format!("{name} must be true or false, not {v:?}")),
    }
}

impl Config {
    pub fn load(dir: &Path) -> Result<Config, String> {
        let path = dir.join("config.toml");
        let text = std::fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e} (run `init` first)", path.display()))?;
        let mut cfg: Config = toml::from_str(&text).map_err(|e| format!("invalid {}: {e}", path.display()))?;
        // Environment variables win over config.toml (container deployments).
        if let Some(v) = std::env::var_os(ENV_BIND) {
            cfg.bind = v.to_string_lossy().into_owned();
        }
        if let Some(v) = std::env::var_os(ENV_LAN_ENABLED) {
            cfg.lan_enabled = parse_bool(ENV_LAN_ENABLED, &v.to_string_lossy())?;
        }
        if cfg.min_tls != "1.3" && cfg.min_tls != "1.2" {
            return Err("min_tls must be \"1.3\" or \"1.2\"".into());
        }
        if let Some(v) = std::env::var("BRIDGE_COORDINATOR_TLS_CERT_FILE").ok().filter(|v| !v.trim().is_empty()) {
            cfg.tls_cert_file = Some(v);
        }
        if let Some(v) = std::env::var("BRIDGE_COORDINATOR_TLS_KEY_FILE").ok().filter(|v| !v.trim().is_empty()) {
            cfg.tls_key_file = Some(v);
        }
        if let Some(v) = std::env::var("BRIDGE_COORDINATOR_TLS_CERT_NAME").ok().filter(|v| !v.trim().is_empty()) {
            cfg.tls_cert_name = Some(v);
        }
        let file_provider_fields = [cfg.tls_cert_file.is_some(), cfg.tls_key_file.is_some(), cfg.tls_cert_name.is_some()];
        if file_provider_fields.iter().any(|set| *set) && !file_provider_fields.iter().all(|set| *set) {
            return Err("tls_cert_file, tls_key_file and tls_cert_name must be configured together".into());
        }
        if let Some(origin) = &cfg.public_origin {
            let canonical = crate::passkeys::canonical_origin(origin)?;
            if canonical != *origin {
                return Err(format!("public_origin must use its canonical form {canonical:?}"));
            }
        }
        if cfg.max_events_per_worker == 0 {
            return Err("max_events_per_worker must be at least 1".into());
        }
        if cfg.trusted_proxies.len() > 64 || cfg.trusted_proxies.iter().any(|cidr| !crate::client_ip::valid_cidr(cidr)) {
            return Err("trusted_proxies must contain at most 64 valid IPv4 or IPv6 CIDRs".into());
        }
        if cfg.max_connections == 0
            || cfg.max_connections_per_ip == 0
            || cfg.request_body_timeout_secs == 0
            || cfg.http_idle_timeout_secs == 0
            || cfg.ws_ping_interval_secs == 0
        {
            return Err(
                "max_connections, max_connections_per_ip, request_body_timeout_secs, http_idle_timeout_secs and ws_ping_interval_secs must be at least 1"
                    .into(),
            );
        }
        if cfg.ws_idle_timeout_secs <= cfg.ws_ping_interval_secs {
            return Err("ws_idle_timeout_secs must be larger than ws_ping_interval_secs".into());
        }
        if !(1..=10_000).contains(&cfg.mutation_limit_per_token) || !(1..=10_000).contains(&cfg.mutation_limit_per_address) {
            return Err("mutation limits must be between 1 and 10000 per 60 seconds".into());
        }
        if !(1..=3650).contains(&cfg.max_token_lifetime_days) {
            return Err("max_token_lifetime_days must be between 1 and 3650".into());
        }
        cfg.listener_binds()?;
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

    pub fn listener_binds(&self) -> Result<Vec<(ListenerGroup, SocketAddr)>, String> {
        let groups = [
            (ListenerGroup::ConsoleRest, self.listener_groups.console_rest.as_deref()),
            (ListenerGroup::WorkerChannel, self.listener_groups.worker_channel.as_deref()),
            (ListenerGroup::Health, self.listener_groups.health.as_deref()),
        ];
        if groups.iter().all(|(_, address)| address.is_none()) {
            return Ok(vec![(ListenerGroup::Combined, self.checked_bind()?)]);
        }
        let mut binds = Vec::new();
        for (group, address) in groups {
            if let Some(address) = address {
                let addr = address
                    .parse::<SocketAddr>()
                    .map_err(|e| format!("invalid listener address {address:?}: {e}"))?;
                if !addr.ip().is_loopback() && !self.lan_enabled {
                    return Err(format!(
                        "refusing to bind {addr}: it is not a loopback address and lan_enabled is not set to true in config.toml"
                    ));
                }
                binds.push((group, addr));
            }
        }
        Ok(binds)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_config(text: &str) -> std::path::PathBuf {
        let nonce = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let dir = std::env::temp_dir().join(format!("bridge-config-test-{}-{nonce}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("config.toml"), text).unwrap();
        dir
    }

    #[test]
    fn mutation_limits_default_and_are_bounded() {
        let defaults = Config::default();
        assert_eq!((defaults.mutation_limit_per_token, defaults.mutation_limit_per_address), (30, 120));

        let valid = temp_config(&config_text("127.0.0.1:8443", false));
        assert_eq!(Config::load(&valid).unwrap().mutation_limit_per_token, 30);
        let _ = std::fs::remove_dir_all(valid);

        let invalid = temp_config(&config_text("127.0.0.1:8443", false).replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 0"));
        assert!(Config::load(&invalid).unwrap_err().contains("mutation limits"));
        let _ = std::fs::remove_dir_all(invalid);
    }

    #[test]
    fn trusted_proxy_configuration_is_bounded_and_requires_cidrs() {
        let valid = temp_config(&config_text("127.0.0.1:8443", false).replace("trusted_proxies = []", "trusted_proxies = [\"10.0.0.0/8\", \"2001:db8::/32\"]"));
        assert_eq!(Config::load(&valid).unwrap().trusted_proxies.len(), 2);
        std::fs::remove_dir_all(valid).unwrap();

        let invalid = temp_config(&config_text("127.0.0.1:8443", false).replace("trusted_proxies = []", "trusted_proxies = [\"10.0.0.1\"]"));
        assert!(Config::load(&invalid).unwrap_err().contains("valid IPv4 or IPv6 CIDRs"));
        std::fs::remove_dir_all(invalid).unwrap();
    }

    #[test]
    fn listener_groups_default_to_combined_and_enable_only_explicit_groups_when_split() {
        let defaults = Config::default().listener_binds().unwrap();
        assert_eq!(defaults, vec![(ListenerGroup::Combined, "127.0.0.1:8443".parse().unwrap())]);

        let dir = temp_config(&format!(
            "{}\n[listener_groups]\nworker_channel = \"127.0.0.1:9444\"\nhealth = \"127.0.0.1:9445\"\n",
            config_text("127.0.0.1:8443", false)
        ));
        let config = Config::load(&dir).unwrap();
        assert_eq!(
            config.listener_binds().unwrap(),
            vec![
                (ListenerGroup::WorkerChannel, "127.0.0.1:9444".parse().unwrap()),
                (ListenerGroup::Health, "127.0.0.1:9445".parse().unwrap()),
            ]
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn api_token_lifetime_default_is_ten_years_and_config_is_bounded() {
        assert_eq!(Config::default().max_token_lifetime_days, 3650);
        let valid = temp_config(&config_text("127.0.0.1:8443", false).replace("max_token_lifetime_days = 3650", "max_token_lifetime_days = 30"));
        assert_eq!(Config::load(&valid).unwrap().max_token_lifetime_days, 30);
        std::fs::remove_dir_all(valid).unwrap();

        let invalid = temp_config(&config_text("127.0.0.1:8443", false).replace("max_token_lifetime_days = 3650", "max_token_lifetime_days = 0"));
        assert!(Config::load(&invalid).unwrap_err().contains("max_token_lifetime_days"));
        std::fs::remove_dir_all(invalid).unwrap();
    }

    #[test]
    fn first_passkey_origin_is_persisted_and_ip_origins_are_refused() {
        let dir = temp_config(&config_text("127.0.0.1:8443", false));
        persist_public_origin(&dir, "https://bridge.example.test").unwrap();
        let config = Config::load(&dir).unwrap();
        assert_eq!(config.public_origin.as_deref(), Some("https://bridge.example.test"));
        assert!(persist_public_origin(&dir, "https://other.example.test").is_err());
        std::fs::remove_dir_all(&dir).unwrap();

        let dir = temp_config(&format!(
            "{}\npublic_origin = \"https://192.0.2.1:8443\"\n",
            config_text("127.0.0.1:8443", false)
        ));
        assert!(Config::load(&dir).unwrap_err().contains("HTTPS DNS origin"));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
