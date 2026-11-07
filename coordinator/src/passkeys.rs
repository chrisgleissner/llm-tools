//! Server-side primitives for WebAuthn ceremonies. HTTP/UI wiring deliberately lives elsewhere.
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use url::Url;
use webauthn_rs::prelude::{
    CreationChallengeResponse, Passkey, PasskeyAuthentication, PasskeyRegistration, PublicKeyCredential, RegisterPublicKeyCredential, RequestChallengeResponse,
    Uuid, Webauthn, WebauthnBuilder,
};

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

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StoredCeremony {
    pub origin: String,
    pub credential_name: Option<String>,
    pub state: CeremonyState,
}

/// Construct a strict HTTPS-only WebAuthn verifier from the exact browser origin. An IP origin
/// cannot form an RP ID and is rejected by `webauthn-rs`.
pub fn verifier(origin: &str) -> Result<Webauthn, String> {
    let url = Url::parse(origin).map_err(|_| "invalid WebAuthn origin".to_string())?;
    if url.scheme() != "https"
        || url.host().is_none()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err("WebAuthn requires an exact HTTPS origin".into());
    }
    let rp_id = url.host_str().ok_or("invalid WebAuthn RP ID")?;
    WebauthnBuilder::new(rp_id, &url)
        .map_err(|_| "invalid WebAuthn RP ID/origin".to_string())?
        .rp_name("LLM Bridge")
        .build()
        .map_err(|_| "invalid WebAuthn configuration".to_string())
}

pub fn begin_registration(wa: &Webauthn, principal: &str, username: &str, label: &str) -> Result<(CreationChallengeResponse, CeremonyState), String> {
    // This coordinator currently has one administrator principal; keep the WebAuthn user handle
    // stable across registration ceremonies without storing an additional identity field.
    let _ = principal;
    let uuid = Uuid::from_u128(1);
    let (options, state) = wa.start_passkey_registration(uuid, username, username, None).map_err(|e| e.to_string())?;
    let _ = label; // Kept in the typed stored ceremony, not the WebAuthn user display name.
    Ok((options, CeremonyState::Registration(state)))
}

pub fn finish_registration(wa: &Webauthn, state: &CeremonyState, credential: &RegisterPublicKeyCredential) -> Result<Passkey, String> {
    let CeremonyState::Registration(state) = state else {
        return Err("wrong WebAuthn ceremony type".into());
    };
    wa.finish_passkey_registration(credential, state).map_err(|e| e.to_string())
}

pub fn begin_authentication(wa: &Webauthn, credentials: &[Passkey]) -> Result<(RequestChallengeResponse, CeremonyState), String> {
    let (options, state) = wa.start_passkey_authentication(credentials).map_err(|e| e.to_string())?;
    Ok((options, CeremonyState::Authentication(state)))
}

pub fn finish_authentication(
    wa: &Webauthn,
    state: &CeremonyState,
    credential: &PublicKeyCredential,
) -> Result<webauthn_rs::prelude::AuthenticationResult, String> {
    let CeremonyState::Authentication(state) = state else {
        return Err("wrong WebAuthn ceremony type".into());
    };
    wa.finish_passkey_authentication(credential, state).map_err(|e| e.to_string())
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
        assert!(WebauthnBuilder::new("other.example.test", &origin).is_err());
        let _wa: Webauthn = WebauthnBuilder::new("bridge.example.test", &origin)
            .unwrap()
            .rp_name("LLM Bridge")
            .build()
            .unwrap();
    }
}
