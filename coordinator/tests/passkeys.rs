mod common;

use bridge_coordinator::db::{Db, WebauthnChallengeRow, SCHEMA_VERSION};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

fn db_path() -> PathBuf {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "bridge-passkeys-{}-{}-{}.sqlite",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ))
}

fn remove_database(path: &std::path::Path) {
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(path.with_extension("sqlite-shm"));
    let _ = std::fs::remove_file(path.with_extension("sqlite-wal"));
}

#[test]
fn migrations_add_passkey_tables_and_challenge_is_session_bound_single_use() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    assert_eq!(SCHEMA_VERSION, 16);
    let now = bridge_coordinator::now();
    let state = WebauthnChallengeRow {
        principal_id: "admin".into(),
        binding: "session-a".into(),
        kind: "registration".into(),
        state: "{}".into(),
        expires_at: now + 300,
    };
    db.webauthn_challenge_put("challenge-1", &state).unwrap();
    assert!(db
        .webauthn_challenge_take("challenge-1", "admin", "session-b", "registration")
        .unwrap()
        .is_none());
    assert_eq!(
        db.webauthn_challenge_take("challenge-1", "admin", "session-a", "registration").unwrap(),
        Some(state.clone())
    );
    assert!(db
        .webauthn_challenge_take("challenge-1", "admin", "session-a", "registration")
        .unwrap()
        .is_none());

    let state = WebauthnChallengeRow {
        expires_at: bridge_coordinator::now() + 299,
        ..state
    };
    db.webauthn_challenge_put("challenge-2", &state).unwrap();
    assert_eq!(
        db.webauthn_challenge_take("challenge-2", "admin", "session-a", "registration").unwrap(),
        Some(state)
    );
    assert!(db
        .webauthn_challenge_take("challenge-2", "admin", "session-a", "registration")
        .unwrap()
        .is_none());
    drop(db);
    remove_database(&path);
}

#[test]
fn schema_10_tokens_migrate_with_empty_last_use_addresses() {
    let path = db_path();
    let conn = rusqlite::Connection::open(&path).unwrap();
    conn.execute_batch(
        "CREATE TABLE api_tokens(
            name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
            last_used INTEGER, scopes TEXT NOT NULL, expires_at INTEGER, token_id TEXT);
         INSERT INTO api_tokens(name, token_hash, created_at, last_used, scopes, expires_at, token_id)
            VALUES('legacy', 'hash', 10, 20, '[\"read\"]', 30, '0123456789abcdef');
         PRAGMA user_version = 10;",
    )
    .unwrap();
    drop(conn);

    let db = Db::open(&path).unwrap();
    let tokens = db.api_tokens_list().unwrap();
    assert_eq!(tokens.len(), 1);
    assert_eq!(tokens[0].name, "legacy");
    assert_eq!(tokens[0].token_id.as_deref(), Some("0123456789abcdef"));
    assert_eq!(tokens[0].last_used, Some(20));
    assert_eq!(tokens[0].last_address, None);
    assert_eq!(tokens[0].expires_at, Some(30));
    remove_database(&path);
}

#[test]
fn challenge_insert_rejects_lifetime_over_five_minutes() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    let now = bridge_coordinator::now();
    let row = WebauthnChallengeRow {
        principal_id: "admin".into(),
        binding: "setup".into(),
        kind: "authentication".into(),
        state: "{}".into(),
        expires_at: now + 301,
    };
    assert!(db.webauthn_challenge_put("too-long", &row).is_err());
    drop(db);
    remove_database(&path);
}

#[test]
fn canonical_passkey_origin_is_bound_once_and_cannot_be_changed() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    assert_eq!(db.passkey_origin("admin").unwrap(), None);
    let row = bridge_coordinator::db::PasskeyCredentialRow {
        credential_id: "credential-1".into(),
        principal_id: "admin".into(),
        credential: "{}".into(),
        user_name: "Phone".into(),
        created_at: bridge_coordinator::now(),
        last_used: None,
        aaguid: None,
        transports: "[]".into(),
        backup_eligible: false,
        backed_up: false,
        sign_count: 0,
    };
    assert!(db.passkey_put_at_origin(&row, "https://bridge.example.test").unwrap());
    assert_eq!(db.passkey_origin("admin").unwrap().as_deref(), Some("https://bridge.example.test"));
    assert!(db.passkey_origin_bind("admin", "https://bridge.example.test").unwrap());
    assert!(!db.passkey_origin_bind("admin", "https://other.example.test").unwrap());
    assert_eq!(db.passkey_origin("admin").unwrap().as_deref(), Some("https://bridge.example.test"));
    let alternate = bridge_coordinator::db::PasskeyCredentialRow {
        credential_id: "credential-2".into(),
        ..row
    };
    assert!(!db.passkey_put_at_origin(&alternate, "https://other.example.test").unwrap());
    assert_eq!(db.passkey_count("admin").unwrap(), 1, "failed origin race must not create another credential");
    drop(db);
    remove_database(&path);
}

#[test]
fn discoverable_authentication_uses_conditional_mediation_without_an_allow_list() {
    let verifier = bridge_coordinator::passkeys::verifier("https://bridge.example.test").unwrap();
    let (options, state) = bridge_coordinator::passkeys::begin_authentication(&verifier, &[]).unwrap();
    let json = serde_json::to_value(options).unwrap();
    assert_eq!(json["mediation"], "conditional");
    assert_eq!(json["publicKey"]["userVerification"], "required");
    assert_eq!(json["publicKey"]["allowCredentials"], serde_json::json!([]));
    let stored = serde_json::to_string(&state).unwrap();
    let restored: bridge_coordinator::passkeys::CeremonyState = serde_json::from_str(&stored).unwrap();
    assert!(matches!(restored, bridge_coordinator::passkeys::CeremonyState::DiscoverableAuthentication(_)));
}

#[test]
fn expired_challenge_cannot_be_consumed() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    let now = bridge_coordinator::now();
    let row = WebauthnChallengeRow {
        principal_id: "admin".into(),
        binding: "setup".into(),
        kind: "authentication".into(),
        state: "{}".into(),
        expires_at: now + 1,
    };
    db.webauthn_challenge_put("expires", &row).unwrap();
    std::thread::sleep(std::time::Duration::from_millis(1200));
    assert!(db.webauthn_challenge_take("expires", "admin", "setup", "authentication").unwrap().is_none());
    drop(db);
    let _ = std::fs::remove_file(path);
}

#[test]
fn new_challenge_write_prunes_expired_rows() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    let first = WebauthnChallengeRow {
        principal_id: "admin".into(),
        binding: "browser-a".into(),
        kind: "authentication".into(),
        state: "{}".into(),
        expires_at: bridge_coordinator::now() + 1,
    };
    db.webauthn_challenge_put("expired", &first).unwrap();
    std::thread::sleep(std::time::Duration::from_millis(1200));

    let second = WebauthnChallengeRow {
        expires_at: bridge_coordinator::now() + 300,
        ..first
    };
    db.webauthn_challenge_put("active", &second).unwrap();
    let connection = rusqlite::Connection::open(&path).unwrap();
    let rows: i64 = connection.query_row("SELECT COUNT(*) FROM webauthn_challenges", [], |row| row.get(0)).unwrap();
    assert_eq!(rows, 1, "only unexpired challenge state should remain");
    drop(connection);
    drop(db);
    remove_database(&path);
}

#[tokio::test(flavor = "multi_thread")]
async fn coordinator_refuses_to_start_when_configured_passkey_origin_changes() {
    let env = common::boot_with(|t| {
        t.replace(
            "allowed_origins = []",
            "allowed_origins = [\"https://bridge.example.test\"]\npublic_origin = \"https://bridge.example.test\"",
        )
    })
    .await;
    let _ = env.admin().await;
    let db = Db::open(&bridge_coordinator::db_path(&env.dir)).unwrap();
    let credential = bridge_coordinator::db::PasskeyCredentialRow {
        credential_id: "credential-1".into(),
        principal_id: "admin".into(),
        credential: "{}".into(),
        user_name: "Phone".into(),
        created_at: bridge_coordinator::now(),
        last_used: None,
        aaguid: None,
        transports: "[]".into(),
        backup_eligible: false,
        backed_up: false,
        sign_count: 0,
    };
    db.passkey_put_at_origin(&credential, "https://bridge.example.test").unwrap();
    drop(db);

    env.srv.stop().await;
    common::edit_config(&env.dir, |text| text.replace("https://bridge.example.test", "https://other.example.test"));
    let error = match bridge_coordinator::server::start(&env.dir).await {
        Ok(server) => {
            server.stop().await;
            panic!("server accepted a changed passkey origin while a passkey remained enrolled");
        }
        Err(error) => error,
    };
    assert!(error.contains("public_origin cannot change while passkeys are enrolled"), "{error}");
    let _ = std::fs::remove_dir_all(&env.dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn coordinator_requires_explicit_origin_for_legacy_unbound_passkeys() {
    let env = common::boot().await;
    let _ = env.admin().await;
    let db = Db::open(&bridge_coordinator::db_path(&env.dir)).unwrap();
    db.passkey_put(&bridge_coordinator::db::PasskeyCredentialRow {
        credential_id: "legacy-credential".into(),
        principal_id: "admin".into(),
        credential: "{}".into(),
        user_name: "Phone".into(),
        created_at: bridge_coordinator::now(),
        last_used: None,
        aaguid: None,
        transports: "[]".into(),
        backup_eligible: false,
        backed_up: false,
        sign_count: 0,
    })
    .unwrap();
    drop(db);

    env.srv.stop().await;
    let error = match bridge_coordinator::server::start(&env.dir).await {
        Ok(server) => {
            server.stop().await;
            panic!("server accepted legacy passkeys without an explicitly configured canonical origin");
        }
        Err(error) => error,
    };
    assert!(error.contains("existing passkeys have no recorded canonical origin"), "{error}");
    let _ = std::fs::remove_dir_all(&env.dir);
}
