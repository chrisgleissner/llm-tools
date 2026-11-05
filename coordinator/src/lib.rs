//! LLM Bridge coordinator library. The binary in `main.rs` is a thin CLI over it,
//! and the integration tests start the real server through [`server::start`].

pub mod auth;
pub mod certs;
pub mod config;
pub mod db;
pub mod http;
pub mod hub;
pub mod init;
pub mod protocol;
pub mod server;
pub mod web;
pub mod ws_worker;

use std::path::{Path, PathBuf};

pub const DB_FILE: &str = "coordinator.sqlite3";
pub const SETUP_TOKEN_FILE: &str = "setup-token";

pub fn db_path(dir: &Path) -> PathBuf {
    dir.join(DB_FILE)
}

pub fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Write `contents` to a new file with the given mode; never overwrites. Returns false if it exists.
pub fn write_new(path: &Path, contents: &[u8], mode: u32) -> std::io::Result<bool> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    match std::fs::OpenOptions::new().write(true).create_new(true).mode(mode).open(path) {
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
    use std::os::unix::fs::OpenOptionsExt;
    let tmp = path.with_extension("tmp-new");
    let _ = std::fs::remove_file(&tmp);
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(mode).open(&tmp)?;
    f.write_all(contents)?;
    f.sync_all()?;
    std::fs::rename(&tmp, path)
}
