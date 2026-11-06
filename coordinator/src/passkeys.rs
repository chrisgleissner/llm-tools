//! Server-side primitives for WebAuthn ceremonies. HTTP/UI wiring deliberately lives elsewhere.
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use webauthn_rs::prelude::{Passkey, PasskeyAuthentication, PasskeyRegistration};

pub const CHALLENGE_TTL_SECS: i64 = 300;
pub const CHALLENGE_BYTES: usize = 32;

/// State returned by webauthn-rs must stay server-side and be persisted with its browser
/// challenge. The payload is JSON-serialized only into the private coordinator database.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", content = "state", rename_all = "snake_case")]
pub enum CeremonyState {
    Registration(PasskeyRegistration),
    Authentication(PasskeyAuthentication),
}

/// Stored passkey plus fields needed for operator-visible metadata and audit decisions. The
/// opaque library credential is authoritative for cryptographic verification and counters.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StoredPasskey {
    pub credential_id: String,
    pub principal_id: String,
    pub credential: Passkey,
    pub user_name: String,
    pub created_at: i64,
    pub last_used: Option<i64>,
    pub aaguid: Option<String>,
    pub transports: Vec<String>,
    pub backup_eligible: bool,
    pub backed_up: bool,
    pub sign_count: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewChallenge {
    pub id: String,
    pub challenge: Vec<u8>,
    pub created_at: i64,
    pub expires_at: i64,
}

/// Generate an unguessable challenge identifier and 256-bit WebAuthn challenge using the
/// operating-system RNG. The database layer enforces the same five-minute maximum at insertion.
pub fn new_challenge(now: i64) -> Result<NewChallenge, ring::error::Unspecified> {
    let rng = SystemRandom::new();
    let mut challenge = vec![0; CHALLENGE_BYTES];
    let mut id = [0; 32];
    rng.fill(&mut challenge)?;
    rng.fill(&mut id)?;
    Ok(NewChallenge {
        id: hex::encode(id),
        challenge,
        created_at: now,
        expires_at: now + CHALLENGE_TTL_SECS,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use url::Url;
    use webauthn_rs::prelude::{Webauthn, WebauthnBuilder};

    #[test]
    fn challenge_has_256_bits_and_five_minute_lifetime() {
        let a = new_challenge(1_800_000_000).unwrap();
        let b = new_challenge(1_800_000_000).unwrap();
        assert_eq!(a.challenge.len(), 32);
        assert_eq!(a.expires_at - a.created_at, 300);
        assert_ne!(a.id, b.id);
        assert_ne!(a.challenge, b.challenge);
    }

    #[test]
    fn library_rejects_ip_address_rp_origin_and_uses_fixed_origin() {
        let ip = Url::parse("https://192.0.2.1:8443").unwrap();
        assert!(WebauthnBuilder::new("192.0.2.1", &ip).is_err());
        let origin = Url::parse("https://bridge.example.test").unwrap();
        let _wa: Webauthn = WebauthnBuilder::new("bridge.example.test", &origin)
            .unwrap()
            .rp_name("LLM Bridge")
            .build()
            .unwrap();
    }
}
