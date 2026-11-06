//! LLM Bridge coordinator library. The binary in `main.rs` is a thin CLI over it,
//! and the integration tests start the real server through [`server::start`].

pub mod auth;
pub mod certs;
pub mod config;
pub mod db;
pub mod http;
pub mod hub;
pub mod init;
pub mod netinfo;
pub mod ops;
pub mod protocol;
pub mod server;
pub mod terminal;
pub mod web;
pub mod ws_worker;

use std::path::{Path, PathBuf};

pub const DB_FILE: &str = "coordinator.sqlite3";
pub const SETUP_TOKEN_FILE: &str = "setup-token";

pub fn db_path(dir: &Path) -> PathBuf {
    dir.join(DB_FILE)
}

/// Current UTC time as `YYYYMMDDTHHMMSSZ` (file name suffixes).
pub fn utc_stamp() -> String {
    let t = now();
    let (y, m, d) = certs::civil_from_days(t.div_euclid(86_400));
    let s = t.rem_euclid(86_400);
    format!("{y:04}{m:02}{d:02}T{:02}{:02}{:02}Z", s / 3600, s % 3600 / 60, s % 60)
}

pub fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// `OpenOptions` whose created files get `mode` on Unix. Windows has no permission bits: files are
/// created normally and the access control list of the data directory is the protection.
#[cfg(unix)]
pub fn private_options(mode: u32) -> std::fs::OpenOptions {
    use std::os::unix::fs::OpenOptionsExt;
    let mut options = std::fs::OpenOptions::new();
    options.mode(mode);
    options
}

#[cfg(not(unix))]
pub fn private_options(_mode: u32) -> std::fs::OpenOptions {
    std::fs::OpenOptions::new()
}

/// Restrict `path` to its owner (`mode`, for example 0o700 or 0o600). Best effort, and a no-op on
/// Windows, where the data directory's access control list applies.
pub fn restrict_permissions(path: &Path, mode: u32) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode));
    }
    #[cfg(not(unix))]
    let _ = (path, mode);
}

/// Write `contents` to a new file with the given mode; never overwrites. Returns false if it exists.
pub fn write_new(path: &Path, contents: &[u8], mode: u32) -> std::io::Result<bool> {
    use std::io::Write;
    match private_options(mode).write(true).create_new(true).open(path) {
        Ok(mut f) => {
            f.write_all(contents)?;
            f.sync_all()?;
            Ok(true)
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(e) => Err(e),
    }
}

/// Atomically replace a file (write temp with mode, rename).
pub fn write_replace(path: &Path, contents: &[u8], mode: u32) -> std::io::Result<()> {
    use std::io::Write;
    let tmp = path.with_extension("tmp-new");
    let _ = std::fs::remove_file(&tmp);
    let mut f = private_options(mode).write(true).create_new(true).open(&tmp)?;
    f.write_all(contents)?;
    f.sync_all()?;
    std::fs::rename(&tmp, path)
}
