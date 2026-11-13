//! Opt-in update-availability check (PRD-011, D57). Off unless `update_check = true`.
//! One GET to the public GitHub releases API of this project, at most once per 24 hours; the result is
//! cached in `<data dir>/update-check.json` (mode 0600). Nothing identifying is sent.
use crate::config::Config;
use serde::{Deserialize, Serialize};
use std::future::Future;
use std::path::{Path, PathBuf};

pub const RELEASES_URL: &str = "https://api.github.com/repos/chrisgleissner/llm-tools/releases/latest";
pub const USER_AGENT: &str = "bridge-coordinator";
pub const INTERVAL_SECS: i64 = 24 * 3600;
pub const TIMEOUT_SECS: u64 = 5;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Cache {
    pub checked_at: i64,
    /// Version of the newest release without a leading `v`; `None` when the last attempt failed.
    pub latest: Option<String>,
    /// Short reason when the last attempt failed (no response body, no URL, no addresses).
    pub error: Option<String>,
}

pub fn cache_path(data_dir: &Path) -> PathBuf {
    data_dir.join("update-check.json")
}

pub fn read_cache(data_dir: &Path) -> Option<Cache> {
    serde_json::from_slice(&std::fs::read(cache_path(data_dir)).ok()?).ok()
}

/// `tag_name` of the GitHub release JSON without a leading `v`/`V`; None when absent, empty, or not a string.
pub fn parse_latest(body: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(body).ok()?;
    let tag = value.get("tag_name")?.as_str()?.trim();
    let version = tag.strip_prefix(['v', 'V']).unwrap_or(tag);
    (!version.is_empty()).then(|| version.to_string())
}

/// True when `latest` is a strictly higher dotted-numeric version than `current`. Compare the numeric
/// components in order (missing component = 0). A component that is not a plain number, or a version with a `-`
/// pre-release suffix on `latest`, returns false.
pub fn is_newer(current: &str, latest: &str) -> bool {
    fn components(version: &str) -> Option<Vec<u64>> {
        if version.is_empty() {
            return None;
        }
        version
            .split('.')
            .map(|part| {
                if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
                    None
                } else {
                    part.parse().ok()
                }
            })
            .collect()
    }
    if latest.contains('-') {
        return false;
    }
    let (Some(current), Some(latest)) = (components(current), components(latest)) else {
        return false;
    };
    for i in 0..current.len().max(latest.len()) {
        let a = current.get(i).copied().unwrap_or(0);
        let b = latest.get(i).copied().unwrap_or(0);
        if b != a {
            return b > a;
        }
    }
    false
}

/// What diagnostics shows: "update available: X" when `is_newer(current, latest)`, "up to date" when
/// latest is known and not newer, "check failed: <error>" when the cache has an error and no latest,
/// "not checked yet" when cache is None.
pub fn describe(current: &str, cache: Option<&Cache>) -> String {
    match cache {
        Some(cache) => match &cache.latest {
            Some(latest) if is_newer(current, latest) => format!("update available: {latest}"),
            Some(_) => "up to date".into(),
            None => cache.error.as_ref().map_or_else(|| "not checked yet".into(), |e| format!("check failed: {e}")),
        },
        None => "not checked yet".into(),
    }
}

/// The real HTTP fetch. Sends only the GitHub JSON Accept header and a generic User-Agent.
pub async fn fetch_release(url: String) -> Result<String, String> {
    use std::time::Duration;
    let client = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(TIMEOUT_SECS))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "network error".to_string())?;
    let response = client
        .get(url)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .send()
        .await
        .map_err(|_| "network error".to_string())?;
    let status = response.status();
    if status.as_u16() != 200 {
        return Err(format!("HTTP {}", status.as_u16()));
    }
    response.text().await.map_err(|_| "network error".to_string())
}

/// Refresh the cache when enabled and due. Failed checks are retried on the next call.
pub async fn refresh<F, Fut>(cfg: &Config, data_dir: &Path, now: i64, fetch: F) -> Option<Cache>
where
    F: FnOnce(String) -> Fut,
    Fut: Future<Output = Result<String, String>>,
{
    if !cfg.update_check {
        return None;
    }
    if let Some(cache) = read_cache(data_dir) {
        if cache.latest.is_some() && now - cache.checked_at < INTERVAL_SECS {
            return Some(cache);
        }
    }
    let (latest, error) = match fetch(RELEASES_URL.to_string()).await {
        Ok(body) => match parse_latest(&body) {
            Some(latest) => (Some(latest), None),
            None => (None, Some("unexpected response".to_string())),
        },
        Err(error) => (None, Some(error)),
    };
    let cache = Cache {
        checked_at: now,
        latest,
        error,
    };
    if let Ok(json) = serde_json::to_vec(&cache) {
        let _ = crate::write_replace(&cache_path(data_dir), &json, 0o600);
    }
    Some(cache)
}

/// Background task for the serving process.
pub async fn run(cfg: Config, data_dir: PathBuf) {
    if !cfg.update_check {
        return;
    }
    loop {
        let _ = refresh(&cfg, &data_dir, crate::now(), fetch_release).await;
        tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };

    fn temp_dir(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("wp9a-uc-{}-{name}", std::process::id()))
    }

    #[test]
    fn parse_latest_strips_v_prefix() {
        assert_eq!(parse_latest(r#"{"tag_name":"v1.5.0"}"#), Some("1.5.0".into()));
        assert_eq!(parse_latest("{}"), None);
        assert_eq!(parse_latest("not json"), None);
    }

    #[test]
    fn is_newer_compares_numeric_components() {
        assert!(is_newer("1.4.9", "1.5.0"));
        assert!(!is_newer("1.5.0", "1.4.9"));
        assert!(!is_newer("1.5", "1.5.0"));
        assert!(is_newer("1.5", "1.5.1"));
        assert!(is_newer("1.9.0", "1.10.0"));
        assert!(!is_newer("1.5.0", "1.5.0"));
        assert!(!is_newer("1.5.0", "1.6.0-rc1"));
        assert!(!is_newer("x", "1.0"));
    }

    #[tokio::test]
    async fn default_config_makes_no_request() {
        let dir = temp_dir("disabled");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let called = Arc::new(AtomicBool::new(false));
        let seen = called.clone();
        assert_eq!(
            refresh(&Config::default(), &dir, 10, move |_| async move {
                seen.store(true, Ordering::SeqCst);
                panic!("network used")
            })
            .await,
            None
        );
        assert!(!called.load(Ordering::SeqCst));
        assert!(!cache_path(&dir).exists());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[tokio::test]
    async fn enabled_check_fetches_once_and_caches() {
        let dir = temp_dir("enabled");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = Config {
            update_check: true,
            ..Config::default()
        };
        let first = refresh(&cfg, &dir, 100, |_| async { Ok(r#"{"tag_name":"v9.9.9"}"#.into()) }).await.unwrap();
        assert_eq!(first.latest.as_deref(), Some("9.9.9"));
        assert!(cache_path(&dir).exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(cache_path(&dir)).unwrap().permissions().mode() & 0o777, 0o600);
        }
        assert_eq!(refresh(&cfg, &dir, 160, |_| async { panic!("unexpected fetch") }).await, Some(first.clone()));
        let third = refresh(&cfg, &dir, 100 + INTERVAL_SECS + 1, |_| async { Ok(r#"{"tag_name":"v10.0.0"}"#.into()) })
            .await
            .unwrap();
        assert_eq!(third.latest.as_deref(), Some("10.0.0"));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[tokio::test]
    async fn failed_fetch_is_cached_with_a_short_error() {
        let dir = temp_dir("failed");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = Config {
            update_check: true,
            ..Config::default()
        };
        let cache = refresh(&cfg, &dir, 100, |_| async { Err("HTTP 403".into()) }).await.unwrap();
        assert_eq!(cache.latest, None);
        assert_eq!(cache.error.as_deref(), Some("HTTP 403"));
        let again = refresh(&cfg, &dir, 160, |_| async { Err("HTTP 403".into()) }).await.unwrap();
        assert_eq!(again.checked_at, 160);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn describe_texts() {
        let good = Cache {
            checked_at: 0,
            latest: Some("1.6.0".into()),
            error: None,
        };
        let current = Cache {
            checked_at: 0,
            latest: Some("1.5.0".into()),
            error: None,
        };
        let failed = Cache {
            checked_at: 0,
            latest: None,
            error: Some("HTTP 403".into()),
        };
        assert_eq!(describe("1.5.0", Some(&good)), "update available: 1.6.0");
        assert_eq!(describe("1.5.0", Some(&current)), "up to date");
        assert_eq!(describe("1.5.0", Some(&failed)), "check failed: HTTP 403");
        assert_eq!(describe("1.5.0", None), "not checked yet");
    }

    #[test]
    fn request_carries_no_identity() {
        assert_eq!(USER_AGENT, "bridge-coordinator");
        assert!(RELEASES_URL.starts_with("https://api.github.com/repos/"));
        assert!(!RELEASES_URL.contains('?'));
    }
}
