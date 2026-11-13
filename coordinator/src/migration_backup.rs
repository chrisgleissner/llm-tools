//! Backup before a schema migration, and refusal of a database from a newer coordinator (PRD-003).
use crate::config::Config;
use rusqlite::{Connection, OpenFlags};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// `PRAGMA user_version` read through a read-only connection, without migration or pragma writes.
/// Returns `None` when the file does not exist or cannot be read as SQLite.
pub fn stored_version(db_file: &Path) -> Option<i64> {
    let c = Connection::open_with_flags(db_file, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
    c.busy_timeout(Duration::from_secs(5)).ok()?;
    c.query_row("PRAGMA user_version", [], |r| r.get(0)).ok()
}

/// Newest regular, non-symlink `coordinator-*.sqlite3` or `pre-migration-v*.sqlite3` by modification time.
pub fn newest_backup(backup_dir: &Path) -> Option<PathBuf> {
    std::fs::read_dir(backup_dir)
        .ok()?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name();
            let name = name.to_str()?;
            if !name.ends_with(".sqlite3") || !(name.starts_with("coordinator-") || name.starts_with("pre-migration-v")) {
                return None;
            }
            let meta = std::fs::symlink_metadata(entry.path()).ok()?;
            if !meta.is_file() {
                return None;
            }
            Some((meta.modified().ok()?, entry.path()))
        })
        .max_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)))
        .map(|(_, path)| path)
}

/// Refuse a database written by a newer coordinator, with recovery instructions and the newest backup.
pub fn check_not_newer(data_dir: &Path, cfg: &Config, db_file: &Path) -> Result<(), String> {
    let Some(version) = stored_version(db_file).filter(|v| *v > crate::db::SCHEMA_VERSION) else {
        return Ok(());
    };
    let backup_dir = cfg.backup_directory(data_dir);
    let backup = match newest_backup(&backup_dir) {
        Some(path) => format!("newest backup: {}", path.display()),
        None => format!("no backup was found in {}", backup_dir.display()),
    };
    Err(format!(
        "database schema version {version} is newer than this binary supports ({}); \
         install a coordinator that supports it, or restore a backup made by this version with \
         `bridge-coordinator restore --data-dir {} --input <backup>`; {backup}",
        crate::db::SCHEMA_VERSION,
        data_dir.display()
    ))
}

/// Back up an existing nonzero older schema before migration, without rotating or deleting backups.
/// `db_file` must be `crate::db_path(data_dir)`, the source used by `ops::backup_with`.
pub fn backup_before_migration(data_dir: &Path, cfg: &Config, db_file: &Path, now: i64) -> Result<Option<PathBuf>, String> {
    let Some(from) = stored_version(db_file).filter(|v| 0 < *v && *v < crate::db::SCHEMA_VERSION) else {
        return Ok(None);
    };
    let to = crate::db::SCHEMA_VERSION;
    let result = (|| {
        let dir = cfg.backup_directory(data_dir);
        crate::backups::ensure_dir(&dir)?;
        let stem = format!("pre-migration-v{from}-to-v{to}-{}", crate::utc_stamp_at(now));
        let mut target = dir.join(format!("{stem}.sqlite3"));
        let mut sequence = 1;
        while target.try_exists().map_err(|e| e.to_string())? || std::fs::symlink_metadata(&target).is_ok() {
            target = dir.join(format!("{stem}-{sequence}.sqlite3"));
            sequence += 1;
        }
        crate::ops::backup_with(
            data_dir,
            &target,
            &crate::ops::BackupOptions {
                force: true,
                ..Default::default()
            },
        )?;
        Ok::<_, String>(target)
    })();
    let target = result.map_err(|e| format!("cannot back up the database before migrating it: {e}"))?;
    eprintln!(
        "bridge-coordinator: database schema {from} will be migrated to {to}; backup written to {}",
        target.display()
    );
    Ok(Some(target))
}
