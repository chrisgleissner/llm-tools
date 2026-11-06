//! Measurement harness (TEST-014/015). Not part of the normal run:
//! `cargo test --release --test bench -- --ignored bench --nocapture`
//! (or `deploy/bench.sh`). It starts the real binary on 127.0.0.1 and a fake worker, and prints a
//! Markdown table. Set `BENCH_OUT=FILE` to also write the table to a file.
mod common;
use bridge_coordinator::ops;
use common::*;
use futures_util::{SinkExt, StreamExt};
use reqwest::Method;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderName;
use tokio_tungstenite::tungstenite::Message as WsMsg;

const BIN: &str = env!("CARGO_BIN_EXE_bridge-coordinator");

struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn cli(dir: &Path, args: &[&str]) -> String {
    let o = Command::new(BIN).args(args).arg("--data-dir").arg(dir).output().unwrap();
    assert!(o.status.success(), "{args:?}: {}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8_lossy(&o.stdout).trim().to_string()
}

fn rss_kib(pid: u32) -> u64 {
    std::fs::read_to_string(format!("/proc/{pid}/status"))
        .unwrap()
        .lines()
        .find_map(|l| l.strip_prefix("VmRSS:"))
        .and_then(|v| v.split_whitespace().next()?.parse().ok())
        .unwrap_or(0)
}

/// Peak resident set size (VmHWM) since process start.
fn peak_rss_kib(pid: u32) -> u64 {
    std::fs::read_to_string(format!("/proc/{pid}/status"))
        .unwrap()
        .lines()
        .find_map(|l| l.strip_prefix("VmHWM:"))
        .and_then(|v| v.split_whitespace().next()?.parse().ok())
        .unwrap_or(0)
}

fn db_bytes(dir: &Path) -> u64 {
    let db = bridge_coordinator::db_path(dir);
    let len = |p: PathBuf| std::fs::metadata(p).map(|m| m.len()).unwrap_or(0);
    len(db.clone()) + len(PathBuf::from(format!("{}-wal", db.display())))
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    sorted[(((sorted.len() - 1) as f64) * p).round() as usize]
}

fn p50_p95(mut v: Vec<f64>) -> (f64, f64) {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    (percentile(&v, 0.5), percentile(&v, 0.95))
}

async fn ws_connect(port: u16, tls: &std::sync::Arc<rustls::ClientConfig>, token: &str) -> Ws {
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

struct Worker {
    ws: Ws,
    generation: u64,
    seq: u64,
}

impl Worker {
    async fn connect(port: u16, tls: &std::sync::Arc<rustls::ClientConfig>, token: &str, cursor: u64) -> Worker {
        let mut w = Worker {
            ws: ws_connect(port, tls, token).await,
            generation: 0,
            seq: 0,
        };
        w.send(
            "hello",
            json!({"worker_id": "bench-wid", "name": "w1", "software_version": "bench", "protocol_versions": [1],
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
        let m = json!({"v": 1, "type": kind, "id": format!("m{}", self.seq), "worker_id": "bench-wid", "generation": self.generation, "payload": payload});
        self.ws.send(WsMsg::Text(m.to_string().into())).await.unwrap();
    }
    async fn recv_type(&mut self, kind: &str) -> Value {
        loop {
            match tokio::time::timeout(Duration::from_secs(30), self.ws.next()).await.expect("timeout") {
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
    /// Send events `after+1..=through`; ids are dense.
    async fn ingest(&mut self, after: u64, through: u64, batch: u64) {
        let text = "x".repeat(200);
        let mut lo = after;
        while lo < through {
            let hi = (lo + batch).min(through);
            let events: Vec<Value> = (lo + 1..=hi)
                .map(|i| json!({"id": i, "kind": "task-progress", "mission_id": "m1", "message": text, "ts": i}))
                .collect();
            self.send("events", json!({"after": lo, "through": hi, "events": events})).await;
            lo = hi;
        }
        // ping/pong: everything before it has been processed
        self.send("ping", json!({})).await;
        self.recv_type("pong").await;
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn bench_coordinator() {
    let dir = tmp_dir();
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    cli(&dir, &["init", "--hostname", "bench.test", "--bind", &format!("127.0.0.1:{port}")]);
    let ca = std::fs::read(dir.join("ca.pem")).unwrap();
    let tls = client_tls(&ca);

    // ---- startup to /healthz
    let t0 = Instant::now();
    let child = Command::new(BIN)
        .args(["serve", "--data-dir"])
        .arg(&dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid = child.id();
    let _server = Server(child);
    while ops::health(&dir).await.is_err() {
        assert!(t0.elapsed() < Duration::from_secs(30), "server did not become healthy");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let startup_ms = t0.elapsed().as_secs_f64() * 1000.0;
    tokio::time::sleep(Duration::from_secs(2)).await;
    let rss_idle = rss_kib(pid);

    // ---- admin, worker
    let client = reqwest::Client::builder().use_preconfigured_tls((*tls).clone()).build().unwrap();
    let mut api = Api {
        c: client,
        base: format!("https://127.0.0.1:{port}"),
        cookie: None,
        csrf: None,
        bearer: None,
    };
    let setup = std::fs::read_to_string(dir.join("setup-token")).unwrap();
    let (st, b) = api
        .send(
            api.bare(Method::POST, "/api/v1/auth/setup")
                .json(&json!({"setup_token": setup.trim(), "username": "admin", "password": PASSWORD})),
        )
        .await;
    assert_eq!(st, 201, "{b}");
    assert_eq!(api.login("admin", PASSWORD).await, 200);
    let token = api.add_worker("w1").await;
    let rss_login = rss_kib(pid);
    let db_empty = db_bytes(&dir);
    let mut worker = Worker::connect(port, &tls, &token, 0).await;

    // ---- 1000 events, RSS
    worker.ingest(0, 1000, 100).await;
    let rss_1000 = rss_kib(pid);
    let peak_1000 = peak_rss_kib(pid);

    // ---- API latency
    let mut missions = vec![];
    for _ in 0..100 {
        let t = Instant::now();
        let (st, _) = api.get("/api/v1/missions").await;
        assert_eq!(st, 200);
        missions.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    let mut commands = vec![];
    for i in 0..100 {
        let body = json!({"target": "w1", "request": request(&format!("bench-{i}"), "mission.list")});
        let t = Instant::now();
        let (st, b) = api.post("/api/v1/commands", body).await;
        assert!(st == 200 || st == 201 || st == 202, "{st} {b}");
        commands.push(t.elapsed().as_secs_f64() * 1000.0);
    }

    // ---- ingest throughput and DB growth: events 1001..=10000 in batches of 500
    let t = Instant::now();
    worker.ingest(1000, 10_000, 500).await;
    let secs = t.elapsed().as_secs_f64();
    let throughput = 9000.0 / secs;
    let growth = db_bytes(&dir).saturating_sub(db_empty);
    let rss_10k = rss_kib(pid);

    // ---- reconnect latency
    let mut reconnect = vec![];
    for _ in 0..20 {
        let _ = worker.ws.close(None).await;
        let t = Instant::now();
        worker = Worker::connect(port, &tls, &token, 10_000).await;
        reconnect.push(t.elapsed().as_secs_f64() * 1000.0);
    }

    let (m50, m95) = p50_p95(missions);
    let (c50, c95) = p50_p95(commands);
    let (r50, r95) = p50_p95(reconnect);
    let table = format!(
        "| Measurement | Result |\n|---|---|\n\
         | Startup to /healthz | {startup_ms:.0} ms |\n\
         | Idle RSS (2 s after healthy) | {:.1} MiB |\n\
         | RSS after admin setup and login (Argon2id, 19 MiB working memory) | {:.1} MiB |
\
         | RSS after 1000 events | {:.1} MiB |\n\
         | Peak RSS (VmHWM) up to and including the 1000-event load | {:.1} MiB |\n\
         | RSS after 10000 events | {:.1} MiB |\n\
         | GET /api/v1/missions p50 / p95 (100 requests) | {m50:.2} / {m95:.2} ms |\n\
         | POST /api/v1/commands p50 / p95 (100 requests) | {c50:.2} / {c95:.2} ms |\n\
         | Event ingest throughput (9000 events, batches of 500, 200-byte payload) | {throughput:.0} events/s |\n\
         | Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20) | {r50:.1} / {r95:.1} ms |\n\
         | Database growth for 10000 events (main file + WAL) | {:.2} MiB ({:.0} bytes/event) |\n",
        rss_idle as f64 / 1024.0,
        rss_login as f64 / 1024.0,
        rss_1000 as f64 / 1024.0,
        peak_1000 as f64 / 1024.0,
        rss_10k as f64 / 1024.0,
        growth as f64 / 1048576.0,
        growth as f64 / 10_000.0,
    );
    println!("\n{table}");
    if let Ok(out) = std::env::var("BENCH_OUT") {
        std::fs::write(out, &table).unwrap();
    }
    let _ = std::fs::remove_dir_all(dir);
}
