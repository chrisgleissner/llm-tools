//! Historical schema fixtures must migrate without losing durable rows (PRD-003).
mod common;

use bridge_coordinator::config::Config;
use bridge_coordinator::db::{Db, SCHEMA_VERSION};
use bridge_coordinator::{db_path, migration_backup};
use rusqlite::{Connection, OpenFlags};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/schema")
}

fn fixture(version: i64) -> PathBuf {
    fixture_dir().join(format!("schema-v{version}.sqlite3"))
}

fn versions() -> BTreeSet<i64> {
    let manifest =
        std::fs::read(fixture_dir().join("MANIFEST.json")).expect("add a fixture with scripts/make_coordinator_schema_fixtures.py: missing manifest");
    let manifest: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
    manifest["fixtures"].as_object().unwrap().keys().map(|v| v.parse().unwrap()).collect()
}

fn copy_fixture(version: i64) -> PathBuf {
    let dir = common::tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::copy(fixture(version), db_path(&dir)).unwrap_or_else(|e| panic!("schema {version}: cannot copy fixture: {e}"));
    dir
}

fn read_only(path: &Path) -> Connection {
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).unwrap()
}

fn number(c: &Connection, sql: &str, version: i64) -> i64 {
    c.query_row(sql, [], |r| r.get(0)).unwrap_or_else(|e| panic!("schema {version}: {sql}: {e}"))
}

fn text(c: &Connection, sql: &str, version: i64) -> String {
    c.query_row(sql, [], |r| r.get(0)).unwrap_or_else(|e| panic!("schema {version}: {sql}: {e}"))
}

#[test]
fn fixtures_cover_every_schema_version_that_shipped() {
    let actual = versions();
    let expected: BTreeSet<_> = (1..=SCHEMA_VERSION).filter(|v| *v != 15).collect();
    assert_eq!(actual, expected, "add a fixture with scripts/make_coordinator_schema_fixtures.py");
    for version in actual {
        assert!(
            fixture(version).is_file(),
            "schema {version}: add a fixture with scripts/make_coordinator_schema_fixtures.py"
        );
    }
}

#[test]
fn every_fixture_migrates_to_the_current_schema_and_keeps_its_rows() {
    for version in versions() {
        let dir = copy_fixture(version);
        let path = db_path(&dir);
        assert_eq!(migration_backup::stored_version(&path), Some(version), "schema {version}: fixture version");
        drop(Db::open(&path).unwrap_or_else(|e| panic!("schema {version}: migration failed: {e}")));
        {
            let c = read_only(&path);
            assert_eq!(number(&c, "PRAGMA user_version", version), SCHEMA_VERSION, "schema {version}: migrated version");
            assert_eq!(text(&c, "PRAGMA integrity_check", version), "ok", "schema {version}: integrity");
            for (sql, expected) in [
                (
                    "SELECT COUNT(*) FROM workers WHERE name='seed-worker' AND token_hash='seed-worker-token-hash'",
                    1,
                ),
                ("SELECT COUNT(*) FROM missions WHERE worker='seed-worker' AND mission_id='mission-seed-1'", 1),
                ("SELECT COUNT(*) FROM events WHERE worker='seed-worker'", 2),
                ("SELECT COUNT(*) FROM audit WHERE actor='seed-admin'", 1),
            ] {
                assert_eq!(number(&c, sql, version), expected, "schema {version}: {sql}");
            }
            assert_eq!(
                text(&c, "SELECT username FROM admin WHERE id=1", version),
                "seed-admin",
                "schema {version}: admin username"
            );
            assert_eq!(
                text(&c, "SELECT password_hash FROM admin WHERE id=1", version),
                "seed-password-hash",
                "schema {version}: admin password hash"
            );
            assert_eq!(
                text(&c, "SELECT token_hash FROM api_tokens WHERE name='seed-token'", version),
                "seed-api-token-hash",
                "schema {version}: API token hash"
            );
        }
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn reopening_a_migrated_database_changes_nothing() {
    let dir = copy_fixture(1);
    let path = db_path(&dir);
    drop(Db::open(&path).unwrap());
    let before = text(&read_only(&path), "SELECT group_concat(sql) FROM sqlite_master", 1);
    drop(Db::open(&path).unwrap());
    let after = text(&read_only(&path), "SELECT group_concat(sql) FROM sqlite_master", 1);
    assert_eq!(before, after, "schema 1: reopening changed the migrated schema");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn backup_is_taken_before_migrating_an_older_database() {
    let dir = copy_fixture(8);
    let path = db_path(&dir);
    let cfg = Config::default();
    let backup = migration_backup::backup_before_migration(&dir, &cfg, &path, 1_800_000_000).unwrap().unwrap();
    assert_eq!(backup.parent(), Some(dir.join("backups").as_path()));
    let name = backup.file_name().unwrap().to_str().unwrap();
    assert!(name.starts_with(&format!("pre-migration-v8-to-v{SCHEMA_VERSION}-")) && name.ends_with(".sqlite3"));
    {
        let c = read_only(&backup);
        assert_eq!(number(&c, "PRAGMA user_version", 8), 8);
        assert_eq!(
            number(
                &c,
                "SELECT COUNT(*) FROM workers WHERE name='seed-worker' AND token_hash='seed-worker-token-hash'",
                8
            ),
            1
        );
    }
    assert_eq!(
        migration_backup::stored_version(&path),
        Some(8),
        "the helper must not migrate the live database"
    );
    let second = migration_backup::backup_before_migration(&dir, &cfg, &path, 1_800_000_000).unwrap().unwrap();
    assert_ne!(backup, second);
    assert_eq!(
        second.file_name().unwrap().to_str().unwrap(),
        format!("{}-1.sqlite3", name.strip_suffix(".sqlite3").unwrap())
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn no_backup_when_the_database_is_current_or_missing() {
    let dir = copy_fixture(SCHEMA_VERSION);
    let path = db_path(&dir);
    let cfg = Config::default();
    drop(Db::open(&path).unwrap());
    assert_eq!(migration_backup::backup_before_migration(&dir, &cfg, &path, 1_800_000_000).unwrap(), None);
    assert!(!dir.join("backups").exists());
    std::fs::remove_dir_all(&dir).unwrap();
    assert_eq!(migration_backup::backup_before_migration(&dir, &cfg, &path, 1_800_000_000).unwrap(), None);
    assert!(!dir.exists(), "a missing database must not create any directories");
}

#[test]
fn a_newer_database_is_refused_and_names_the_newest_backup() {
    let dir = copy_fixture(SCHEMA_VERSION);
    let path = db_path(&dir);
    let cfg = Config::default();
    let newer = SCHEMA_VERSION + 1;
    Connection::open(&path).unwrap().pragma_update(None, "user_version", newer).unwrap();
    let err = migration_backup::check_not_newer(&dir, &cfg, &path).unwrap_err();
    assert!(
        err.contains(&newer.to_string()) && err.contains(&SCHEMA_VERSION.to_string()) && err.contains("no backup was found"),
        "{err}"
    );
    let backups = dir.join("backups");
    std::fs::create_dir_all(&backups).unwrap();
    let backup = backups.join("coordinator-20260101T000000Z.sqlite3");
    std::fs::write(&backup, b"backup").unwrap();
    let err = migration_backup::check_not_newer(&dir, &cfg, &path).unwrap_err();
    assert!(err.contains(backup.to_str().unwrap()) && err.contains("bridge-coordinator restore"), "{err}");
    assert!(Db::open(&path).is_err(), "Db::open must also refuse schema {newer}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn an_old_database_is_not_changed_by_the_read_only_version_probe() {
    let path = fixture(3);
    let before = std::fs::read(&path).unwrap();
    for _ in 0..2 {
        assert_eq!(migration_backup::stored_version(&path), Some(3), "schema 3: read-only probe version");
        assert_eq!(std::fs::read(&path).unwrap(), before, "schema 3: read-only probe changed fixture bytes");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn starting_on_an_old_database_takes_a_backup_first_and_migrates() {
    let dir = common::tmp_dir();
    bridge_coordinator::init::init_with(&dir, &common::sans(), None).unwrap();
    common::edit_config(&dir, |t| t.replace("8443", "0"));
    std::fs::copy(fixture(8), db_path(&dir)).unwrap();

    let srv = bridge_coordinator::server::start(&dir).await.unwrap();
    let prefix = format!("pre-migration-v8-to-v{SCHEMA_VERSION}-");
    let backups: Vec<_> = std::fs::read_dir(dir.join("backups"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            let name = path.file_name().unwrap().to_str().unwrap();
            name.starts_with(&prefix) && name.ends_with(".sqlite3")
        })
        .collect();
    assert_eq!(backups.len(), 1, "expected one pre-migration backup: {backups:?}");
    {
        let c = read_only(&backups[0]);
        assert_eq!(number(&c, "PRAGMA user_version", 8), 8);
        assert_eq!(
            number(
                &c,
                "SELECT COUNT(*) FROM workers WHERE name='seed-worker' AND token_hash='seed-worker-token-hash'",
                8
            ),
            1
        );
    }
    assert_eq!(number(&read_only(&db_path(&dir)), "PRAGMA user_version", SCHEMA_VERSION), SCHEMA_VERSION);
    tokio::time::timeout(std::time::Duration::from_secs(10), srv.stop())
        .await
        .expect("shutdown within 10 s");
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn starting_on_a_newer_database_is_refused_naming_the_backup() {
    let dir = common::tmp_dir();
    bridge_coordinator::init::init_with(&dir, &common::sans(), None).unwrap();
    common::edit_config(&dir, |t| t.replace("8443", "0"));
    let path = db_path(&dir);
    std::fs::copy(fixture(SCHEMA_VERSION), &path).unwrap();
    Connection::open(&path)
        .unwrap()
        .pragma_update(None, "user_version", SCHEMA_VERSION + 1)
        .unwrap();
    let backups = dir.join("backups");
    std::fs::create_dir_all(&backups).unwrap();
    std::fs::write(backups.join("coordinator-20260101T000000Z.sqlite3"), b"backup").unwrap();

    let err = match bridge_coordinator::server::start(&dir).await {
        Err(err) => err,
        Ok(srv) => {
            tokio::time::timeout(std::time::Duration::from_secs(10), srv.stop())
                .await
                .expect("shutdown within 10 s");
            panic!("a database newer than this binary supports must be refused");
        }
    };
    assert!(err.contains("newer than"), "{err}");
    assert!(err.contains("bridge-coordinator restore"), "{err}");
    assert!(err.contains("coordinator-20260101T000000Z.sqlite3"), "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn starting_on_a_current_database_takes_no_migration_backup() {
    let dir = common::tmp_dir();
    bridge_coordinator::init::init_with(&dir, &common::sans(), None).unwrap();
    common::edit_config(&dir, |t| t.replace("8443", "0"));
    let srv = bridge_coordinator::server::start(&dir).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(10), srv.stop())
        .await
        .expect("shutdown within 10 s");
    let backups = dir.join("backups");
    if backups.exists() {
        for entry in std::fs::read_dir(backups).unwrap() {
            let entry = entry.unwrap();
            assert!(!entry.file_name().to_str().unwrap().starts_with("pre-migration-"));
        }
    }
    std::fs::remove_dir_all(dir).unwrap();
}
