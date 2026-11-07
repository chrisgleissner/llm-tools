//! Password hashing, opaque tokens and the login rate limiter.

use argon2::password_hash::{PasswordHasher, PasswordVerifier};
use argon2::{Algorithm, Argon2, Params, Version};
use base64::Engine;
use rand::rngs::SysRng;
use rand::TryRng;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const MIN_PASSWORD_LEN: usize = 15;
pub const MAX_PASSWORD_LEN: usize = 256;
pub const GLOBAL_MUTATION_BUDGET_KEY: &str = "global:mutations";
pub const GLOBAL_MUTATION_LIMIT_PER_MINUTE: usize = 600;

fn argon() -> Argon2<'static> {
    // 19 MiB, t=2, p=1 (coordinator.md "Storage").
    Argon2::new(
        Algorithm::Argon2id,
        Version::V0x13,
        Params::new(19 * 1024, 2, 1, None).expect("valid argon2 parameters"),
    )
}

pub fn hash_password(password: &str) -> String {
    argon().hash_password(password.as_bytes()).expect("argon2 hashing").to_string()
}

pub fn verify_password(password: &str, phc: &str) -> bool {
    argon().verify_password(password.as_bytes(), phc).is_ok()
}

/// 32 random bytes, URL-safe base64 without padding (43 characters).
pub fn random_token() -> String {
    let mut b = [0u8; 32];
    SysRng.try_fill_bytes(&mut b).expect("operating system random source");
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

/// Rate-limit key for a client address. IPv6 clients are keyed by their /64 (a single subscriber
/// normally controls a whole /64, so per-address keys would be free to rotate); IPv4-mapped IPv6
/// addresses are keyed as the IPv4 address.
pub fn addr_key(ip: &std::net::IpAddr) -> String {
    match ip.to_canonical() {
        std::net::IpAddr::V4(a) => a.to_string(),
        std::net::IpAddr::V6(a) => {
            let s = a.segments();
            format!("{:x}:{:x}:{:x}:{:x}::/64", s[0], s[1], s[2], s[3])
        }
    }
}

#[derive(Default)]
struct Entry {
    failures: VecDeque<Instant>,
    in_flight: usize,
    strikes: u32,
    strike_expires: Option<Instant>,
    blocked_until: Option<Instant>,
}

/// Sliding-window limiter: at most `limit` failures per `window` per key. An attempt reserves a
/// slot under the lock before the expensive check starts, and attempts still in progress count
/// against the limit, so concurrent requests cannot all pass the check before any failure is
/// recorded. When the key table is full of live keys, new keys are refused (fail closed).
pub struct RateLimiter {
    limit: usize,
    window: Duration,
    max_keys: usize,
    /// Extra table entries only usable by privileged callers (addresses with a recent successful login).
    reserve_keys: usize,
    entries: Mutex<HashMap<String, Entry>>,
}

/// Fixed-window request budget for authenticated mutations. Unlike the login limiter, admitted
/// requests consume the budget regardless of their outcome. Keys are namespaced by the caller.
pub struct MutationLimiter {
    window: Duration,
    max_keys: usize,
    entries: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl MutationLimiter {
    pub fn new(window: Duration, max_keys: usize) -> Self {
        Self {
            window,
            max_keys,
            entries: Mutex::new(HashMap::new()),
        }
    }

    /// Reserve each supplied key atomically, returning the longest retry delay if any budget is
    /// exhausted. The caller should use separate namespaced keys for token and address budgets.
    pub fn check(&self, keys: &[(String, usize)]) -> Option<u64> {
        let now = Instant::now();
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        entries.retain(|_, hits| {
            while hits.front().is_some_and(|at| now.duration_since(*at) >= self.window) {
                hits.pop_front();
            }
            !hits.is_empty()
        });
        let missing = keys.iter().filter(|(key, _)| !entries.contains_key(key)).count();
        if entries.len().saturating_add(missing) > self.max_keys {
            return Some(self.window.as_secs().max(1));
        }
        let mut retry_after = 0;
        for (key, limit) in keys {
            let Some(hits) = entries.get(key) else { continue };
            if hits.len() >= *limit {
                if let Some(oldest) = hits.front() {
                    let wait = self.window.saturating_sub(now.duration_since(*oldest));
                    let seconds = wait.as_secs() + u64::from(wait.subsec_nanos() > 0);
                    retry_after = retry_after.max(seconds.max(1));
                }
            }
        }
        if retry_after > 0 {
            return Some(retry_after);
        }
        for (key, _) in keys {
            entries.entry(key.clone()).or_default().push_back(now);
        }
        None
    }
}

/// An admitted attempt. Dropping it without calling [`Attempt::succeed`] or [`Attempt::release`]
/// records a failure, so an abandoned request (client gone mid-verification) still counts.
pub struct Attempt<'a> {
    limiter: &'a RateLimiter,
    keys: Vec<String>,
}

impl RateLimiter {
    pub fn new(limit: usize, window: Duration, max_keys: usize) -> RateLimiter {
        RateLimiter {
            limit,
            window,
            max_keys,
            reserve_keys: 256,
            entries: Mutex::new(HashMap::new()),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, Entry>> {
        self.entries.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn expire(e: &mut Entry, now: Instant, window: Duration) {
        while e.failures.front().is_some_and(|t| now.duration_since(*t) >= window) {
            e.failures.pop_front();
        }
    }

    fn prune(map: &mut HashMap<String, Entry>, now: Instant, window: Duration) {
        map.retain(|_, e| {
            Self::expire(e, now, window);
            if e.strike_expires.is_some_and(|expires| expires <= now) {
                e.strikes = 0;
                e.strike_expires = None;
                e.blocked_until = None;
            }
            !e.failures.is_empty() || e.in_flight > 0 || e.blocked_until.is_some_and(|until| until > now) || e.strikes > 0
        });
    }

    fn retry_seconds(duration: Duration) -> u64 {
        duration.as_secs() + u64::from(duration.subsec_nanos() > 0)
    }

    /// Reserve one attempt on every key, or return the seconds to wait. `privileged` callers may
    /// use the table reserve when it is otherwise full.
    pub fn acquire(&self, keys: &[String], privileged: bool) -> Result<Attempt<'_>, u64> {
        let now = Instant::now();
        let mut m = self.lock();
        let mut new_keys = 0;
        for k in keys {
            match m.get_mut(k) {
                Some(e) => {
                    Self::expire(e, now, self.window);
                    if e.strike_expires.is_some_and(|expires| expires <= now) {
                        e.strikes = 0;
                        e.strike_expires = None;
                        e.blocked_until = None;
                    }
                    if let Some(until) = e.blocked_until.filter(|until| *until > now) {
                        return Err(Self::retry_seconds(until.duration_since(now)).max(1));
                    }
                    if e.failures.len() + e.in_flight >= self.limit {
                        e.strikes = e.strikes.saturating_add(1).max(1);
                        e.strike_expires = Some(now + Duration::from_secs(3600));
                        let multiplier = 1u32 << e.strikes.saturating_sub(1).min(6);
                        let backoff = self.window.max(Duration::from_secs(1)).saturating_mul(multiplier).min(Duration::from_secs(900));
                        e.blocked_until = Some(now + backoff);
                        return Err(Self::retry_seconds(backoff).max(1));
                    }
                }
                None => new_keys += 1,
            }
        }
        let cap = self.max_keys + if privileged { self.reserve_keys } else { 0 };
        if new_keys > 0 && m.len() + new_keys > cap {
            Self::prune(&mut m, now, self.window);
            if m.len() + new_keys > cap {
                return Err(self.window.as_secs()); // fail closed
            }
        }
        for k in keys {
            m.entry(k.clone()).or_default().in_flight += 1;
        }
        Ok(Attempt {
            limiter: self,
            keys: keys.to_vec(),
        })
    }

    /// Number of table entries (for tests and diagnostics).
    pub fn key_count(&self) -> usize {
        self.lock().len()
    }

    fn finish(&self, keys: &[String], failed: bool) {
        let now = Instant::now();
        let mut m = self.lock();
        for k in keys {
            if let Some(e) = m.get_mut(k) {
                e.in_flight = e.in_flight.saturating_sub(1);
                if failed {
                    e.failures.push_back(now);
                    while e.failures.len() > self.limit {
                        e.failures.pop_front();
                    }
                }
                if e.failures.is_empty() && e.in_flight == 0 && e.strikes == 0 && e.blocked_until.is_none_or(|until| until <= now) {
                    m.remove(k);
                }
            }
        }
    }
}

impl Attempt<'_> {
    /// Add keys to a running attempt (the user name is only known after the body is parsed).
    /// On refusal the attempt is unchanged and the seconds to wait are returned.
    pub fn add_keys(&mut self, keys: &[String], privileged: bool) -> Result<(), u64> {
        let mut extra = self.limiter.acquire(keys, privileged)?;
        // The reservations now belong to this attempt; `extra` drops with no keys.
        self.keys.append(&mut extra.keys);
        Ok(())
    }

    pub fn fail(mut self) {
        let keys = std::mem::take(&mut self.keys);
        self.limiter.finish(&keys, true);
    }

    /// Success: the reservation is released without recording a failure.
    pub fn succeed(mut self) {
        let keys = std::mem::take(&mut self.keys);
        self.limiter.finish(&keys, false);
    }

    /// The request was refused before any credential was checked (malformed, too large): no penalty.
    pub fn release(self) {
        self.succeed();
    }
}

impl Drop for Attempt<'_> {
    fn drop(&mut self) {
        if !self.keys.is_empty() {
            let keys = std::mem::take(&mut self.keys);
            self.limiter.finish(&keys, true);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn password_policy_uses_the_single_factor_minimum_and_allows_long_passphrases() {
        assert!(valid_password(&"x".repeat(MIN_PASSWORD_LEN - 1)).is_err());
        assert!(valid_password(&"x".repeat(MIN_PASSWORD_LEN)).is_ok());
        assert!(valid_password(&"correct horse battery staple".repeat(8)).is_ok());
        assert!(valid_password(&"x".repeat(MAX_PASSWORD_LEN + 1)).is_err());
    }
}
