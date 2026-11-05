//! Operator commands: init defaults, info, health, backup/restore, setup-token, recovery commands.
mod common;
use bridge_coordinator::db::Db;
use bridge_coordinator::{db_path, ops};
use common::*;
use std::io::Write;
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
    let der = rustls_pemfile::certs(&mut &*pem).next().unwrap().unwrap();
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
    assert!(cfg.contains("bind = \"0.0.0.0:9443\"") && cfg.contains("lan_enabled = true"), "{cfg}");
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
    assert_eq!(std::fs::metadata(&out).unwrap().permissions().mode() & 0o777, 0o600);
    assert!(ops::backup(&env.dir, &out).unwrap_err().contains("refusing to overwrite"));
    api.add_worker("w2").await;
    // Restore is refused while the server holds the lock.
    let err = ops::restore(&env.dir, &out).unwrap_err();
    assert!(err.contains("stop the coordinator"), "{err}");
    let (dir, srv) = (env.dir.clone(), env.srv);
    srv.stop().await;
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
    let o = run(&["admin", "reset-password", "--data-dir", d], Some("a long enough password\n"));
    assert!(o.status.success(), "{}", text(&o));
    let (_, hash) = db.admin_get().unwrap().unwrap();
    assert!(hash.starts_with("$argon2id$"), "{hash}");
    assert_eq!(db.session_count().unwrap(), 0, "all sessions deleted");
    assert!(!run(&["admin", "reset-password", "--data-dir", d], Some("short\n")).status.success());

    // cert renew with a new address, then cert install of the same pair.
    let old = std::fs::read(dir.join("server.pem")).unwrap();
    let o = run(&["cert", "renew", "--data-dir", d, "--hostname", "a.test", "--ip", "192.0.2.9"], None);
    assert!(o.status.success(), "{}", text(&o));
    assert_ne!(old, std::fs::read(dir.join("server.pem")).unwrap());
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
    assert!(cfg.contains(&format!("bind = \"{bind}\"")) && cfg.contains("lan_enabled = true"), "{cfg}");
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
    Command::new("kill").args(["-TERM", &child.id().to_string()]).status().unwrap();
    let status = child.wait().unwrap();
    assert!(status.success(), "{status:?}");
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
    assert!(!run_env(&["health"], None, &env).status.success());

    // The environment wins over config.toml: lan_enabled=false refuses the non-loopback bind.
    let env2 = [
        ("BRIDGE_COORDINATOR_DATA_DIR", d),
        ("BRIDGE_COORDINATOR_BIND", bind.as_str()),
        ("BRIDGE_COORDINATOR_LAN_ENABLED", "false"),
    ];
    let o = run_env(&["serve"], None, &env2);
    assert_eq!(o.status.code(), Some(1));
    assert!(text(&o).contains("lan_enabled"), "{}", text(&o));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn init_accepts_an_existing_empty_directory() {
    let dir = tmp_dir();
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
    let o = run(&["init", "--data-dir", dir.to_str().unwrap(), "--hostname", "a.test"], None);
    assert!(o.status.success(), "{}", text(&o));
    assert!(dir.join("server.pem").exists());
    let _ = std::fs::remove_dir_all(dir);
}
