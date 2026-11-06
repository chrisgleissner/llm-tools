use bridge_coordinator::db::{Db, WebauthnChallengeRow, SCHEMA_VERSION};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

fn db_path() -> PathBuf {
    std::env::temp_dir().join(format!(
        "bridge-passkeys-{}-{}.sqlite",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()
    ))
}

#[test]
fn migrations_add_passkey_tables_and_challenge_is_session_bound_single_use() {
    let path = db_path();
    let db = Db::open(&path).unwrap();
    assert_eq!(SCHEMA_VERSION, 8);
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
    let _ = std::fs::remove_file(path);
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
    let _ = std::fs::remove_file(path);
}
