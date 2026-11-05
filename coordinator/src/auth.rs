//! Password hashing, opaque tokens and the login rate limiter.

use argon2::password_hash::{rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use base64::Engine;
use rand::RngCore;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const MIN_PASSWORD_LEN: usize = 12;
pub const MAX_PASSWORD_LEN: usize = 256;

fn argon() -> Argon2<'static> {
    // 19 MiB, t=2, p=1 (coordinator.md "Storage").
    Argon2::new(
        Algorithm::Argon2id,
        Version::V0x13,
        Params::new(19 * 1024, 2, 1, None).expect("valid argon2 parameters"),
    )
}

pub fn hash_password(password: &str) -> String {
    let salt = SaltString::generate(&mut OsRng);
    argon().hash_password(password.as_bytes(), &salt).expect("argon2 hashing").to_string()
}

pub fn verify_password(password: &str, phc: &str) -> bool {
    PasswordHash::new(phc).is_ok_and(|h| argon().verify_password(password.as_bytes(), &h).is_ok())
}

/// 32 random bytes, URL-safe base64 without padding (43 characters).
pub fn random_token() -> String {
    let mut b = [0u8; 32];
    OsRng.fill_bytes(&mut b);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b)
}

pub fn sha256_hex(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}

/// Constant-time equality: compares fixed-length digests without early exit.
pub fn ct_eq(a: &str, b: &str) -> bool {
    let (x, y) = (Sha256::digest(a.as_bytes()), Sha256::digest(b.as_bytes()));
    x.iter().zip(y.iter()).fold(0u8, |acc, (p, q)| acc | (p ^ q)) == 0
}

pub fn valid_username(u: &str) -> bool {
    (1..=64).contains(&u.len()) && u.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'@'))
}

pub fn valid_password(p: &str) -> Result<(), String> {
    if p.len() < MIN_PASSWORD_LEN {
        return Err(format!("password must be at least {MIN_PASSWORD_LEN} characters"));
    }
    if p.len() > MAX_PASSWORD_LEN {
        return Err(format!("password must be at most {MAX_PASSWORD_LEN} bytes"));
    }
    Ok(())
}

/// Sliding-window failure limiter: at most `limit` failures per `window` per key.
pub struct RateLimiter {
    limit: usize,
    window: Duration,
    max_keys: usize,
    failures: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl RateLimiter {
    pub fn new(limit: usize, window: Duration, max_keys: usize) -> RateLimiter {
        RateLimiter {
            limit,
            window,
            max_keys,
            failures: Mutex::new(HashMap::new()),
        }
    }

    fn prune(map: &mut HashMap<String, VecDeque<Instant>>, now: Instant, window: Duration) {
        map.retain(|_, q| {
            while q.front().is_some_and(|t| now.duration_since(*t) >= window) {
                q.pop_front();
            }
            !q.is_empty()
        });
    }

    /// Seconds to wait when `key` is limited, or `None` when it may proceed.
    pub fn blocked(&self, key: &str) -> Option<u64> {
        let now = Instant::now();
        let mut m = self.failures.lock().unwrap_or_else(|e| e.into_inner());
        let q = m.get_mut(key)?;
        while q.front().is_some_and(|t| now.duration_since(*t) >= self.window) {
            q.pop_front();
        }
        if q.len() >= self.limit {
            let oldest = *q.front()?;
            Some((self.window.saturating_sub(now.duration_since(oldest))).as_secs() + 1)
        } else {
            None
        }
    }

    pub fn fail(&self, key: &str) {
        let now = Instant::now();
        let mut m = self.failures.lock().unwrap_or_else(|e| e.into_inner());
        if !m.contains_key(key) && m.len() >= self.max_keys {
            Self::prune(&mut m, now, self.window);
            if m.len() >= self.max_keys {
                return; // table full of live entries: new keys are covered by the per-address key
            }
        }
        let q = m.entry(key.to_string()).or_default();
        q.push_back(now);
        while q.len() > self.limit {
            q.pop_front();
        }
    }
}
