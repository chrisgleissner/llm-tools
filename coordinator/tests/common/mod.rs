#![allow(dead_code)]
use bridge_coordinator::certs::Sans;
use bridge_coordinator::server::{self, RunningServer};
use futures_util::{SinkExt, StreamExt};
use reqwest::{Client, Method, RequestBuilder};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::ServerName;
use serde_json::{json, Value};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpStream;
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderName;
use tokio_tungstenite::tungstenite::Message as WsMsg;
use tokio_tungstenite::WebSocketStream;

pub type Ws = WebSocketStream<TlsStream<TcpStream>>;
static COUNTER: AtomicU64 = AtomicU64::new(0);
pub const PASSWORD: &str = "correct horse battery";

pub fn tmp_dir() -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos();
    std::env::temp_dir().join(format!("bridge-coord-test-{}-{n}-{nanos}", std::process::id()))
}

pub fn sans() -> Sans {
    Sans {
        hostnames: vec![],
        ips: vec![],
    }
}

pub fn client_tls(ca: &[u8]) -> Arc<rustls::ClientConfig> {
    let mut roots = rustls::RootCertStore::empty();
    for c in rustls::pki_types::CertificateDer::pem_slice_iter(ca) {
        roots.add(c.unwrap()).unwrap();
    }
    Arc::new(
        rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_root_certificates(roots)
            .with_no_client_auth(),
    )
}

pub struct Env {
    pub dir: PathBuf,
    pub srv: RunningServer,
    pub addr: SocketAddr,
    pub tls: Arc<rustls::ClientConfig>,
    pub client: Client,
}

pub fn edit_config(dir: &Path, f: impl FnOnce(String) -> String) {
    let p = dir.join("config.toml");
    let text = std::fs::read_to_string(&p).unwrap();
    std::fs::write(&p, f(text)).unwrap();
}

/// Initialise a data directory with the production init code and start the real server on 127.0.0.1:0.
pub async fn boot_with(config: impl FnOnce(String) -> String) -> Env {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    edit_config(&dir, |t| config(t.replace("8443", "0")));
    let srv = server::start(&dir).await.unwrap();
    let ca = std::fs::read(dir.join("ca.pem")).unwrap();
    let tls = client_tls(&ca);
    let client = Client::builder().use_preconfigured_tls((*tls).clone()).build().unwrap();
    Env {
        addr: srv.addr,
        dir,
        srv,
        tls,
        client,
    }
}

pub async fn boot() -> Env {
    boot_with(|t| t).await
}

impl Env {
    pub fn base(&self) -> String {
        format!("https://127.0.0.1:{}", self.addr.port())
    }
    pub fn origin(&self) -> String {
        self.base()
    }
    pub fn api(&self) -> Api {
        Api {
            c: self.client.clone(),
            base: self.base(),
            cookie: None,
            csrf: None,
            bearer: None,
        }
    }
    pub async fn stop(self) {
        tokio::time::timeout(Duration::from_secs(10), self.srv.stop())
            .await
            .expect("shutdown within 10 s");
        let _ = std::fs::remove_dir_all(&self.dir);
    }
    /// Complete first-run setup and log in; returns an authenticated session API.
    pub async fn admin(&self) -> Api {
        let mut api = self.admin_unstepped().await;
        let (status, body) = api.step_up(PASSWORD, None, None).await;
        assert_eq!(status, 200, "test fixture step-up failed: {body}");
        api
    }
    /// Create the first admin session without the ten-minute step-up marker, for auth regressions.
    pub async fn admin_unstepped(&self) -> Api {
        let token = std::fs::read_to_string(self.dir.join("setup-token")).unwrap();
        let mut api = self.api();
        let (st, body) = api
            .send(
                api.bare(Method::POST, "/api/v1/auth/setup")
                    .json(&json!({"setup_token": token.trim(), "username": "admin", "password": PASSWORD})),
            )
            .await;
        assert_eq!(st, 201, "{body}");
        api.login("admin", PASSWORD).await;
        api
    }
    // Test helper: callers inspect the handshake error, so it is returned unboxed.
    #[allow(clippy::result_large_err)]
    pub async fn ws(&self, path: &str, headers: &[(&str, &str)]) -> Result<Ws, tokio_tungstenite::tungstenite::Error> {
        let tcp = TcpStream::connect(self.addr).await.unwrap();
        let tls = TlsConnector::from(self.tls.clone())
            .connect(ServerName::try_from("127.0.0.1").unwrap(), tcp)
            .await
            .unwrap();
        let mut req = format!("wss://127.0.0.1:{}{path}", self.addr.port()).into_client_request()?;
        for (k, v) in headers {
            req.headers_mut().insert(k.parse::<HeaderName>().unwrap(), v.parse().unwrap());
        }
        Ok(tokio_tungstenite::client_async(req, tls).await?.0)
    }
}

pub struct Api {
    pub c: Client,
    pub base: String,
    pub cookie: Option<String>,
    pub csrf: Option<String>,
    pub bearer: Option<String>,
}

impl Api {
    /// A request with no credentials or CSRF headers.
    pub fn bare(&self, method: Method, path: &str) -> RequestBuilder {
        self.c.request(method, format!("{}{path}", self.base))
    }
    /// A request with this API's credentials; cookie sessions also send Origin and X-CSRF-Token.
    pub fn rb(&self, method: Method, path: &str) -> RequestBuilder {
        let mut rb = self.bare(method, path);
        if let Some(t) = &self.bearer {
            rb = rb.bearer_auth(t);
        }
        if let Some(c) = &self.cookie {
            rb = rb.header("cookie", format!("__Host-bridge_session={c}")).header("origin", self.base.clone());
        }
        if let Some(c) = &self.csrf {
            rb = rb.header("x-csrf-token", c.clone());
        }
        rb
    }
    pub async fn send(&self, rb: RequestBuilder) -> (u16, Value) {
        let r = rb.send().await.unwrap();
        let st = r.status().as_u16();
        let text = r.text().await.unwrap();
        (st, serde_json::from_str(&text).unwrap_or(Value::String(text)))
    }
    pub async fn get(&self, path: &str) -> (u16, Value) {
        self.send(self.rb(Method::GET, path)).await
    }
    pub async fn post(&self, path: &str, body: Value) -> (u16, Value) {
        self.send(self.rb(Method::POST, path).json(&body)).await
    }
    pub async fn login(&mut self, user: &str, password: &str) -> u16 {
        let r = self
            .bare(Method::POST, "/api/v1/auth/login")
            .json(&json!({"username": user, "password": password}))
            .send()
            .await
            .unwrap();
        let st = r.status().as_u16();
        if st == 200 {
            let sc = r.headers().get("set-cookie").unwrap().to_str().unwrap().to_string();
            let value = sc.split(';').next().unwrap().strip_prefix("__Host-bridge_session=").unwrap().to_string();
            self.cookie = Some(value);
            let body: Value = r.json().await.unwrap();
            self.csrf = Some(body["csrf_token"].as_str().unwrap().to_string());
        }
        st
    }
    pub async fn step_up(&mut self, password: &str, totp_code: Option<&str>, recovery_code: Option<&str>) -> (u16, Value) {
        let mut body = json!({"password":password});
        if let Some(code) = totp_code {
            body["totp_code"] = json!(code);
        }
        if let Some(code) = recovery_code {
            body["recovery_code"] = json!(code);
        }
        let response = self.rb(Method::POST, "/api/v1/auth/step-up").json(&body).send().await.unwrap();
        let status = response.status().as_u16();
        let cookie = response.headers().get("set-cookie").and_then(|v| v.to_str().ok()).map(str::to_owned);
        let text = response.text().await.unwrap();
        let body = serde_json::from_str(&text).unwrap_or(Value::String(text));
        if status == 200 {
            if let Some(cookie) = cookie {
                self.cookie = cookie
                    .split(';')
                    .next()
                    .and_then(|part| part.strip_prefix("__Host-bridge_session="))
                    .map(str::to_owned);
            }
            self.csrf = body["csrf_token"].as_str().map(str::to_owned);
        }
        (status, body)
    }
    /// Add a worker through the API and return its token.
    pub async fn add_worker(&mut self, name: &str) -> String {
        let (step_up_status, step_up_body) = self.step_up(PASSWORD, None, None).await;
        assert_eq!(step_up_status, 200, "{step_up_body}");
        let (st, b) = self.post("/api/v1/workers", json!({"name": name})).await;
        assert_eq!(st, 201, "{b}");
        b["token"].as_str().unwrap().to_string()
    }
}

pub fn caps() -> Value {
    json!([
        {"operation": "mission.list", "summary": "list", "mutating": false, "needs_mission": false, "params": {}},
        {"operation": "mission.status", "summary": "status", "mutating": false, "needs_mission": true, "params": {}},
        {"operation": "task.show", "summary": "show", "mutating": false, "needs_mission": true, "params": {"task_number": {"type": "int", "required": false}}},
        {"operation": "task.list", "summary": "list tasks", "mutating": false, "needs_mission": true, "params": {"limit": {"type": "int"}, "offset": {"type": "int"}}},
        {"operation": "mission.events", "summary": "events", "mutating": false, "needs_mission": true, "params": {"after": {"type": "int"}, "limit": {"type": "int"}}},
        {"operation": "session.list", "summary": "operations", "mutating": false, "needs_mission": false, "params": {"mission": {"type": "str"}, "limit": {"type": "int"}, "offset": {"type": "int"}}},
        {"operation": "task.amend", "summary": "amend", "mutating": true, "needs_mission": true, "params": {"task_number": {"type": "int", "required": false}, "prompt": {"type": "str", "required": true}}},
        {"operation": "task.add", "summary": "add", "mutating": true, "needs_mission": true, "params": {"title": {"type": "str", "required": true}}},
        {"operation": "mission.delete", "summary": "delete", "mutating": true, "needs_mission": true, "params": {"confirm": {"type": "bool", "required": false}}}
    ])
}

pub fn request(id: &str, op: &str) -> Value {
    json!({"schema_version": 1, "operation": op, "params": {}, "mission_id": null, "target": "w1", "request_id": id})
}

pub struct Fw {
    pub ws: Ws,
    pub worker_id: String,
    pub generation: u64,
    pub seq: u64,
}

impl Fw {
    // Test helper: callers inspect the handshake error, so it is returned unboxed.
    #[allow(clippy::result_large_err)]
    pub async fn raw(env: &Env, token: &str) -> Result<Ws, tokio_tungstenite::tungstenite::Error> {
        env.ws("/api/v1/worker/connect", &[("authorization", &format!("Bearer {token}"))]).await
    }

    /// Connect, send hello and read the welcome.
    pub async fn connect(env: &Env, token: &str, worker_id: &str, cursor: u64) -> (Fw, Value) {
        let ws = Fw::raw(env, token).await.unwrap();
        let mut fw = Fw {
            ws,
            worker_id: worker_id.to_string(),
            generation: 0,
            seq: 0,
        };
        fw.send(
            "hello",
            json!({"worker_id": worker_id, "name": "w1", "host_name": "test-host", "software_version": "test", "protocol_versions": [1],
            "platform": {"system": "Linux"}, "capabilities": caps(),
            "missions": [{"id": "m1", "name": "mission", "status": "running", "folder": "proj", "updated_at": null}], "event_cursor": cursor}),
        )
        .await;
        let welcome = fw.recv().await;
        assert_eq!(welcome["type"], "welcome", "{welcome}");
        fw.generation = welcome["payload"]["generation"].as_u64().unwrap();
        (fw, welcome)
    }

    /// Like `connect`, but also waits until the connection is past the coordinator's resend pass.
    /// After the welcome, the coordinator redelivers every unfinished command. A command submitted
    /// over HTTP before that pass finishes would reach the worker twice: once through the live queue
    /// and once through the pass (documented at-least-once delivery). Use this when the test submits a
    /// command right after connecting and does not itself test redelivery; use `connect` for tests
    /// that expect the redelivered commands.
    pub async fn connect_settled(env: &Env, token: &str, worker_id: &str, cursor: u64) -> (Fw, Value) {
        let (mut fw, welcome) = Fw::connect(env, token, worker_id, cursor).await;
        fw.barrier().await;
        (fw, welcome)
    }

    pub async fn send_raw(&mut self, text: String) {
        self.ws.send(WsMsg::Text(text.into())).await.unwrap();
    }

    pub async fn send(&mut self, kind: &str, payload: Value) {
        self.seq += 1;
        let m = json!({"v": 1, "type": kind, "id": format!("m{}", self.seq), "worker_id": self.worker_id, "generation": self.generation, "payload": payload});
        self.send_raw(m.to_string()).await;
    }

    pub async fn recv(&mut self) -> Value {
        loop {
            let m = tokio::time::timeout(Duration::from_secs(10), self.ws.next())
                .await
                .expect("timed out waiting for a message");
            match m {
                Some(Ok(WsMsg::Text(t))) => return serde_json::from_str(t.as_str()).unwrap(),
                Some(Ok(WsMsg::Ping(_) | WsMsg::Pong(_))) => {}
                other => panic!("expected a text message, got {other:?}"),
            }
        }
    }

    /// Ping and wait for the pong: every earlier message has been processed once this returns.
    pub async fn barrier(&mut self) {
        self.send("ping", json!({})).await;
        loop {
            let m = self.recv().await;
            if m["type"] == "pong" {
                return;
            }
        }
    }

    pub async fn expect_close(&mut self) -> Option<u16> {
        expect_close(&mut self.ws).await
    }
}

/// Wait for the peer to close; returns the close code when a close frame arrived.
pub async fn expect_close(ws: &mut Ws) -> Option<u16> {
    loop {
        match tokio::time::timeout(Duration::from_secs(10), ws.next())
            .await
            .expect("timed out waiting for close")
        {
            Some(Ok(WsMsg::Close(f))) => return f.map(|f| u16::from(f.code)),
            None | Some(Err(_)) => return None,
            Some(Ok(_)) => {}
        }
    }
}

/// Register a live connection for `name` that nobody reads, so the coordinator treats the worker as
/// online (it refuses read-only commands for an offline worker). Keep the returned receiver alive.
pub fn mark_online(env: &Env, name: &str, worker_id: &str) -> tokio::sync::mpsc::Receiver<bridge_coordinator::hub::Outbound> {
    let (tx, rx) = tokio::sync::mpsc::channel(bridge_coordinator::hub::OUTBOUND_QUEUE);
    let (close, _) = tokio::sync::watch::channel(None);
    env.srv.state.hub.register(
        name,
        bridge_coordinator::hub::ConnHandle {
            conn_id: env.srv.state.hub.next_conn_id(),
            generation: 1,
            worker_id: worker_id.to_string(),
            tx,
            close,
        },
    );
    rx
}
