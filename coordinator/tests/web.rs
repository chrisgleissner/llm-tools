mod common;
use common::*;

fn web_fixture() -> std::path::PathBuf {
    let d = tmp_dir().with_extension("web");
    std::fs::create_dir_all(d.join("assets")).unwrap();
    std::fs::write(
        d.join("index.html"),
        "<!doctype html><title>spa</title><script src=\"/app-ABCD1234.js\"></script>",
    )
    .unwrap();
    std::fs::write(d.join("app-ABCD1234.js"), "console.log(1)").unwrap();
    std::fs::write(d.join("assets/app.css"), "body{}").unwrap();
    // A secret outside the web root, next to it.
    std::fs::write(d.parent().unwrap().join("outside-secret.txt"), "TOP-SECRET").unwrap();
    d
}

async fn boot_web() -> (Env, std::path::PathBuf) {
    let web = web_fixture();
    let line = format!("\nweb_dir = {:?}\n", web.to_str().unwrap());
    (boot_with(move |t| t + &line).await, web)
}

async fn get(env: &Env, path: &str) -> (u16, reqwest::header::HeaderMap, String) {
    let r = env.client.get(format!("{}{path}", env.base())).send().await.unwrap();
    (r.status().as_u16(), r.headers().clone(), r.text().await.unwrap())
}

#[tokio::test]
async fn serves_index_and_assets_publicly_with_security_headers() {
    let (env, web) = boot_web().await;
    let (st, h, body) = get(&env, "/").await;
    assert_eq!(st, 200);
    assert!(body.contains("<title>spa</title>"));
    assert_eq!(h["content-type"], "text/html; charset=utf-8");
    let csp = h["content-security-policy"].to_str().unwrap();
    assert!(csp.contains("default-src 'self'") && csp.contains("script-src 'self'") && csp.contains("style-src 'self' 'unsafe-inline';"));
    // xterm.js injects <style> elements, so only style-src may allow inline styles; scripts never.
    assert!(!csp.replace("style-src 'self' 'unsafe-inline';", "").contains("unsafe-inline"));
    assert!(csp.contains(&format!("wss://127.0.0.1:{}", env.addr.port())));
    assert_eq!(h["x-content-type-options"], "nosniff");
    assert_eq!(h["referrer-policy"], "no-referrer");
    assert_eq!(h["x-frame-options"], "DENY");
    assert!(h["strict-transport-security"].to_str().unwrap().starts_with("max-age="));
    assert_eq!(h["cache-control"], "no-cache");

    let (st, h, body) = get(&env, "/app-ABCD1234.js").await;
    assert_eq!((st, body.as_str()), (200, "console.log(1)"));
    assert_eq!(h["content-type"], "text/javascript; charset=utf-8");
    assert!(h["cache-control"].to_str().unwrap().contains("immutable"));
    let (st, h, _) = get(&env, "/assets/app.css").await;
    assert_eq!(st, 200);
    assert_eq!(h["content-type"], "text/css; charset=utf-8");
    env.stop().await;
    let _ = std::fs::remove_dir_all(web);
}

#[tokio::test]
async fn unknown_paths_get_the_app_shell_but_api_paths_do_not() {
    let (env, web) = boot_web().await;
    let (st, _, body) = get(&env, "/missions/abc").await;
    assert_eq!(st, 200);
    assert!(body.contains("<title>spa</title>"));
    let (st, _, _) = get(&env, "/missing.js").await;
    assert_eq!(st, 404);
    let (st, h, body) = get(&env, "/api/v1/nothing").await;
    assert_eq!(st, 404);
    assert!(body.contains("not-found"));
    assert_eq!(h["content-type"], "application/json");
    let (st, _, body) = get(&env, "/healthz").await;
    assert_eq!((st, body.as_str()), (200, "{\"status\":\"ok\"}"));
    env.stop().await;
    let _ = std::fs::remove_dir_all(web);
}

#[tokio::test]
async fn path_traversal_never_leaves_the_web_root() {
    let (env, web) = boot_web().await;
    let secret = web.file_name().unwrap().to_str().unwrap().to_string();
    let _ = secret;
    // Raw TCP over TLS so the client cannot normalise the path before sending.
    for path in [
        "/../outside-secret.txt",
        "/%2e%2e/outside-secret.txt",
        "/%2E%2E%2Foutside-secret.txt",
        "/assets/../../outside-secret.txt",
        "/..%5coutside-secret.txt",
        "/assets/%2e%2e/%2e%2e/outside-secret.txt",
        "//outside-secret.txt",
        "/%00",
    ] {
        let (st, body) = raw_get(&env, path).await;
        assert!(!body.contains("TOP-SECRET"), "{path} leaked the file");
        assert!(st == 404 || st == 400 || (st == 200 && body.contains("<title>spa</title>")), "{path}: {st}");
    }
    env.stop().await;
    let _ = std::fs::remove_file(web.parent().unwrap().join("outside-secret.txt"));
    let _ = std::fs::remove_dir_all(web);
}

#[cfg(unix)]
#[tokio::test]
async fn symlink_escaping_the_root_is_refused() {
    let (env, web) = boot_web().await;
    let outside = web.parent().unwrap().join("outside-secret.txt");
    std::os::unix::fs::symlink(&outside, web.join("link.txt")).unwrap();
    let (st, _, body) = get(&env, "/link.txt").await;
    assert_eq!(st, 404);
    assert!(!body.contains("TOP-SECRET"));
    env.stop().await;
    let _ = std::fs::remove_file(outside);
    let _ = std::fs::remove_dir_all(web);
}

#[tokio::test]
async fn post_to_static_paths_is_not_served() {
    let (env, web) = boot_web().await;
    let r = env.client.post(format!("{}/", env.base())).send().await.unwrap();
    assert_eq!(r.status().as_u16(), 404);
    env.stop().await;
    let _ = std::fs::remove_dir_all(web);
}

#[tokio::test]
async fn missing_web_dir_setting_is_a_startup_error() {
    let dir = tmp_dir();
    bridge_coordinator::init::init(&dir, &sans()).unwrap();
    edit_config(&dir, |t| t.replace("8443", "0") + "\nweb_dir = \"/nonexistent/web\"\n");
    let e = bridge_coordinator::server::start(&dir).await.err().expect("must refuse");
    assert!(e.contains("index.html"), "{e}");
    let _ = std::fs::remove_dir_all(dir);
}

async fn raw_get(env: &Env, path: &str) -> (u16, String) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let tcp = tokio::net::TcpStream::connect(env.addr).await.unwrap();
    let mut tls = tokio_rustls::TlsConnector::from(env.tls.clone())
        .connect(rustls::pki_types::ServerName::try_from("127.0.0.1").unwrap(), tcp)
        .await
        .unwrap();
    tls.write_all(format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").as_bytes())
        .await
        .unwrap();
    let mut buf = Vec::new();
    let _ = tls.read_to_end(&mut buf).await;
    let text = String::from_utf8_lossy(&buf).to_string();
    let status = text.split_whitespace().nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    (status, text)
}
