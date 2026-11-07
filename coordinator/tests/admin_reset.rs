//! `admin reset-password` (PRD-036): the interactive prompt (through an injected reader, not a real
//! terminal), validation, `--temporary` with the forced change at the API, token handling, session
//! deletion and the audit entry.
mod common;
use bridge_coordinator::admin_reset::{self, Options, PasswordInput};
use bridge_coordinator::db::Db;
use bridge_coordinator::{auth, db_path};
use common::*;
use serde_json::json;
use std::collections::VecDeque;
use std::io::Write;
use std::process::{Command, Output, Stdio};

/// Answers from a script instead of a terminal; records the prompts it was shown.
struct Scripted {
    tty: bool,
    answers: VecDeque<String>,
    prompts: Vec<String>,
}

impl Scripted {
    fn typed(answers: &[&str]) -> Scripted {
        Scripted {
            tty: true,
            answers: answers.iter().map(|s| s.to_string()).collect(),
            prompts: vec![],
        }
    }
    fn piped(line: &str) -> Scripted {
        Scripted {
            tty: false,
            ..Scripted::typed(&[line])
        }
    }
}

impl PasswordInput for Scripted {
    fn interactive(&self) -> bool {
        self.tty
    }
    fn read_secret(&mut self, prompt: &str) -> Result<String, String> {
        self.prompts.push(prompt.to_string());
        self.answers.pop_front().ok_or_else(|| "no more input".to_string())
    }
}

const NEW: &str = "a brand new password";

fn opts() -> Options {
    Options {
        username: None,
        keep_api_tokens: false,
        temporary: false,
    }
}

/// A database with an administrator, one session and two API tokens.
fn seeded() -> (std::path::PathBuf, Db) {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    let db = Db::open(&db_path(&dir)).unwrap();
    db.admin_create("admin", &auth::hash_password("the old password")).unwrap();
    db.session_create("session-hash", "csrf").unwrap();
    db.token_create("laptop", &auth::sha256_hex("tok-1"), None).unwrap();
    db.token_create("ci", &auth::sha256_hex("tok-2"), None).unwrap();
    (dir, db)
}

fn password_is(db: &Db, password: &str) -> bool {
    auth::verify_password(password, &db.admin_get().unwrap().unwrap().1)
}

#[test]
fn prompt_asks_twice_sets_the_password_and_clears_sessions_and_tokens() {
    let (dir, db) = seeded();
    let mut input = Scripted::typed(&[NEW, NEW]);
    let report = admin_reset::reset_password(&db, &opts(), &mut input).unwrap();
    assert_eq!(input.prompts, ["New password: ", "Repeat new password: "]);
    assert!(password_is(&db, NEW) && !password_is(&db, "the old password"));
    assert_eq!(db.session_count().unwrap(), 0, "all sessions deleted");
    assert_eq!(report.revoked_tokens, 2);
    assert!(db.token_lookup(&auth::sha256_hex("tok-1")).unwrap().is_none());
    assert!(report.temporary_password.is_none());
    assert!(!db.password_change_required().unwrap(), "a chosen password needs no further change");
    assert!(db.password_reset_notice().unwrap().is_some(), "the next sign-in can say the host reset it");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn mismatched_prompts_change_nothing() {
    let (dir, db) = seeded();
    let err = admin_reset::reset_password(&db, &opts(), &mut Scripted::typed(&[NEW, "a different password"]))
        .err()
        .unwrap();
    assert!(err.contains("do not match"), "{err}");
    assert!(password_is(&db, "the old password"));
    assert_eq!(db.session_count().unwrap(), 1);
    assert!(db.token_lookup(&auth::sha256_hex("tok-1")).unwrap().is_some());
    assert!(db.password_reset_notice().unwrap().is_none());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn the_web_password_rules_apply_to_the_prompt() {
    let (dir, db) = seeded();
    let short = "x".repeat(auth::MIN_PASSWORD_LEN - 1);
    let err = admin_reset::reset_password(&db, &opts(), &mut Scripted::typed(&[&short, &short]))
        .err()
        .unwrap();
    assert!(err.contains("at least"), "{err}");
    let long = "x".repeat(auth::MAX_PASSWORD_LEN + 1);
    let err = admin_reset::reset_password(&db, &opts(), &mut Scripted::typed(&[&long, &long])).err().unwrap();
    assert!(err.contains("at most"), "{err}");
    assert!(password_is(&db, "the old password"));
    assert_eq!(db.session_count().unwrap(), 1);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn piped_input_is_read_once_without_prompts() {
    let (dir, db) = seeded();
    let mut input = Scripted::piped(NEW);
    admin_reset::reset_password(&db, &opts(), &mut input).unwrap();
    assert_eq!(input.prompts, [""], "one read, no prompt text");
    assert!(password_is(&db, NEW));
    let err = admin_reset::reset_password(&db, &opts(), &mut Scripted::piped("short")).err().unwrap();
    assert!(err.contains("at least"), "{err}");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn username_must_name_the_single_administrator() {
    let (dir, db) = seeded();
    let wrong = Options {
        username: Some("someone".into()),
        ..opts()
    };
    let err = admin_reset::reset_password(&db, &wrong, &mut Scripted::piped(NEW)).err().unwrap();
    assert!(err.contains("exactly one administrator") && err.contains("admin"), "{err}");
    assert!(password_is(&db, "the old password"));
    let right = Options {
        username: Some("admin".into()),
        ..opts()
    };
    admin_reset::reset_password(&db, &right, &mut Scripted::piped(NEW)).unwrap();
    assert!(password_is(&db, NEW));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn keep_api_tokens_keeps_them_and_the_audit_entry_says_so() {
    let (dir, db) = seeded();
    let keep = Options {
        keep_api_tokens: true,
        ..opts()
    };
    let report = admin_reset::reset_password(&db, &keep, &mut Scripted::piped(NEW)).unwrap();
    assert_eq!(report.revoked_tokens, 0);
    assert!(db.token_lookup(&auth::sha256_hex("tok-1")).unwrap().is_some());
    assert!(db.token_lookup(&auth::sha256_hex("tok-2")).unwrap().is_some());
    assert_eq!(db.session_count().unwrap(), 0, "sessions are deleted either way");
    let audit = db.audit_list(10).unwrap();
    let entry = audit.iter().find(|e| e["action"] == "password.reset").expect("audit entry");
    assert_eq!(entry["actor"], "cli");
    assert_eq!(entry["remote"], "local");
    assert!(entry["detail"].as_str().unwrap().contains("API tokens kept"), "{entry}");
    assert!(!entry.to_string().contains(NEW), "the password is never logged");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn temporary_reset_audit_entry_has_the_token_count_and_no_password() {
    let (dir, db) = seeded();
    admin_reset::reset_password(&db, &Options { temporary: true, ..opts() }, &mut Scripted::piped("")).unwrap();
    let audit = db.audit_list(10).unwrap();
    let entry = audit.iter().find(|e| e["action"] == "password.reset").expect("audit entry");
    let detail = entry["detail"].as_str().unwrap();
    assert!(detail.contains("2 API token(s) revoked") && detail.contains("temporary password"), "{detail}");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn temporary_passwords_are_generated_valid_and_different() {
    let a = admin_reset::temporary_password();
    let b = admin_reset::temporary_password();
    assert_ne!(a, b);
    assert!(auth::valid_password(&a).is_ok(), "{a}");
    assert!(a.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'), "{a}");
}

#[tokio::test(flavor = "multi_thread")]
async fn temporary_password_forces_a_change_at_the_api_until_it_is_changed() {
    let env = boot().await;
    let old = env.admin().await;
    let db = Db::open(&db_path(&env.dir)).unwrap();
    let report = admin_reset::reset_password(&db, &Options { temporary: true, ..opts() }, &mut Scripted::piped("")).unwrap();
    let temporary = report.temporary_password.expect("generated password");

    // The old session is gone; the old password no longer works.
    assert_eq!(old.get("/api/v1/workers").await.0, 401);
    let mut api = env.api();
    assert_eq!(api.login("admin", PASSWORD).await, 401);

    // The temporary password signs in, but nothing else works until the password is changed.
    assert_eq!(api.login("admin", &temporary).await, 200);
    for path in ["/api/v1/workers", "/api/v1/missions", "/api/v1/diagnostics"] {
        let (st, body) = api.get(path).await;
        assert_eq!(st, 403, "{path}: {body}");
        assert_eq!(body["error"]["code"], "password-change-required", "{path}: {body}");
    }
    let (st, body) = api.post("/api/v1/workers", json!({"name": "w1"})).await;
    assert_eq!((st, body["error"]["code"].as_str()), (403, Some("password-change-required")), "{body}");

    // The state endpoint still answers and says why.
    let (st, state) = api.get("/api/v1/auth/state").await;
    assert_eq!(st, 200);
    assert_eq!(state["authenticated"], true);
    assert_eq!(state["password_change_required"], true);
    assert!(state["password_reset_notice"]["message"]
        .as_str()
        .unwrap()
        .contains("reset by an administrator on the host"));

    // A wrong current password is refused; the right one changes it.
    let (st, _) = api.post("/api/v1/auth/password", json!({"current": "not it at all!", "new": NEW})).await;
    assert_eq!(st, 403);
    let (st, _) = api.post("/api/v1/auth/password", json!({"current": temporary, "new": "x"})).await;
    assert_eq!(st, 400, "the web rules apply to the new password");
    let (st, body) = api.post("/api/v1/auth/password", json!({"current": temporary, "new": NEW})).await;
    assert_eq!(st, 200, "{body}");

    // Normal service afterwards: the flag and the notice are gone.
    let mut api = env.api();
    assert_eq!(api.login("admin", &temporary).await, 401, "the temporary password is dead");
    assert_eq!(api.login("admin", NEW).await, 200);
    assert_eq!(api.get("/api/v1/workers").await.0, 200);
    let (_, state) = api.get("/api/v1/auth/state").await;
    assert_eq!(state["password_change_required"], false);
    assert!(state.get("password_reset_notice").is_none(), "{state}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn sign_out_stays_available_while_a_change_is_required() {
    let env = boot().await;
    let _ = env.admin().await;
    let db = Db::open(&db_path(&env.dir)).unwrap();
    let report = admin_reset::reset_password(&db, &Options { temporary: true, ..opts() }, &mut Scripted::piped("")).unwrap();
    let mut api = env.api();
    assert_eq!(api.login("admin", &report.temporary_password.unwrap()).await, 200);
    assert_eq!(api.post("/api/v1/auth/logout", json!({})).await.0, 200);
    assert_eq!(api.get("/api/v1/auth/state").await.1["authenticated"], false);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_chosen_password_signs_in_normally_and_shows_the_notice() {
    let env = boot().await;
    let _ = env.admin().await;
    let db = Db::open(&db_path(&env.dir)).unwrap();
    admin_reset::reset_password(&db, &opts(), &mut Scripted::piped(NEW)).unwrap();
    let mut api = env.api();
    assert_eq!(api.login("admin", NEW).await, 200);
    assert_eq!(api.get("/api/v1/workers").await.0, 200, "no forced change for a chosen password");
    let (_, state) = api.get("/api/v1/auth/state").await;
    assert_eq!(state["password_change_required"], false);
    assert!(state["password_reset_notice"]["at"].as_i64().unwrap() <= bridge_coordinator::now());
    // Not shown to an anonymous visitor.
    assert!(env.api().get("/api/v1/auth/state").await.1.get("password_reset_notice").is_none());
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tokens_revoked_by_default_and_kept_with_the_option_at_the_api() {
    for keep in [false, true] {
        let env = boot().await;
        let _ = env.admin().await;
        let db = Db::open(&db_path(&env.dir)).unwrap();
        db.token_create_scoped("laptop", &auth::sha256_hex("tok-api"), None, &["admin".to_string()])
            .unwrap();
        let mut api = env.api();
        api.bearer = Some("tok-api".into());
        assert_eq!(api.get("/api/v1/workers").await.0, 200);
        let o = Options {
            keep_api_tokens: keep,
            temporary: true,
            ..opts()
        };
        admin_reset::reset_password(&db, &o, &mut Scripted::piped("")).unwrap();
        assert_eq!(api.get("/api/v1/workers").await.0, if keep { 200 } else { 401 }, "keep={keep}");
        env.stop().await;
    }
}

fn run(args: &[&str], stdin: &str) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_bridge-coordinator"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.as_mut().unwrap().write_all(stdin.as_bytes()).unwrap();
    drop(child.stdin.take());
    child.wait_with_output().unwrap()
}

fn text(o: &Output) -> String {
    format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr))
}

#[test]
fn command_line_prints_the_temporary_password_once_and_it_signs_in_only_for_the_change() {
    let (dir, db) = seeded();
    let d = dir.to_str().unwrap();
    let o = run(&["admin", "reset-password", "--temporary", "--data-dir", d], "");
    assert!(o.status.success(), "{}", text(&o));
    let out = String::from_utf8_lossy(&o.stdout).to_string();
    let line = out.lines().find(|l| l.starts_with("temporary password")).expect(&out);
    let password = line.rsplit(' ').next().unwrap();
    assert!(auth::valid_password(password).is_ok() && password_is(&db, password), "{out}");
    assert_eq!(out.matches(password).count(), 1, "printed once: {out}");
    assert!(db.password_change_required().unwrap());
    assert!(out.contains("2 API token(s) revoked"), "{out}");

    let o = run(&["admin", "reset-password", "--temporary", "--keep-api-tokens", "--data-dir", d], "");
    assert!(String::from_utf8_lossy(&o.stdout).contains("API tokens kept"), "{}", text(&o));

    let o = run(&["admin", "reset-password", "--username", "nobody", "--data-dir", d], &format!("{NEW}\n"));
    assert!(!o.status.success() && text(&o).contains("exactly one administrator"), "{}", text(&o));
    let o = run(&["admin", "reset-password", "--username", "admin", "--data-dir", d], &format!("{NEW}\n"));
    assert!(o.status.success(), "{}", text(&o));
    assert!(password_is(&db, NEW) && !db.password_change_required().unwrap());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn help_describes_the_new_options() {
    let o = run(&["help"], "");
    let help = text(&o);
    for needle in ["--temporary", "--username NAME", "--keep-api-tokens", "twice, without echo"] {
        assert!(help.contains(needle), "{needle} missing from help:\n{help}");
    }
}

#[cfg(unix)]
#[test]
fn files_created_by_another_user_are_given_back_to_the_data_directory_owner() {
    use std::os::unix::fs::MetadataExt;
    let (dir, db) = seeded();
    drop(db);
    assert!(admin_reset::files_needing_chown(&dir).is_empty(), "same owner: nothing to do");
    admin_reset::restore_ownership(&dir); // no-op, must not fail
    let me = std::fs::metadata(&dir).unwrap();
    if me.uid() == 0 {
        // Only a privileged test run can create the mismatch.
        std::os::unix::fs::chown(db_path(&dir), Some(12345), Some(12345)).unwrap();
        assert_eq!(admin_reset::files_needing_chown(&dir), [db_path(&dir)]);
        admin_reset::restore_ownership(&dir);
        assert_eq!(std::fs::metadata(db_path(&dir)).unwrap().uid(), 0);
    }
    let _ = std::fs::remove_dir_all(dir);
}
