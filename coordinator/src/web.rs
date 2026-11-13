//! Static serving of the web interface (WEB-009, WEB-012).
//!
//! The files are public: the single-page application contains no data, and every
//! API call it makes is authenticated separately. Paths are decoded and checked
//! segment by segment, then canonicalised and required to stay inside the web root.

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use std::path::{Component, Path, PathBuf};

/// Where the web files live when neither `--web-dir` nor `web_dir` is set.
pub const SYSTEM_WEB_DIR: &str = "/usr/share/bridge-coordinator/web";

/// Pick the web root: explicit setting, else `web` next to the executable, else the system path.
/// Returns `None` when no candidate is a directory containing `index.html`.
pub fn resolve_web_dir(explicit: Option<&Path>) -> Result<Option<PathBuf>, String> {
    if let Some(p) = explicit {
        if !p.join("index.html").is_file() {
            return Err(format!("web directory {} has no index.html", p.display()));
        }
        return Ok(Some(p.to_path_buf()));
    }
    let mut candidates = Vec::new();
    if let Some(dir) = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)) {
        candidates.push(dir.join("web"));
    }
    candidates.push(PathBuf::from(SYSTEM_WEB_DIR));
    Ok(candidates.into_iter().find(|c| c.join("index.html").is_file()))
}

fn percent_decode(s: &str) -> Option<String> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let hex = s.get(i + 1..i + 3)?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// Map a request path to a relative file path, or `None` for anything that is not a plain,
/// forward-only path (`..`, `.`, empty segments, backslashes, NUL, hidden files).
pub fn safe_relative(path: &str) -> Option<PathBuf> {
    let decoded = percent_decode(path)?;
    if decoded.contains(['\0', '\\']) {
        return None;
    }
    let mut rel = PathBuf::new();
    for seg in decoded.strip_prefix('/')?.split('/') {
        if seg.is_empty() || seg == "." || seg == ".." || seg.starts_with('.') {
            return None;
        }
        rel.push(seg);
    }
    if rel.components().any(|c| !matches!(c, Component::Normal(_))) {
        return None;
    }
    Some(rel)
}

/// esbuild writes `name-HASH.ext` with an 8 character upper-case alphanumeric hash.
fn hashed_name(rel: &Path) -> bool {
    rel.file_stem()
        .and_then(|s| s.to_str())
        .and_then(|s| s.rsplit_once('-'))
        .is_some_and(|(_, h)| h.len() == 8 && h.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()))
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "webmanifest" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "txt" => "text/plain; charset=utf-8",
        "map" => "application/json",
        "woff2" => "font/woff2",
        _ => "application/octet-stream",
    }
}

fn csp(headers: &HeaderMap) -> String {
    let host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .filter(|h| !h.is_empty() && h.chars().all(|c| c.is_ascii_alphanumeric() || ".-:[]".contains(c)));
    let ws = host.map(|h| format!(" wss://{h}")).unwrap_or_default();
    format!(
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; \
         connect-src 'self'{ws}; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
}

fn with_headers(mut r: Response, ctype: &'static str, cache: &'static str, headers: &HeaderMap) -> Response {
    let h = r.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(ctype));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    if let Ok(v) = HeaderValue::from_str(&csp(headers)) {
        h.insert(header::CONTENT_SECURITY_POLICY, v);
    }
    r
}

/// Whether the client accepts gzip (`Accept-Encoding: gzip`, not `gzip;q=0`).
fn accepts_gzip(headers: &HeaderMap) -> bool {
    headers.get(header::ACCEPT_ENCODING).and_then(|v| v.to_str().ok()).is_some_and(|v| {
        v.split(',').any(|item| {
            let mut parts = item.split(';');
            let coding = parts.next().unwrap_or("").trim();
            let refused = parts.any(|p| p.trim().strip_prefix("q=").and_then(|q| q.trim().parse::<f32>().ok()).is_some_and(|q| q <= 0.0));
            coding.eq_ignore_ascii_case("gzip") && !refused
        })
    })
}

/// Text assets that the build stores next to a gzip copy (`app-HASH.js.gz`).
fn compressible(rel: &Path) -> bool {
    matches!(
        rel.extension().and_then(|e| e.to_str()),
        Some("html" | "js" | "mjs" | "css" | "svg" | "json" | "webmanifest")
    )
}

/// The file at `rel` for a client that accepts gzip: the build's `.gz` copy when it exists (smaller by about 75% on a slow link), else the file itself.
/// The copy goes through the same root check as any file. The `bool` says that the bytes are gzip.
async fn read_asset(root: &Path, rel: &Path, gzip: bool) -> Option<(Vec<u8>, PathBuf, bool)> {
    if gzip && compressible(rel) {
        let mut name = rel.as_os_str().to_os_string();
        name.push(".gz");
        if let Some((bytes, _)) = read_file(root, Path::new(&name)).await {
            return Some((bytes, rel.to_path_buf(), true));
        }
    }
    let (bytes, full) = read_file(root, rel).await?;
    Some((bytes, full, false))
}

async fn read_file(root: &Path, rel: &Path) -> Option<(Vec<u8>, PathBuf)> {
    let (root, rel) = (root.to_path_buf(), rel.to_path_buf());
    tokio::task::spawn_blocking(move || {
        let root = std::fs::canonicalize(root).ok()?;
        let full = std::fs::canonicalize(root.join(rel)).ok()?;
        if !full.starts_with(&root) || !std::fs::metadata(&full).ok()?.is_file() {
            return None;
        }
        Some((std::fs::read(&full).ok()?, full))
    })
    .await
    .ok()?
}

/// Router fallback: static files for GET/HEAD outside the API, `index.html` for unknown paths.
pub async fn serve(root: Option<PathBuf>, method: Method, uri: Uri, headers: HeaderMap) -> Response {
    let path = uri.path();
    if path == "/api" || path.starts_with("/api/") || !matches!(method, Method::GET | Method::HEAD) {
        return crate::http::err(StatusCode::NOT_FOUND, "not-found", "no such route").into_response();
    }
    let Some(root) = root else {
        return with_headers((StatusCode::OK, PLACEHOLDER).into_response(), "text/html; charset=utf-8", "no-store", &headers);
    };
    let looks_like_file = path.rsplit('/').next().is_some_and(|s| s.contains('.'));
    let gzip = accepts_gzip(&headers);
    let (bytes, full, zipped, immutable) = match safe_relative(path) {
        Some(rel) if path != "/" => match read_asset(&root, &rel, gzip).await {
            Some((b, f, z)) => (b, f, z, hashed_name(&rel)),
            None if looks_like_file => return (StatusCode::NOT_FOUND, "not found").into_response(),
            None => match read_asset(&root, Path::new("index.html"), gzip).await {
                Some((b, f, z)) => (b, f, z, false),
                None => return (StatusCode::NOT_FOUND, "not found").into_response(),
            },
        },
        // "/" and malformed paths both get the application shell; the shell does its own routing.
        _ if path == "/" || !looks_like_file => match read_asset(&root, Path::new("index.html"), gzip).await {
            Some((b, f, z)) => (b, f, z, false),
            None => return (StatusCode::NOT_FOUND, "not found").into_response(),
        },
        _ => return (StatusCode::NOT_FOUND, "not found").into_response(),
    };
    let ctype = content_type(&full);
    let cache = if immutable { "public, max-age=31536000, immutable" } else { "no-cache" };
    let body = if method == Method::HEAD { Body::empty() } else { Body::from(bytes) };
    let mut response = with_headers(Response::new(body), ctype, cache, &headers);
    if compressible(&full) {
        response.headers_mut().insert(header::VARY, HeaderValue::from_static("accept-encoding"));
    }
    if zipped {
        response.headers_mut().insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
    }
    response
}

const PLACEHOLDER: &str = "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Bridge coordinator</title></head><body><h1>Bridge coordinator</h1><p>The web interface is not installed. The API is under <code>/api/v1</code>.</p></body></html>";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_traversal_and_odd_paths() {
        for bad in [
            "/../etc/passwd",
            "/a/../../etc/passwd",
            "/%2e%2e/etc/passwd",
            "/%2E%2E%2fetc/passwd",
            "/a%5c..%5cb",
            "/a\\b",
            "/a%00b",
            "//etc/passwd",
            "/.env",
            "/a/./b",
            "/%zz",
        ] {
            assert!(safe_relative(bad).is_none(), "{bad} must be rejected");
        }
    }

    #[test]
    fn gzip_is_used_only_when_the_client_accepts_it() {
        let with = |v: &'static str| {
            let mut h = HeaderMap::new();
            h.insert(header::ACCEPT_ENCODING, HeaderValue::from_static(v));
            h
        };
        assert!(accepts_gzip(&with("gzip")));
        assert!(accepts_gzip(&with("br, gzip;q=0.5, deflate")));
        assert!(accepts_gzip(&with("GZIP")));
        assert!(!accepts_gzip(&with("br, deflate")));
        assert!(!accepts_gzip(&with("gzip;q=0")));
        assert!(!accepts_gzip(&HeaderMap::new()));
    }

    #[test]
    fn accepts_plain_paths() {
        assert_eq!(safe_relative("/app.js").unwrap(), PathBuf::from("app.js"));
        assert_eq!(safe_relative("/assets/a%20b.css").unwrap(), PathBuf::from("assets/a b.css"));
    }
}
