use bridge_coordinator::db::{ApiTokenCreate, Db};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

fn db_path() -> PathBuf {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "bridge-principal-{}-{}-{}.sqlite",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn sessions_tokens_and_audit_rows_belong_to_a_stable_principal() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    db.admin_create("operator", "hash").unwrap();
    db.session_create_with("session-hash", "csrf", true).unwrap();
    db.token_create_identified_with_restrictions(ApiTokenCreate {
        name: "automation",
        token_id: "token-id",
        secret_hash: "secret-hash",
        principal_id: "admin",
        expires_at: None,
        scopes: &["read".to_string()],
        allowed_cidrs: &[],
        allowed_workers: &[],
    })
    .unwrap();
    db.audit_checked("operator", "test", "", "127.0.0.1").unwrap();

    let conn = rusqlite::Connection::open(&path).unwrap();
    let session_principal: String = conn
        .query_row("SELECT principal_id FROM sessions WHERE token_hash='session-hash'", [], |r| r.get(0))
        .unwrap();
    let token_principal: String = conn
        .query_row("SELECT principal_id FROM api_tokens WHERE name='automation'", [], |r| r.get(0))
        .unwrap();
    let audit_principal: String = conn.query_row("SELECT principal_id FROM audit WHERE action='test'", [], |r| r.get(0)).unwrap();
    let sender_binding: (Option<String>, Option<String>) = conn
        .query_row(
            "SELECT sender_constraint_kind,sender_constraint_id FROM api_tokens WHERE name='automation'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(session_principal, "admin");
    assert_eq!(token_principal, "admin");
    assert_eq!(audit_principal, "admin");
    assert_eq!(sender_binding, (None, None));
    assert_eq!(db.principals_list().unwrap()[0]["id"], "admin");

    drop(conn);
    drop(db);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_file(path.with_extension("sqlite-shm"));
    let _ = std::fs::remove_file(path.with_extension("sqlite-wal"));
}
