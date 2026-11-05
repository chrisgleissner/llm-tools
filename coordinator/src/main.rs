//! `bridge-coordinator` command line (see docs/distributed/coordinator.md).

use bridge_coordinator::certs::{self, Sans};
use bridge_coordinator::db::Db;
use bridge_coordinator::{auth, db_path, protocol, server};
use std::net::IpAddr;
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "usage: bridge-coordinator <command> [--data-dir DIR]
  init [--hostname NAME]... [--ip ADDR]...
  serve [--web-dir DIR]
  worker add NAME | worker revoke NAME
  admin reset-password          (new password on stdin)
  cert renew [--hostname NAME]... [--ip ADDR]... | cert install --cert FILE --key FILE
  token create NAME | token revoke NAME
--data-dir defaults to $BRIDGE_COORDINATOR_DATA_DIR.";

struct Args {
    data_dir: Option<PathBuf>,
    hostnames: Vec<String>,
    ips: Vec<IpAddr>,
    cert: Option<PathBuf>,
    key: Option<PathBuf>,
    web_dir: Option<PathBuf>,
    words: Vec<String>,
}

fn parse(args: Vec<String>) -> Result<Args, String> {
    let mut a = Args {
        data_dir: None,
        hostnames: vec![],
        ips: vec![],
        cert: None,
        key: None,
        web_dir: None,
        words: vec![],
    };
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        let mut value = |flag: &str| it.next().ok_or_else(|| format!("{flag} needs a value"));
        match arg.as_str() {
            "--data-dir" => a.data_dir = Some(value("--data-dir")?.into()),
            "--hostname" => a.hostnames.push(value("--hostname")?),
            "--ip" => {
                let v = value("--ip")?;
                a.ips.push(v.parse().map_err(|_| format!("invalid IP address {v:?}"))?);
            }
            "--cert" => a.cert = Some(value("--cert")?.into()),
            "--web-dir" => a.web_dir = Some(value("--web-dir")?.into()),
            "--key" => a.key = Some(value("--key")?.into()),
            s if s.starts_with("--") => return Err(format!("unknown option {s}")),
            _ => a.words.push(arg),
        }
    }
    Ok(a)
}

fn open_db(dir: &std::path::Path) -> Result<Db, String> {
    if !dir.join("config.toml").exists() {
        return Err(format!("{} is not an initialised data directory (run `init`)", dir.display()));
    }
    Db::open(&db_path(dir)).map_err(|e| format!("cannot open database: {e}"))
}

async fn run(args: Args) -> Result<(), String> {
    let words: Vec<&str> = args.words.iter().map(String::as_str).collect();
    if words.is_empty() || words == ["help"] {
        println!("{USAGE}");
        return Ok(());
    }
    let dir = args
        .data_dir
        .clone()
        .or_else(|| std::env::var_os("BRIDGE_COORDINATOR_DATA_DIR").map(PathBuf::from))
        .ok_or("--data-dir is required")?;
    let sans = Sans {
        hostnames: args.hostnames.clone(),
        ips: args.ips.clone(),
    };
    match words.as_slice() {
        ["init"] => bridge_coordinator::init::init(&dir, &sans),
        ["serve"] => {
            let srv = server::start_with(&dir, args.web_dir.as_deref()).await?;
            eprintln!("bridge-coordinator listening on https://{}", srv.addr);
            let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).map_err(|e| e.to_string())?;
            tokio::select! { _ = term.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
            eprintln!("shutting down");
            srv.stop().await;
            Ok(())
        }
        ["worker", "add", name] => {
            if !protocol::valid_name(name) {
                return Err("worker name must be lowercase letters, digits and '-' (at most 63)".into());
            }
            let db = open_db(&dir)?;
            let token = auth::random_token();
            if !db.worker_add(name, &auth::sha256_hex(&token)).map_err(|e| e.to_string())? {
                return Err(format!("worker {name} already exists"));
            }
            db.audit("cli", "worker.add", name, "local");
            println!("{token}");
            Ok(())
        }
        ["worker", "revoke", name] => {
            let db = open_db(&dir)?;
            if !db.worker_revoke(name).map_err(|e| e.to_string())? {
                return Err(format!("no worker named {name}"));
            }
            db.audit("cli", "worker.revoke", name, "local");
            Ok(())
        }
        ["token", "create", name] => {
            if !protocol::valid_name(name) {
                return Err("token name must be lowercase letters, digits and '-' (at most 63)".into());
            }
            let db = open_db(&dir)?;
            let token = auth::random_token();
            if !db.token_create(name, &auth::sha256_hex(&token)).map_err(|e| e.to_string())? {
                return Err(format!("token {name} already exists"));
            }
            db.audit("cli", "token.create", name, "local");
            println!("{token}");
            Ok(())
        }
        ["token", "revoke", name] => {
            let db = open_db(&dir)?;
            if !db.token_revoke(name).map_err(|e| e.to_string())? {
                return Err(format!("no token named {name}"));
            }
            db.audit("cli", "token.revoke", name, "local");
            Ok(())
        }
        ["admin", "reset-password"] => {
            let db = open_db(&dir)?;
            if db.admin_get().map_err(|e| e.to_string())?.is_none() {
                return Err("no administrator exists yet; use the setup token".into());
            }
            let mut line = String::new();
            std::io::stdin().read_line(&mut line).map_err(|e| e.to_string())?;
            let password = line.trim_end_matches(['\n', '\r']);
            auth::valid_password(password)?;
            db.admin_set_password(&auth::hash_password(password)).map_err(|e| e.to_string())?;
            db.audit("cli", "password.reset", "all sessions deleted", "local");
            Ok(())
        }
        ["cert", "renew"] => certs::renew(&dir, &sans),
        ["cert", "install"] => {
            let (c, k) = (args.cert.as_ref().ok_or("--cert is required")?, args.key.as_ref().ok_or("--key is required")?);
            certs::install(&dir, c, k)
        }
        _ => Err(format!("unknown command\n{USAGE}")),
    }?;
    Ok(())
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    let args = match parse(std::env::args().skip(1).collect()) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    match run(args).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(1)
        }
    }
}
