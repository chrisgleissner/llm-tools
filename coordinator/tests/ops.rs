//! Operator commands: init defaults, info, health, backup/restore, setup-token, recovery commands.
mod common;
use bridge_coordinator::db::Db;
use bridge_coordinator::{db_path, ops};
use common::*;
use rustls::pki_types::pem::PemObject;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output, Stdio};

fn run(args: &[&str], stdin: Option<&str>) -> Output {
    run_env(args, stdin, &[])
}

fn run_env(args: &[&str], stdin: Option<&str>, envs: &[(&str, &str)]) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_bridge-coordinator"))
        .args(args)
        .envs(envs.iter().copied())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    if let Some(text) = stdin {
        child.stdin.as_mut().unwrap().write_all(text.as_bytes()).unwrap();
    }
    drop(child.stdin.take());
    child.wait_with_output().unwrap()
}

fn text(o: &Output) -> String {
    format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr))
}

fn contains(file: &Path, needle: &[u8]) -> bool {
    let pem = std::fs::read(file).unwrap();
    let der = rustls::pki_types::CertificateDer::pem_slice_iter(&pem).next().unwrap().unwrap();
    der.as_ref().windows(needle.len()).any(|w| w == needle)
}

#[test]
fn init_without_flags_discovers_names_and_prints_next_steps() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    let o = run(&["init", "--data-dir", d], None);
    let out = text(&o);
    assert!(o.status.success(), "{out}");
    assert!(out.contains("https://localhost:8443/"), "{out}");
    assert!(out.contains(dir.join("ca.pem").to_str().unwrap()), "{out}");
    assert!(out.contains(dir.join("setup-token").to_str().unwrap()), "{out}");
    assert!(out.contains("CA SHA-256:"), "{out}");
    assert!(out.contains("Firefox") && out.contains("Android") && out.contains("Chrome"), "{out}");
    assert!(!out.to_lowercase().contains("proceed anyway"));
    if let Some(host) = bridge_coordinator::netinfo::machine_hostname() {
        assert!(contains(&dir.join("server.pem"), host.as_bytes()), "hostname {host} is a SAN");
        assert!(contains(&dir.join("server.pem"), format!("{host}.home.arpa").as_bytes()) || host.contains('.'));
    }
    assert!(contains(&dir.join("server.pem"), b"localhost"));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn explicit_names_replace_discovery_and_bind_enables_lan() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    let o = run(
        &[
            "init",
            "--data-dir",
            d,
            "--hostname",
            "bridge.example.test",
            "--ip",
            "192.0.2.7",
            "--bind",
            "0.0.0.0:9443",
        ],
        None,
    );
    assert!(o.status.success(), "{}", text(&o));
    let cert = dir.join("server.pem");
    assert!(contains(&cert, b"bridge.example.test"));
    assert!(contains(&cert, &[192, 0, 2, 7]));
    assert!(!contains(&cert, b".home.arpa"));
    let cfg = std::fs::read_to_string(dir.join("config.toml")).unwrap();
    assert!(cfg.contains("bind = \"0.0.0.0:9443\"") && cfg.contains("exposure = \"lan\""), "{cfg}");
    let info = text(&run(&["info", "--data-dir", d], None));
    assert!(info.contains("LAN access:") && info.contains("enabled"), "{info}");
    assert!(info.contains("Bind address:      0.0.0.0:9443"), "{info}");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn info_prints_fingerprint_and_flags_uncovered_url() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    let info = text(&run(&["info", "--data-dir", d], None));
    let fp = ops::ca_fingerprint(&dir).unwrap();
    assert_eq!(fp.len(), 32 * 3 - 1);
    assert!(info.contains(&fp), "{info}");
    assert!(info.contains("https://localhost:8443/") && info.contains("loopback only"), "{info}");
    let _ = std::fs::remove_dir_all(dir);
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

#[tokio::test(flavor = "multi_thread")]
async fn health_checks_the_configured_bind_with_the_local_ca() {
    let port = free_port();
    let env = boot_with(|t| t.replace("127.0.0.1:0", &format!("127.0.0.1:{port}"))).await;
    ops::health(&env.dir).await.unwrap();
    let d = env.dir.to_str().unwrap().to_string();
    let ok = tokio::task::spawn_blocking({
        let d = d.clone();
        move || run(&["health", "--data-dir", &d], None)
    })
    .await
    .unwrap();
    assert!(ok.status.success(), "{}", text(&ok));
    let dir = env.dir.clone();
    env.srv.stop().await;
    assert!(ops::health(&dir).await.is_err());
    let bad = tokio::task::spawn_blocking(move || run(&["health", "--data-dir", &d], None)).await.unwrap();
    assert_eq!(bad.status.code(), Some(1));
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn backup_and_restore_round_trip() {
    let env = boot().await;
    let api = env.admin().await;
    api.add_worker("w1").await;
    let out = env.dir.join("backup.sqlite3");
    ops::backup(&env.dir, &out).unwrap(); // online: the server is running
    #[cfg(unix)]
    assert_eq!(std::fs::metadata(&out).unwrap().permissions().mode() & 0o777, 0o600);
    assert!(ops::backup(&env.dir, &out).unwrap_err().contains("refusing to overwrite"));
    api.add_worker("w2").await;
    // Restore is refused while the server holds the lock.
    let err = ops::restore(&env.dir, &out).unwrap_err();
    assert!(err.contains("stop the coordinator"), "{err}");
    let (dir, srv) = (env.dir.clone(), env.srv);
    srv.stop().await;
    let released_lock = ops::lock_data_dir(&dir).expect("stopped server releases its data-directory lock");
    drop(released_lock);
    let kept = ops::restore(&dir, &out).unwrap();
    assert!(kept.file_name().unwrap().to_str().unwrap().contains(".before-restore-"));
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.worker_get("w1").unwrap().is_some());
    assert!(db.worker_get("w2").unwrap().is_none(), "restored state predates w2");
    let old = Db::open(&kept).unwrap();
    assert!(old.worker_get("w2").unwrap().is_some(), "replaced database is kept");
    assert!(ops::restore(&dir, &dir.join("missing")).is_err());
    std::fs::write(dir.join("junk"), b"not a database at all, just text").unwrap();
    assert!(ops::restore(&dir, &dir.join("junk")).is_err());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn backup_restore_cli_and_setup_token() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    let before = std::fs::read_to_string(dir.join("setup-token")).unwrap();
    let o = run(&["setup-token", "--data-dir", d], None);
    assert!(o.status.success(), "{}", text(&o));
    let after = std::fs::read_to_string(dir.join("setup-token")).unwrap();
    assert_ne!(before, after);
    #[cfg(unix)]
    assert_eq!(std::fs::metadata(dir.join("setup-token")).unwrap().permissions().mode() & 0o777, 0o600);
    let bk = dir.join("b.sqlite3");
    assert!(run(&["backup", "--data-dir", d, "--output", bk.to_str().unwrap()], None).status.success());
    assert!(!run(&["backup", "--data-dir", d, "--output", bk.to_str().unwrap()], None).status.success());
    assert!(run(&["restore", "--data-dir", d, "--input", bk.to_str().unwrap()], None).status.success());
    // After an administrator exists no new setup token is issued.
    Db::open(&db_path(&dir)).unwrap().admin_create("admin", "hash").unwrap();
    let o = run(&["setup-token", "--data-dir", d], None);
    assert!(!o.status.success() && text(&o).contains("administrator already exists"), "{}", text(&o));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn recovery_commands_reset_password_and_certificates() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    // No administrator yet.
    assert!(!run(&["admin", "reset-password", "--data-dir", d], Some("a long enough password\n"))
        .status
        .success());
    let db = Db::open(&db_path(&dir)).unwrap();
    db.admin_create("admin", "old-hash").unwrap();
    db.session_create("tokenhash", "csrf").unwrap();
    let principal = db.admin_principal_id().unwrap().unwrap();
    db.totp_pending_set(&principal, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ").unwrap();
    db.totp_enable_with_recovery_codes(&principal, &["$argon2id$recovery-hash".into()], 1).unwrap();
    db.admin_set_password("browser-change-hash", true).unwrap();
    assert!(db.totp_get(&principal).unwrap().unwrap().1, "browser password changes must preserve MFA");
    assert_eq!(db.recovery_code_hashes(&principal).unwrap().len(), 1);
    let o = run(&["admin", "reset-password", "--data-dir", d], Some("a long enough password\n"));
    assert!(o.status.success(), "{}", text(&o));
    let (_, hash) = db.admin_get().unwrap().unwrap();
    assert!(hash.starts_with("$argon2id$"), "{hash}");
    assert_eq!(db.session_count().unwrap(), 0, "all sessions deleted");
    assert!(db.totp_get(&principal).unwrap().is_none(), "local password recovery clears a lost TOTP factor");
    assert!(db.recovery_code_hashes(&principal).unwrap().is_empty());
    assert!(!run(&["admin", "reset-password", "--data-dir", d], Some("short\n")).status.success());

    // cert renew within the CA's name constraints works.
    let old = std::fs::read(dir.join("server.pem")).unwrap();
    let o = run(&["cert", "renew", "--data-dir", d, "--hostname", "a.test"], None);
    assert!(o.status.success(), "{}", text(&o));
    assert_ne!(old, std::fs::read(dir.join("server.pem")).unwrap());
    // A new address is outside them: renew refuses and changes nothing; new-ca makes a new CA.
    let renewed = std::fs::read(dir.join("server.pem")).unwrap();
    let o = run(&["cert", "renew", "--data-dir", d, "--hostname", "a.test", "--ip", "192.0.2.9"], None);
    let msg = text(&o);
    assert!(!o.status.success() && msg.contains("192.0.2.9") && msg.contains("cert new-ca"), "{msg}");
    assert!(!msg.contains("a.test,"), "only the offending name is listed: {msg}");
    assert_eq!(renewed, std::fs::read(dir.join("server.pem")).unwrap());
    let old_ca = std::fs::read(dir.join("ca.pem")).unwrap();
    let o = run(&["cert", "new-ca", "--data-dir", d, "--hostname", "a.test", "--ip", "192.0.2.9"], None);
    assert!(o.status.success(), "{}", text(&o));
    assert!(text(&o).contains("import"), "{}", text(&o));
    let kept: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("ca.pem.old-"))
        .collect();
    assert_eq!(kept.len(), 1, "{kept:?}");
    assert_eq!(old_ca, std::fs::read(dir.join(&kept[0])).unwrap());
    assert_ne!(old_ca, std::fs::read(dir.join("ca.pem")).unwrap());
    assert!(contains(&dir.join("server.pem"), &[192, 0, 2, 9]));
    let (c, k) = (dir.join("copy.pem"), dir.join("copy.key"));
    std::fs::copy(dir.join("server.pem"), &c).unwrap();
    std::fs::copy(dir.join("server.key"), &k).unwrap();
    let o = run(
        &["cert", "install", "--data-dir", d, "--cert", c.to_str().unwrap(), "--key", k.to_str().unwrap()],
        None,
    );
    assert!(o.status.success(), "{}", text(&o));
    // A mismatched pair is rejected.
    std::fs::write(&k, b"not a key").unwrap();
    assert!(!run(
        &["cert", "install", "--data-dir", d, "--cert", c.to_str().unwrap(), "--key", k.to_str().unwrap()],
        None
    )
    .status
    .success());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn local_reset_passkeys_clears_credentials_origin_binding_and_sessions() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    let db = Db::open(&db_path(&dir)).unwrap();
    db.admin_create("admin", "old-hash").unwrap();
    db.session_create("tokenhash", "csrf").unwrap();
    let conn = rusqlite::Connection::open(db_path(&dir)).unwrap();
    conn.execute("INSERT INTO auth_policy(principal_id,passkey_origin) VALUES('admin','https://a.test')", [])
        .unwrap();
    conn.execute(
        "INSERT INTO passkeys(credential_id,principal_id,credential,user_name,created_at) VALUES('cred','admin','{}','admin',1)",
        [],
    )
    .unwrap();
    drop(conn);

    let out = run(&["admin", "reset-passkeys", "--data-dir", d], None);
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("1 passkey"));
    assert_eq!(db.passkey_count("admin").unwrap(), 0);
    assert_eq!(db.passkey_origin("admin").unwrap(), None);
    assert_eq!(db.session_count().unwrap(), 0);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn init_twice_changes_nothing_and_says_so() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    let (cert, key, cfg) = (
        std::fs::read(dir.join("server.pem")).unwrap(),
        std::fs::read(dir.join("ca.key")).unwrap(),
        std::fs::read(dir.join("config.toml")).unwrap(),
    );
    let o = run(&["init", "--data-dir", d, "--hostname", "other.test"], None);
    assert!(o.status.success() && text(&o).contains("already initialised"), "{}", text(&o));
    assert_eq!(cert, std::fs::read(dir.join("server.pem")).unwrap());
    assert_eq!(key, std::fs::read(dir.join("ca.key")).unwrap());
    assert_eq!(cfg, std::fs::read(dir.join("config.toml")).unwrap());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn environment_variables_select_data_dir_and_override_config() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    let port = free_port().to_string();
    let bind = format!("0.0.0.0:{port}");
    let env = [
        ("BRIDGE_COORDINATOR_DATA_DIR", d),
        ("BRIDGE_COORDINATOR_BIND", bind.as_str()),
        ("BRIDGE_COORDINATOR_LAN_ENABLED", "true"),
    ];
    // init and the admin commands run on an empty directory using only the environment.
    let o = run_env(&["init", "--hostname", "a.test"], None, &env);
    assert!(o.status.success(), "{}", text(&o));
    let info = text(&run_env(&["info"], None, &env));
    assert!(
        info.contains(&format!("Bind address:      {bind}")) && info.contains("LAN access:        enabled"),
        "{info}"
    );
    // init wrote a config.toml consistent with the environment it ran in.
    let cfg = std::fs::read_to_string(dir.join("config.toml")).unwrap();
    assert!(cfg.contains(&format!("bind = \"{bind}\"")) && cfg.contains("exposure = \"lan\""), "{cfg}");
    assert!(run_env(&["setup-token"], None, &env).status.success());
    assert!(run_env(&["backup", "--output", dir.join("b.db").to_str().unwrap()], None, &env)
        .status
        .success());

    // serve with only the environment, health as a container HEALTHCHECK, clean SIGTERM exit.
    let mut child = Command::new(env!("CARGO_BIN_EXE_bridge-coordinator"))
        .arg("serve")
        .envs(env.iter().copied())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut healthy = false;
    for _ in 0..100 {
        if run_env(&["health"], None, &env).status.success() {
            healthy = true;
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    assert!(healthy, "health succeeds against the environment-configured bind");
    let started = std::time::Instant::now();
    // SIGTERM is a Unix signal; Windows has no equivalent for a console-less child, so the process
    // is terminated and only the exit time and the following health failure are checked there.
    #[cfg(unix)]
    {
        Command::new("kill").args(["-TERM", &child.id().to_string()]).status().unwrap();
        let status = child.wait().unwrap();
        assert!(status.success(), "{status:?}");
    }
    #[cfg(not(unix))]
    {
        child.kill().unwrap();
        child.wait().unwrap();
    }
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
    assert!(!run_env(&["health"], None, &env).status.success());

    // The legacy environment alias still wins over config.toml and refuses a non-loopback bind.
    let env2 = [
        ("BRIDGE_COORDINATOR_DATA_DIR", d),
        ("BRIDGE_COORDINATOR_BIND", bind.as_str()),
        ("BRIDGE_COORDINATOR_LAN_ENABLED", "false"),
    ];
    let o = run_env(&["serve"], None, &env2);
    assert_eq!(o.status.code(), Some(1));
    assert!(text(&o).contains("exposure"), "{}", text(&o));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn init_accepts_an_existing_empty_directory() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    #[cfg(unix)]
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
    let o = run(&["init", "--data-dir", dir.to_str().unwrap(), "--hostname", "a.test"], None);
    assert!(o.status.success(), "{}", text(&o));
    assert!(dir.join("server.pem").exists());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn token_create_sets_expiry_and_password_reset_revokes_tokens() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    Db::open(&db_path(&dir)).unwrap().admin_create("admin", "old-hash").unwrap();
    let now = bridge_coordinator::now();
    for (name, args, days, wanted_scopes) in [
        ("default", vec![], Some(90), vec!["read"]),
        ("short", vec!["--expires-days", "3"], Some(3), vec!["read"]),
        ("forever", vec!["--expires-days", "0"], None, vec!["read"]),
        (
            "ops",
            vec!["--scope", "operate", "--scope", "terminal:view"],
            Some(90),
            vec!["operate", "terminal:view"],
        ),
    ] {
        let mut a = vec!["token", "create", name, "--data-dir", d];
        a.extend(args);
        let o = run(&a, None);
        assert!(o.status.success(), "{}", text(&o));
        assert!(text(&o).contains("token scopes"), "{}", text(&o));
        let db = Db::open(&db_path(&dir)).unwrap();
        let tokens = db.tokens_list().unwrap();
        let expires = tokens.iter().find(|(n, _)| n == name).unwrap().1;
        match days {
            Some(days) => {
                let e = expires.expect("has an expiry");
                assert!((e - (now + days * 86_400)).abs() < 60, "{name}: {e}");
            }
            None => assert_eq!(expires, None, "{name} never expires"),
        }
        let token = String::from_utf8_lossy(&o.stdout).trim().to_string();
        let record = db.token_lookup(&bridge_coordinator::auth::sha256_hex(&token)).unwrap().unwrap();
        assert_eq!(record.scopes, wanted_scopes);
    }
    assert!(!run(&["token", "create", "bad-scope", "--scope", "unknown", "--data-dir", d], None)
        .status
        .success());
    assert!(!run(&["token", "create", "bad", "--expires-days", "soon", "--data-dir", d], None)
        .status
        .success());
    // Resetting the password revokes every API token unless asked not to.
    let o = run(
        &["admin", "reset-password", "--keep-api-tokens", "--data-dir", d],
        Some("a long enough password\n"),
    );
    assert!(o.status.success(), "{}", text(&o));
    assert_eq!(Db::open(&db_path(&dir)).unwrap().tokens_list().unwrap().len(), 4, "kept on request");
    let o = run(&["admin", "reset-password", "--data-dir", d], Some("a long enough password\n"));
    assert!(o.status.success() && text(&o).contains("4 API token(s) revoked"), "{}", text(&o));
    assert!(Db::open(&db_path(&dir)).unwrap().tokens_list().unwrap().is_empty());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn restore_lists_the_credentials_it_reinstated() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    assert!(run(&["init", "--data-dir", d, "--hostname", "a.test"], None).status.success());
    assert!(run(&["worker", "add", "w1", "--data-dir", d], None).status.success());
    assert!(run(&["token", "create", "ci", "--data-dir", d], None).status.success());
    let db = Db::open(&db_path(&dir)).unwrap();
    db.session_create("old-session", "csrf").unwrap();
    drop(db);
    let bk = dir.join("b.sqlite3");
    assert!(run(&["backup", "--data-dir", d, "--output", bk.to_str().unwrap()], None).status.success());
    // After the backup the operator revokes both; the restore brings them back.
    assert!(run(&["worker", "revoke", "w1", "--data-dir", d], None).status.success());
    assert!(run(&["token", "revoke", "ci", "--data-dir", d], None).status.success());
    let o = run(&["restore", "--data-dir", d, "--input", bk.to_str().unwrap()], None);
    let out = text(&o);
    assert!(o.status.success(), "{out}");
    assert!(out.contains("worker w1: bridge-coordinator worker revoke w1"), "{out}");
    assert!(out.contains("API token ci") && out.contains("bridge-coordinator token revoke ci"), "{out}");
    let db = Db::open(&db_path(&dir)).unwrap();
    assert!(db.worker_get("w1").unwrap().is_some());
    assert_eq!(db.session_count().unwrap(), 0, "browser sessions are not carried over");
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn container_without_public_names_lists_only_localhost_and_says_how_to_add_names() {
    let dir = tmp_dir();
    let d = dir.to_str().unwrap();
    let env = [
        ("BRIDGE_COORDINATOR_DATA_DIR", d),
        ("BRIDGE_COORDINATOR_BIND", "0.0.0.0:8443"),
        ("BRIDGE_COORDINATOR_LAN_ENABLED", "true"),
        ("BRIDGE_COORDINATOR_CONTAINER", "1"),
        ("BRIDGE_COORDINATOR_PUBLIC_NAMES", "localhost"),
    ];
    assert!(run_env(&["init"], None, &env).status.success());
    let info = text(&run_env(&["info"], None, &env));
    let urls: Vec<&str> = info.lines().filter(|l| l.trim_start().starts_with("https://")).collect();
    // The random container host name and bridge-network address are never offered.
    assert_eq!(urls, vec!["  https://localhost:8443/"], "{info}");
    assert!(info.contains("BRIDGE_PUBLIC_NAMES") && info.contains("cert new-ca"), "{info}");

    // With public names the hint disappears and the names are listed as covered.
    let named = [env[0], env[1], env[2], env[3], ("BRIDGE_COORDINATOR_PUBLIC_NAMES", "bridge.home.arpa")];
    let dir2 = tmp_dir();
    let named2: Vec<(&str, &str)> = named
        .iter()
        .map(|&(k, v)| {
            if k == "BRIDGE_COORDINATOR_DATA_DIR" {
                (k, dir2.to_str().unwrap())
            } else {
                (k, v)
            }
        })
        .collect();
    assert!(run_env(&["init"], None, &named2).status.success());
    let info = text(&run_env(&["info"], None, &named2));
    assert!(
        info.contains("https://bridge.home.arpa:8443/\n") && !info.contains("BRIDGE_PUBLIC_NAMES"),
        "{info}"
    );
}
