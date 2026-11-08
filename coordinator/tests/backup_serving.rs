//! DOG-024: a backup must not stop the coordinator from serving.
//!
//! The test builds a large database (the shape of a Raspberry Pi coordinator that has been through
//! a replication flood: events, finished commands with bulky results, audit rows and free pages),
//! starts the real `bridge-coordinator serve` as a separate process, keeps a fake worker writing
//! event batches the whole time (as a connected worker does), polls `/healthz` and
//! `/api/v1/auth/state` four times a second, and runs `bridge-coordinator backup` as a second process.
//! It requires that
//!
//! * no polled request takes 1 s or longer,
//! * the backup finishes within `BACKUP_TEST_SECS` (default 90 s) although the worker keeps writing,
//! * the copy passes `integrity_check`, holds the same rows as the live database at the moment the
//!   backup started (at least) and is smaller than the source when the source has free pages.
//!
//! Environment (all optional):
//! `BACKUP_TEST_MB` live data in MiB (default 100), `BACKUP_TEST_BLOAT_MB` free pages (default 60),
//! `BACKUP_TEST_BIN` coordinator binary to serve and back up with (default: the one under test; set it
//! to a binary built before the fix to see the old behaviour), `BACKUP_TEST_FLAGS` extra backup flags
//! (default: `--max-mb-s 40` when the binary under test is used), `BACKUP_TEST_OUT` a file that
//! receives the measurements, `BACKUP_TEST_KEEP` a directory to keep the generated database in.
mod common;
use bridge_coordinator::{db::Db, db_path, ops};
use common::*;
use futures_util::{SinkExt, StreamExt};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderName;
use tokio_tungstenite::tungstenite::Message as WsMsg;

const BIN: &str = env!("CARGO_BIN_EXE_bridge-coordinator");

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn cli(bin: &str, dir: &Path, args: &[&str]) -> String {
    let o = Command::new(bin).args(args).arg("--data-dir").arg(dir).output().unwrap();
    assert!(o.status.success(), "{args:?}: {}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8_lossy(&o.stdout).trim().to_string()
}

/// A small deterministic generator of JSON-like text, so the fixture compresses the way real events,
/// results and audit details do (random bytes would not).
struct Text(u64);
impl Text {
    const WORDS: &'static [&'static str] = &[
        "task",
        "progress",
        "mission",
        "provider",
        "capacity",
        "refresh",
        "operation",
        "verified",
        "claude",
        "codex",
        "running",
        "paused",
        "branch",
        "commit",
        "checkpoint",
        "prompt",
        "assembled",
        "controller",
        "heartbeat",
        "status",
        "repository",
        "worker",
        "session",
        "restored",
        "native",
        "suspend",
        "decision",
        "wake",
        "plan",
        "usage",
        "weekly",
        "window",
        "reset",
    ];
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn sentence(&mut self, bytes: usize) -> String {
        let mut s = String::with_capacity(bytes + 16);
        while s.len() < bytes {
            s.push_str(Self::WORDS[self.next() as usize % Self::WORDS.len()]);
            s.push(if self.next().is_multiple_of(7) { '\n' } else { ' ' });
        }
        s.truncate(bytes);
        s.replace('\n', "\\n")
    }
}

/// Fill a new database with about `live_mb` MiB of data in use and `bloat_mb` MiB of free pages.
/// Returns the row counts.
fn make_fixture(dir: &Path, live_mb: u64, bloat_mb: u64) -> (u64, u64, u64) {
    let path = db_path(dir);
    let db = Db::open(&path).unwrap();
    // w1 was enrolled by the CLI; the other two only give the event table more than one partition.
    for w in ["w2", "w3"] {
        assert!(db.worker_add(w, &format!("hash-{w}")).unwrap());
    }
    drop(db);
    let mut c = Connection::open(&path).unwrap();
    c.pragma_update(None, "synchronous", "OFF").unwrap();
    let mut text = Text(7);
    let mb = 1024 * 1024u64;
    let now = bridge_coordinator::now();
    let events_per_worker = ((live_mb * mb * 35 / 100) / 3 / 400).clamp(1, 50_000);
    let audit_rows = ((live_mb * mb * 15 / 100) / 260).clamp(1, 100_000);
    let command_rows = (live_mb * mb - events_per_worker * 3 * 400 - audit_rows * 260) / 6_300;
    let tx = c.transaction().unwrap();
    {
        let mut e = tx.prepare("INSERT INTO events(worker,id,body) VALUES(?,?,?)").unwrap();
        for w in ["w1", "w2", "w3"] {
            for id in 1..=events_per_worker {
                let body = json!({"id": id, "kind": "task-progress", "mission_id": "m1", "ts": id, "message": text.sentence(250)});
                e.execute(params![w, id as i64, body.to_string()]).unwrap();
            }
            tx.execute("UPDATE workers SET event_cursor=? WHERE name=?", params![events_per_worker as i64, w])
                .unwrap();
        }
        let mut cmd = tx
            .prepare(
                "INSERT INTO commands(request_id,worker,operation,mutating,request,state,result,created_at,updated_at) \
                 VALUES(?,?,?,?,?,'done',?,?,?)",
            )
            .unwrap();
        for i in 0..command_rows {
            let result = json!({"outcome": "ok", "result": {"text": text.sentence(6_000)}});
            let at = now - 60 * 60 - (text.next() % (3 * 86_400)) as i64;
            cmd.execute(params![
                format!("req-{i:08}"),
                ["w1", "w2", "w3"][i as usize % 3],
                if i % 5 == 0 { "task.add" } else { "mission.status" },
                (i % 5 == 0) as i64,
                json!({"schema_version": 1, "operation": "mission.status", "request_id": format!("req-{i:08}")}).to_string(),
                result.to_string(),
                at,
                at
            ])
            .unwrap();
        }
        let mut a = tx
            .prepare("INSERT INTO audit(ts,actor,action,detail,remote,principal_id) VALUES(?,?,?,?,?,'system')")
            .unwrap();
        for i in 0..audit_rows {
            a.execute(params![now - i as i64, "token:cli", "api.request", text.sentence(150), "127.0.0.1"])
                .unwrap();
        }
    }
    tx.commit().unwrap();
    if bloat_mb > 0 {
        c.execute_batch("CREATE TABLE junk(b BLOB);").unwrap();
        let tx = c.transaction().unwrap();
        {
            let mut j = tx.prepare("INSERT INTO junk(b) VALUES(?)").unwrap();
            let blob = text.sentence(60_000).into_bytes();
            for _ in 0..bloat_mb * mb / 60_000 {
                j.execute([&blob]).unwrap();
            }
        }
        tx.commit().unwrap();
        c.execute_batch("DROP TABLE junk;").unwrap();
    }
    c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(())).unwrap();
    (events_per_worker * 3, command_rows, audit_rows)
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_rustls::client::TlsStream<TcpStream>>;

async fn ws_connect(port: u16, tls: &Arc<rustls::ClientConfig>, token: &str) -> Ws {
    let tcp = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    let tls = TlsConnector::from(tls.clone())
        .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
        .await
        .unwrap();
    let mut req = format!("wss://127.0.0.1:{port}/api/v1/worker/connect").into_client_request().unwrap();
    req.headers_mut()
        .insert("authorization".parse::<HeaderName>().unwrap(), format!("Bearer {token}").parse().unwrap());
    tokio_tungstenite::client_async(req, tls).await.unwrap().0
}

/// A connected worker that sends numbered event batches and waits for each to be processed.
struct Worker {
    ws: Ws,
    generation: u64,
    seq: u64,
    cursor: u64,
}

impl Worker {
    async fn connect(port: u16, tls: &Arc<rustls::ClientConfig>, token: &str, cursor: u64) -> Worker {
        let mut w = Worker {
            ws: ws_connect(port, tls, token).await,
            generation: 0,
            seq: 0,
            cursor,
        };
        w.send(
            "hello",
            json!({"worker_id": "dog024-wid", "name": "w1", "software_version": "test", "protocol_versions": [1],
            "platform": {"system": "Linux"}, "capabilities": caps(),
            "missions": [{"id": "m1", "name": "mission", "status": "running", "folder": "proj", "updated_at": null}], "event_cursor": cursor}),
        )
        .await;
        let welcome = w.recv_type("welcome").await;
        w.generation = welcome["payload"]["generation"].as_u64().unwrap();
        w
    }
    async fn send(&mut self, kind: &str, payload: Value) {
        self.seq += 1;
        let m = json!({"v": 1, "type": kind, "id": format!("m{}", self.seq), "worker_id": "dog024-wid", "generation": self.generation, "payload": payload});
        self.ws.send(WsMsg::Text(m.to_string().into())).await.unwrap();
    }
    async fn recv_type(&mut self, kind: &str) -> Value {
        loop {
            match tokio::time::timeout(Duration::from_secs(60), self.ws.next()).await.expect("worker: timeout") {
                Some(Ok(WsMsg::Text(t))) => {
                    let v: Value = serde_json::from_str(t.as_str()).unwrap();
                    assert_ne!(v["type"], "error", "{v}");
                    if v["type"] == kind {
                        return v;
                    }
                }
                Some(Ok(_)) => {}
                other => panic!("worker connection ended: {other:?}"),
            }
        }
    }
    /// One batch of 25 events; returns once the coordinator has stored it (the pong follows it).
    async fn batch(&mut self) -> Duration {
        let started = Instant::now();
        let (after, through) = (self.cursor, self.cursor + 25);
        let events: Vec<Value> = (after + 1..=through)
            .map(|i| json!({"id": i, "kind": "task-progress", "mission_id": "m1", "message": "x".repeat(200), "ts": i}))
            .collect();
        self.send("events", json!({"after": after, "through": through, "events": events})).await;
        self.send("ping", json!({})).await;
        self.recv_type("pong").await;
        self.cursor = through;
        started.elapsed()
    }
}

#[derive(Default, Debug)]
struct Latency {
    count: u64,
    slowest: Duration,
    over_one_second: u64,
}

impl Latency {
    fn add(&mut self, d: Duration) {
        self.count += 1;
        self.slowest = self.slowest.max(d);
        if d >= Duration::from_secs(1) {
            self.over_one_second += 1;
        }
    }
}

/// Bytes the process has passed to write calls (Linux only; `None` elsewhere or once it exited).
fn written_bytes(pid: u32) -> Option<u64> {
    std::fs::read_to_string(format!("/proc/{pid}/io"))
        .ok()?
        .lines()
        .find_map(|l| l.strip_prefix("wchar:"))
        .and_then(|v| v.trim().parse().ok())
}

fn rows(path: &Path) -> (i64, i64, i64) {
    let c = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let n = |t: &str| c.query_row(&format!("SELECT COUNT(*) FROM {t}"), [], |r| r.get::<_, i64>(0)).unwrap();
    (n("events"), n("commands"), n("audit"))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_backup_of_a_large_database_does_not_stop_serving() {
    let bin = std::env::var("BACKUP_TEST_BIN").unwrap_or_else(|_| BIN.to_string());
    let old_binary = bin != BIN;
    let flags: Vec<String> = match std::env::var("BACKUP_TEST_FLAGS") {
        Ok(f) => f.split_whitespace().map(str::to_string).collect(),
        Err(_) if old_binary => vec![],
        // The product default write cap (6 MiB/s) is kept: at 40 MiB/s the copy saturated the shared disk of
        // the CI runners and a commit (and the request queued behind it) took 1.2 s on Linux and 3 s on Windows.
        Err(_) => vec!["--assume-mb-s".into(), "100".into()],
    };
    let live_mb = env_u64("BACKUP_TEST_MB", 100);
    let bloat_mb = env_u64("BACKUP_TEST_BLOAT_MB", 60);
    let limit = Duration::from_secs(env_u64("BACKUP_TEST_SECS", 90));
    let dir = std::env::var_os("BACKUP_TEST_KEEP").map(PathBuf::from).unwrap_or_else(tmp_dir);
    std::fs::create_dir_all(&dir).unwrap();
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    cli(&bin, &dir, &["init", "--hostname", "dog024.test", "--bind", &format!("127.0.0.1:{port}")]);
    let token = cli(&bin, &dir, &["worker", "add", "w1"]);
    let t = Instant::now();
    let (events, commands, audit) = make_fixture(&dir, live_mb, bloat_mb);
    let source_bytes = std::fs::metadata(db_path(&dir)).unwrap().len();
    let mut report = format!(
        "fixture: {events} events, {commands} finished commands, {audit} audit rows; database file {:.1} MiB (generated in {:.0} s)\n",
        source_bytes as f64 / 1048576.0,
        t.elapsed().as_secs_f64()
    );
    let ca = std::fs::read(dir.join("ca.pem")).unwrap();
    let tls = client_tls(&ca);
    let child = Command::new(&bin)
        .args(["serve", "--data-dir"])
        .arg(&dir)
        .stdout(Stdio::null())
        .stderr(std::fs::File::create(dir.join("serve.log")).unwrap())
        .spawn()
        .unwrap();
    let _server = Server(child);
    let serve_log = dir.join("serve.log");
    let started = Instant::now();
    while ops::health(&dir).await.is_err() {
        assert!(started.elapsed() < Duration::from_secs(60), "server did not become healthy");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let client = reqwest::Client::builder()
        .use_preconfigured_tls((*tls).clone())
        .timeout(Duration::from_secs(120))
        .build()
        .unwrap();
    let mut worker = Worker::connect(port, &tls, &token, events / 3).await;

    let stop = Arc::new(AtomicBool::new(false));
    // Pollers: the process health route (no database) and the auth state route (reads the database).
    let poll = |path: &'static str| {
        let (client, stop, serve_log) = (client.clone(), stop.clone(), serve_log.clone());
        tokio::spawn(async move {
            let mut lat = Latency::default();
            while !stop.load(Ordering::Relaxed) {
                let at = Instant::now();
                let r = client.get(format!("https://127.0.0.1:{port}{path}")).send().await;
                lat.add(at.elapsed());
                let seen = match &r {
                    Ok(r) => format!("status {}", r.status()),
                    Err(e) => format!("error {e:?}"),
                };
                assert!(
                    r.is_ok_and(|r| r.status().is_success()),
                    "{path} failed during the backup after {:.1} s: {seen}; server log: {}",
                    at.elapsed().as_secs_f64(),
                    std::fs::read_to_string(&serve_log).unwrap_or_default()
                );
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            lat
        })
    };
    let health = poll("/healthz");
    let state = poll("/api/v1/auth/state");
    // Writer: one event batch every 50 ms for as long as the backup runs.
    let writer_stop = stop.clone();
    let writer = tokio::spawn(async move {
        let mut lat = Latency::default();
        while !writer_stop.load(Ordering::Relaxed) {
            lat.add(worker.batch().await);
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        lat
    });
    tokio::time::sleep(Duration::from_secs(2)).await; // baseline traffic

    let out = dir.join("backup.sqlite3");
    let before = rows(&db_path(&dir));
    // Control run for measurements: the same traffic and fixture, no backup.
    let control = std::env::var_os("BACKUP_TEST_CONTROL").is_some();
    let mut backup = Command::new(if control { "sleep" } else { &bin })
        .args(if control { vec!["25".to_string()] } else { vec![] })
        .args(if control { vec![] } else { vec!["backup", "--output"] })
        .args(if control { vec![] } else { vec![out.to_str().unwrap()] })
        .args(if control { &[][..] } else { &flags[..] })
        .args(if control { vec![] } else { vec!["--data-dir", dir.to_str().unwrap()] })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let backup_started = Instant::now();
    let mut backup_written = 0u64;
    let finished = loop {
        if let Some(status) = backup.try_wait().unwrap() {
            break Some(status);
        }
        backup_written = written_bytes(backup.id()).unwrap_or(backup_written);
        if backup_started.elapsed() > limit {
            let _ = backup.kill();
            let _ = backup.wait();
            break None;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    let backup_secs = backup_started.elapsed();
    let backup_output = backup.wait_with_output().unwrap();
    let backup_text = format!(
        "{}{}",
        String::from_utf8_lossy(&backup_output.stdout),
        String::from_utf8_lossy(&backup_output.stderr)
    );
    tokio::time::sleep(Duration::from_secs(1)).await;
    stop.store(true, Ordering::Relaxed);
    let (health, state, writer) = (health.await.unwrap(), state.await.unwrap(), writer.await.unwrap());
    report += &format!(
        "backup: {} in {:.1} s (limit {} s) with {}\n\
         /healthz: {} requests, slowest {:.0} ms, {} at or over 1 s\n\
         /api/v1/auth/state: {} requests, slowest {:.0} ms, {} at or over 1 s\n\
         backup process bytes written (wchar): {:.0} MiB\n\
         worker event batches: {} stored, slowest {:.0} ms, {} at or over 1 s\n",
        match &finished {
            Some(s) if s.success() => "finished".to_string(),
            Some(s) => format!("failed ({s})"),
            None => "DID NOT FINISH, killed".to_string(),
        },
        backup_secs.as_secs_f64(),
        limit.as_secs(),
        if flags.is_empty() { "default options".to_string() } else { flags.join(" ") },
        health.count,
        health.slowest.as_secs_f64() * 1000.0,
        health.over_one_second,
        state.count,
        state.slowest.as_secs_f64() * 1000.0,
        state.over_one_second,
        backup_written as f64 / 1048576.0,
        writer.count,
        writer.slowest.as_secs_f64() * 1000.0,
        writer.over_one_second,
    );
    if !control && finished.as_ref().is_some_and(|s| s.success()) {
        let copy_bytes = std::fs::metadata(&out).unwrap().len();
        report += &format!(
            "copy: {:.1} MiB (source file {:.1} MiB)\n",
            copy_bytes as f64 / 1048576.0,
            source_bytes as f64 / 1048576.0
        );
    }
    report += &format!("backup output:\n{}\n", backup_text.trim_end());
    println!("{report}");
    if let Some(file) = std::env::var_os("BACKUP_TEST_OUT") {
        std::fs::write(file, &report).unwrap();
    }

    if control {
        return;
    }
    assert!(
        finished.is_some(),
        "the backup did not finish in {limit:?} while the worker kept writing\n{report}"
    );
    assert!(finished.unwrap().success(), "the backup failed\n{report}");
    assert_eq!(
        health.over_one_second + state.over_one_second,
        0,
        "a request took 1 s or longer during the backup\n{report}\nserver log: {}",
        std::fs::read_to_string(&serve_log).unwrap_or_default()
    );
    // The copy is a consistent database that holds at least what the live one held when the backup began.
    let copy = rows(&out);
    assert!(
        copy.0 >= before.0 && copy.1 >= before.1 && copy.2 >= before.2,
        "copy {copy:?} lacks rows of {before:?}"
    );
    let version = Connection::open(&out)
        .unwrap()
        .query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
        .unwrap();
    assert_eq!(version, "ok");
    if !old_binary && bloat_mb >= 20 {
        assert!(
            std::fs::metadata(&out).unwrap().len() + (bloat_mb * 1048576) / 2 < source_bytes,
            "the copy must not carry the free pages\n{report}"
        );
    }
    drop(_server);
    if std::env::var_os("BACKUP_TEST_KEEP").is_none() {
        let _ = std::fs::remove_dir_all(&dir);
    }
}
