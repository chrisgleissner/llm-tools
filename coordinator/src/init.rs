//! `init`: data directory, database, CA, server certificate, config and setup token.

use crate::certs::{self, Sans};
use crate::config::DEFAULT_CONFIG_TEXT;
use crate::db::Db;
use crate::{auth, db_path, write_new, SETUP_TOKEN_FILE};

pub fn init(dir: &std::path::Path, sans: &Sans) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    write_new(&dir.join("config.toml"), DEFAULT_CONFIG_TEXT.as_bytes(), 0o600).map_err(|e| e.to_string())?;
    let db = Db::open(&db_path(dir)).map_err(|e| e.to_string())?;
    certs::init_certs(dir, sans)?;
    // A setup token is only issued while no administrator exists.
    if db.admin_get().map_err(|e| e.to_string())?.is_none() {
        let created = write_new(&dir.join(SETUP_TOKEN_FILE), format!("{}\n", auth::random_token()).as_bytes(), 0o600).map_err(|e| e.to_string())?;
        if created {
            println!("setup token written to {}", dir.join(SETUP_TOKEN_FILE).display());
        }
    }
    println!("initialised {}", dir.display());
    println!("CA certificate for workers and browsers: {}", dir.join("ca.pem").display());
    Ok(())
}
