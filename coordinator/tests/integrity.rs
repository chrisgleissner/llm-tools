//! FIND-002: the coordinator checks the database file when it starts and does not serve from a damaged one.
mod common;
use bridge_coordinator::{db::Db, db_path, server};
use common::*;

/// Fill the audit table so that the file has several b-tree pages, then close it (the WAL is checkpointed).
fn populated_data_dir() -> std::path::PathBuf {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    edit_config(&dir, |t| t.replace("8443", "0"));
    let db = Db::open(&db_path(&dir)).unwrap();
    for i in 0..400 {
        db.audit("cli", "test.fill", &format!("row {i} {}", "x".repeat(200)), "local");
    }
    drop(db);
    dir
}

fn damage(dir: &std::path::Path) {
    let file = db_path(dir);
    let mut data = std::fs::read(&file).unwrap();
    for page in 1..data.len() / 4096 {
        data[page * 4096..page * 4096 + 8].fill(0xff);
    }
    std::fs::write(&file, data).unwrap();
    for suffix in ["-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{suffix}", file.display()));
    }
}

#[tokio::test]
async fn a_damaged_database_is_refused_at_start_with_the_restore_step() {
    let dir = populated_data_dir();
    // An older schema version makes Db::open run migrations, which write to the file.
    rusqlite::Connection::open(db_path(&dir))
        .unwrap()
        .execute_batch("PRAGMA user_version = 3; PRAGMA wal_checkpoint(TRUNCATE);")
        .unwrap();
    damage(&dir);
    let before = std::fs::read(db_path(&dir)).unwrap();
    let error = match server::start(&dir).await {
        Ok(_) => panic!("the coordinator started on a database that fails quick_check"),
        Err(e) => e,
    };
    assert!(error.contains("integrity check failed"), "{error}");
    assert!(error.contains("bridge-coordinator restore --data-dir"), "{error}");
    assert!(error.contains("will not serve"), "{error}");
    // The refusal happens before Db::open sets the journal mode or migrates: the damaged file is not written to.
    assert!(
        std::fs::read(db_path(&dir)).unwrap() == before,
        "the damaged database file was modified before the refusal"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn an_intact_database_starts() {
    let dir = populated_data_dir();
    let srv = server::start(&dir).await.expect("an intact database starts");
    srv.stop().await;
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn quick_check_reports_nothing_for_an_intact_file_and_the_problems_for_a_damaged_one() {
    let dir = populated_data_dir();
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.quick_check(5).unwrap().is_empty());
    drop(db);
    damage(&dir);
    let found = Db::open(&db_path(&dir)).and_then(|db| db.quick_check(5));
    assert!(found.map(|p| !p.is_empty()).unwrap_or(true));
    let _ = std::fs::remove_dir_all(&dir);
}
