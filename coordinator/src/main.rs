//! `bridge-coordinator` command line (see docs/distributed/coordinator.md).

use bridge_coordinator::admin_reset;
use bridge_coordinator::certs::{self, Sans};
use bridge_coordinator::config;
use bridge_coordinator::db::Db;
use bridge_coordinator::{auth, db_path, ops, protocol, scopes, server};
use std::net::IpAddr;
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "usage: bridge-coordinator <command> [--data-dir DIR]
  init [--hostname NAME]... [--ip ADDR]... [--bind ADDR:PORT]
                                (no --hostname/--ip: localhost, this machine's host name,
                                 <host>.home.arpa and its non-loopback addresses)
  info                          (URL, CA path and SHA-256, bind address, LAN state)
  check-config                  (validate config.toml and the BRIDGE_COORDINATOR_* variables; lists every
                                 error with key, value and fix; exit 0 valid, 1 not; --web-dir as for serve)
  health                        (HTTPS GET /healthz with the local CA; exit 0 healthy, 1 not)
  backup --output FILE | restore --input FILE   (restore needs the server stopped)
  setup-token                   (new setup token; only while no administrator exists)
  serve [--web-dir DIR]
  worker add NAME | worker revoke NAME
  admin reset-password [--temporary] [--keep-api-tokens] [--username NAME]
                                (on a terminal: asks for the new password twice, without echo;
                                 otherwise reads it from stdin. --temporary generates a one-time
                                 password, prints it once and forces a change at the next web
                                 sign-in. Deletes all sessions; revokes all API tokens unless kept.
                                 There is exactly one administrator account, so --username is only
                                 checked against its name. Run as the service user, or as root:
                                 the database files then keep the data directory's owner)
  cert renew [--hostname NAME]... [--ip ADDR]...   (new server certificate, same CA; names must be
                                                    within the CA's name constraints)
  cert new-ca [--hostname NAME]... [--ip ADDR]...  (new name-constrained CA and server certificate;
                                                    the old CA is kept as ca.pem.old-<UTC>)
  cert install --cert FILE --key FILE
  token create NAME [--scope SCOPE]... [--expires-days N] | token revoke NAME
                                (default scope read, default expiry 90 days, 0 = never)
--data-dir defaults to $BRIDGE_COORDINATOR_DATA_DIR, then /var/lib/bridge-coordinator.
Environment: BRIDGE_COORDINATOR_BIND (host:port) and BRIDGE_COORDINATOR_LAN_ENABLED (true/false)
override config.toml.";

/// API tokens expire after this many days unless `--expires-days` says otherwise.
const DEFAULT_TOKEN_DAYS: u32 = 90;

struct Args {
    data_dir: Option<PathBuf>,
    hostnames: Vec<String>,
    ips: Vec<IpAddr>,
    cert: Option<PathBuf>,
    key: Option<PathBuf>,
    web_dir: Option<PathBuf>,
    output: Option<PathBuf>,
    input: Option<PathBuf>,
    bind: Option<String>,
    expires_days: Option<u32>,
    scopes: Vec<String>,
    keep_api_tokens: bool,
    temporary: bool,
    username: Option<String>,
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
        output: None,
        input: None,
        bind: None,
        expires_days: None,
        scopes: vec![],
        keep_api_tokens: false,
        temporary: false,
        username: None,
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
            "--output" => a.output = Some(value("--output")?.into()),
            "--input" => a.input = Some(value("--input")?.into()),
            "--bind" => a.bind = Some(value("--bind")?),
            "--expires-days" => {
                let v = value("--expires-days")?;
                a.expires_days = Some(v.parse().map_err(|_| format!("--expires-days must be a whole number of days, not {v:?}"))?);
            }
            "--scope" => a.scopes.push(value("--scope")?),
            "--keep-api-tokens" => a.keep_api_tokens = true,
            "--temporary" => a.temporary = true,
            "--username" => a.username = Some(value("--username")?),
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
        .or_else(|| std::env::var_os(config::ENV_DATA_DIR).filter(|v| !v.is_empty()).map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from(config::DEFAULT_DATA_DIR));
    // Explicit --hostname/--ip replace discovery; with neither, discover this machine's names.
    let sans = if args.hostnames.is_empty() && args.ips.is_empty() {
        bridge_coordinator::netinfo::discovered_sans()
    } else {
        Sans {
            hostnames: args.hostnames.clone(),
            ips: args.ips.clone(),
        }
    };
    match words.as_slice() {
        ["init"] => bridge_coordinator::init::init_with(&dir, &sans, args.bind.as_deref()),
        ["info"] => {
            print!("{}", ops::report(&dir, false)?);
            Ok(())
        }
        ["check-config"] => {
            let cfg = config::Config::load_checked(&dir, args.web_dir.is_some())?;
            println!(
                "config ok: {} (bind {}, lan_enabled {})",
                dir.join("config.toml").display(),
                cfg.bind,
                cfg.lan_enabled
            );
            Ok(())
        }
        ["health"] => {
            ops::health(&dir).await?;
            println!("ok");
            Ok(())
        }
        ["backup"] => {
            let out = args.output.as_ref().ok_or("--output is required")?;
            ops::backup(&dir, out)?;
            println!(
                "backup written to {} (the files listed in docs/distributed/install.md are not part of it)",
                out.display()
            );
            Ok(())
        }
        ["restore"] => {
            let input = args.input.as_ref().ok_or("--input is required")?;
            let kept = ops::restore(&dir, input)?;
            println!("restored; the previous database is kept as {}", kept.display());
            print!("{}", ops::restored_credentials(&dir)?);
            Ok(())
        }
        ["setup-token"] => {
            let path = ops::setup_token(&dir)?;
            println!("new setup token written to {}", path.display());
            Ok(())
        }
        ["serve"] => {
            let srv = server::start_with(&dir, args.web_dir.as_deref()).await?;
            eprintln!("bridge-coordinator listening on https://{}", srv.addr);
            #[cfg(unix)]
            {
                let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).map_err(|e| e.to_string())?;
                tokio::select! { _ = term.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
            }
            #[cfg(not(unix))]
            {
                let _ = tokio::signal::ctrl_c().await;
            }
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
            let days = args.expires_days.unwrap_or(DEFAULT_TOKEN_DAYS);
            let expires_at = (days > 0).then(|| bridge_coordinator::now() + i64::from(days) * 86_400);
            let token_scopes = if args.scopes.is_empty() {
                vec![scopes::READ.to_string()]
            } else {
                args.scopes.clone()
            };
            if token_scopes.iter().any(|scope| !scopes::valid(scope)) {
                return Err(format!("unknown token scope; choose from {}", scopes::ALL.join(", ")));
            }
            if token_scopes.iter().collect::<std::collections::BTreeSet<_>>().len() != token_scopes.len() {
                return Err("token scopes must not be repeated".into());
            }
            let db = open_db(&dir)?;
            let token = auth::random_token();
            if !db
                .token_create_scoped(name, &auth::sha256_hex(&token), expires_at, &token_scopes)
                .map_err(|e| e.to_string())?
            {
                return Err(format!("token {name} already exists"));
            }
            let detail = if days == 0 {
                format!("{name} (never expires)")
            } else {
                format!("{name} (expires in {days} days)")
            };
            db.audit("cli", "token.create", &detail, "local");
            eprintln!("note: token scopes: {}", token_scopes.join(", "));
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
            let opts = admin_reset::Options {
                username: args.username.clone(),
                keep_api_tokens: args.keep_api_tokens,
                temporary: args.temporary,
            };
            let report = admin_reset::reset_password(&db, &opts, &mut admin_reset::TerminalInput::new());
            drop(db);
            admin_reset::restore_ownership(&dir);
            print!("{}", report?.text(args.keep_api_tokens));
            Ok(())
        }
        ["cert", "renew"] => certs::renew(&dir, &sans),
        ["cert", "new-ca"] => {
            let stamp = certs::new_ca(&dir, &sans)?;
            println!("new name-constrained CA and server certificate written to {}", dir.display());
            println!("previous CA kept as ca.pem.old-{stamp} and ca.key.old-{stamp}; delete ca.key.old-{stamp} once nothing needs the old CA");
            println!("new CA SHA-256: {}", ops::ca_fingerprint(&dir)?);
            println!(
                "Every browser must import the new ca.pem and every worker must be re-enrolled with it \
                 (workers pin the CA they were enrolled with); restart the coordinator to serve the new certificate."
            );
            Ok(())
        }
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
            // Configuration errors are complete lines already.
            if e.starts_with("config error:") {
                eprintln!("{e}");
            } else {
                eprintln!("error: {e}");
            }
            ExitCode::from(1)
        }
    }
}
