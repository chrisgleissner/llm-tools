//! DOG-024: a slow database commit must not stop the coordinator from accepting connections,
//! completing TLS handshakes and answering `/healthz`, requests that need the database must get a clear
//! 503 instead of hanging, and serving must recover when the commit ends.
//!
//! The stall is made the way a slow card makes one: a second connection holds the database write lock
//! for several seconds, so the coordinator's first write waits inside SQLite's busy handler while it
//! holds its connection mutex, and every other database call queues behind that mutex. The runtime has
//! two worker threads, as in production.
mod common;
use bridge_coordinator::db_path;
use common::*;
use rusqlite::Connection;
use serde_json::json;
use std::time::{Duration, Instant};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Error as WsError;

const HOLD: Duration = Duration::from_secs(6);

async fn fresh_client_get(env: &Env, path: &str) -> (Duration, Result<u16, String>) {
    let client = reqwest::Client::builder()
        .use_preconfigured_tls((*env.tls).clone())
        .timeout(Duration::from_secs(20))
        .build()
        .unwrap();
    let started = Instant::now();
    let r = client.get(format!("{}{path}", env.base())).send().await;
    (started.elapsed(), r.map(|r| r.status().as_u16()).map_err(|e| e.to_string()))
}

async fn handshake(env: &Env) -> Duration {
    let started = Instant::now();
    let tcp = TcpStream::connect(env.addr).await.unwrap();
    TlsConnector::from(env.tls.clone())
        .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
        .await
        .expect("TLS handshake");
    started.elapsed()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_slow_commit_leaves_the_server_responsive_and_it_recovers() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (mut fw, _) = Fw::connect_settled(&env, &token, "wid-stall", 0).await;

    // The write lock is taken by another connection (another process would behave the same).
    let path = db_path(&env.dir);
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let holder = std::thread::spawn(move || {
        let c = Connection::open(path).unwrap();
        c.execute_batch("BEGIN IMMEDIATE").unwrap();
        ready_tx.send(()).unwrap();
        std::thread::sleep(HOLD);
        c.execute_batch("COMMIT").unwrap();
    });
    ready_rx.recv().unwrap();
    let stall_started = Instant::now();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });

    // The worker's event batch is a write: it waits in SQLite's busy handler with the mutex held.
    fw.send(
        "events",
        json!({"after": 0, "through": 1, "events": [{"id": 1, "kind": "task-progress", "mission_id": "m1", "message": "x", "ts": 1}]}),
    )
    .await;
    let first = Instant::now();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        first.elapsed() < Duration::from_secs(1),
        "a 300 ms timer took {:?}: the runtime threads are stuck on the database",
        first.elapsed()
    );
    // Requests that need the database queue behind it.
    let queued: Vec<_> = (0..3)
        .map(|_| {
            let (base, tls) = (env.base(), env.tls.clone());
            tokio::spawn(async move {
                let client = reqwest::Client::builder()
                    .use_preconfigured_tls((*tls).clone())
                    .timeout(Duration::from_secs(30))
                    .build()
                    .unwrap();
                let started = Instant::now();
                let r = client.get(format!("{base}/api/v1/auth/state")).send().await.unwrap();
                let status = r.status().as_u16();
                (started.elapsed(), status, r.text().await.unwrap())
            })
        })
        .collect();
    // A worker connect, started now, needs the database as well.
    let connect = {
        let (addr, tls, token) = (env.addr, env.tls.clone(), token.clone());
        tokio::spawn(async move {
            let started = Instant::now();
            let tcp = TcpStream::connect(addr).await.unwrap();
            let tls = TlsConnector::from(tls)
                .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
                .await
                .unwrap();
            let mut req = format!("wss://127.0.0.1:{}/api/v1/worker/connect", addr.port()).into_client_request().unwrap();
            req.headers_mut().insert("authorization", format!("Bearer {token}").parse().unwrap());
            let r = tokio::time::timeout(Duration::from_secs(10), tokio_tungstenite::client_async(req, tls))
                .await
                .expect("worker connect hung");
            (r.map(|_| ()), started.elapsed())
        })
    };
    let tick = Instant::now();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        tick.elapsed() < Duration::from_secs(1),
        "the runtime did not run a 300 ms timer for {:?}: its threads are stuck on the database",
        tick.elapsed()
    );

    // While the stall lasts: handshakes, /healthz and the listener answer within a second.
    let mut slowest = Duration::ZERO;
    let mut probes = 0;
    while stall_started.elapsed() < Duration::from_secs(4) {
        let (took, status) = fresh_client_get(&env, "/healthz").await;
        assert_eq!(status, Ok(200), "/healthz during the stall");
        slowest = slowest.max(took).max(handshake(&env).await);
        probes += 1;
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    assert!(probes >= 10, "only {probes} probes ran during the stall");
    println!("{probes} probes of /healthz and TLS handshakes during the stall, slowest {slowest:?}");
    assert!(slowest < Duration::from_secs(1), "a probe took {slowest:?} while a commit was stalled");

    // A worker that tries to connect got an answer: 503, not a hang.
    let (connect, connect_took) = connect.await.unwrap();
    match connect {
        Err(WsError::Http(resp)) => assert_eq!(resp.status().as_u16(), 503, "worker connect during the stall"),
        other => panic!("worker connect during the stall: {other:?}"),
    }
    assert!(connect_took < Duration::from_secs(5), "the worker connect answered after {connect_took:?}");

    // The queued reads were answered with a clear 503 and a problem document, not left hanging.
    for q in queued {
        let (took, status, body) = q.await.unwrap();
        assert_eq!(status, 503, "{body}");
        assert!(body.contains("database-busy") && body.contains("retry"), "{body}");
        assert!(took < Duration::from_secs(6), "a queued request waited {took:?}");
    }

    // After the commit ends serving is back, including worker connects and writes.
    holder.join().unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    let (took, status) = fresh_client_get(&env, "/api/v1/auth/state").await;
    assert_eq!(status, Ok(200));
    assert!(took < Duration::from_secs(1), "{took:?}");
    let (mut again, _) = Fw::connect_settled(&env, &token, "wid-stall", 0).await;
    again
        .send(
            "events",
            json!({"after": 0, "through": 1, "events": [{"id": 1, "kind": "task-progress", "mission_id": "m1", "message": "x", "ts": 1}]}),
        )
        .await;
    again.barrier().await;
    let (_, list) = api.get("/api/v1/workers").await;
    assert_eq!(list["workers"][0]["name"], "w1");
    let (took, status) = fresh_client_get(&env, "/healthz").await;
    assert_eq!((status, took < Duration::from_secs(1)), (Ok(200), true));
    env.stop().await;
}
