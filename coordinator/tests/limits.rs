//! Global connection cap and idle timeouts (NET-010).
mod common;
use common::*;
use futures_util::StreamExt;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::tungstenite::Message as WsMsg;

async fn tls_connect(env: &Env) -> tokio_rustls::client::TlsStream<TcpStream> {
    let tcp = TcpStream::connect(env.addr).await.unwrap();
    TlsConnector::from(env.tls.clone())
        .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
        .await
        .unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn connections_over_the_cap_are_closed_promptly() {
    let env = boot_with(|t| t.replace("max_connections = 256", "max_connections = 2")).await;
    let a = TcpStream::connect(env.addr).await.unwrap();
    let b = TcpStream::connect(env.addr).await.unwrap();
    // Let the server accept both before the third arrives.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut c = TcpStream::connect(env.addr).await.unwrap();
    let mut buf = [0u8; 16];
    let started = Instant::now();
    let r = tokio::time::timeout(Duration::from_secs(3), c.read(&mut buf))
        .await
        .expect("refused without hanging");
    assert!(matches!(r, Ok(0) | Err(_)), "{r:?}");
    assert!(started.elapsed() < Duration::from_secs(3));
    // Slots are released when the idle connections go away.
    drop((a, b));
    let mut ok = false;
    for _ in 0..30 {
        if let Ok(r) = env.client.get(format!("{}/healthz", env.base())).send().await {
            ok = r.status() == 200;
            if ok {
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok, "server accepts again after slots free up");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn idle_keep_alive_connection_is_closed() {
    let env = boot_with(|t| t.replace("http_idle_timeout_secs = 30", "http_idle_timeout_secs = 1")).await;
    let mut s = tls_connect(&env).await;
    s.write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n").await.unwrap();
    let started = Instant::now();
    let mut buf = [0u8; 2048];
    let mut got_response = false;
    loop {
        match tokio::time::timeout(Duration::from_secs(6), s.read(&mut buf)).await.expect("closed within 6 s") {
            Ok(0) | Err(_) => break,
            Ok(_) => got_response = true,
        }
    }
    assert!(got_response);
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "closed after the idle timeout, not the TLS or drain limits"
    );
    env.stop().await;
}

fn fast_ws(t: String) -> String {
    t.replace("ws_ping_interval_secs = 30", "ws_ping_interval_secs = 1")
        .replace("ws_idle_timeout_secs = 90", "ws_idle_timeout_secs = 2")
}

fn session_headers(api: &Api) -> Vec<(String, String)> {
    vec![
        ("cookie".into(), format!("__Host-bridge_session={}", api.cookie.as_ref().unwrap())),
        ("origin".into(), api.base.clone()),
    ]
}

async fn stream(env: &Env, api: &Api) -> Ws {
    let h = session_headers(api);
    let refs: Vec<(&str, &str)> = h.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
    env.ws("/api/v1/stream", &refs).await.unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn silent_stream_client_is_closed_after_idle_timeout() {
    let env = boot_with(fast_ws).await;
    let api = env.admin().await;
    let mut ws = stream(&env, &api).await;
    // Not reading means no pongs go out.
    tokio::time::sleep(Duration::from_millis(4500)).await;
    // A client that never read its queued pings may see a reset instead of the close frame.
    let code = expect_close(&mut ws).await;
    assert!(matches!(code, None | Some(1001)), "{code:?}");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_client_that_answers_pings_stays_open() {
    let env = boot_with(fast_ws).await;
    let api = env.admin().await;
    let mut ws = stream(&env, &api).await;
    let mut pings = 0;
    let end = Instant::now() + Duration::from_secs(5);
    while let Ok(m) = tokio::time::timeout(end.saturating_duration_since(Instant::now()), ws.next()).await {
        match m {
            Some(Ok(WsMsg::Ping(_))) => pings += 1,
            Some(Ok(WsMsg::Pong(_))) => {}
            other => panic!("connection ended early: {other:?}"),
        }
    }
    assert!(pings >= 3, "server pinged {pings} times");
    env.stop().await;
}
