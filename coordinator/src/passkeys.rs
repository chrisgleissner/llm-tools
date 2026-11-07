//! Server-side primitives for WebAuthn ceremonies. HTTP/UI wiring deliberately lives elsewhere.
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use url::Url;
use webauthn_rs::prelude::{
    COSEAlgorithm, CreationChallengeResponse, DiscoverableAuthentication, Passkey, PasskeyAuthentication, PasskeyRegistration, PublicKeyCredential,
    RegisterPublicKeyCredential, RequestChallengeResponse, Uuid, Webauthn, WebauthnBuilder,
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
    DiscoverableAuthentication(DiscoverableAuthentication),
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

/// Return the canonical browser origin WebAuthn accepts. IP literals are excluded because they
/// cannot be WebAuthn RP IDs, even when the operator supplies a trusted certificate for one.
pub fn canonical_origin(origin: &str) -> Result<String, String> {
    let url = Url::parse(origin).map_err(|_| "invalid public origin".to_string())?;
    if url.scheme() != "https"
        || url.host().is_none()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.host(), Some(url::Host::Domain(_)))
    {
        return Err("public_origin must be an HTTPS DNS origin without a path, query, fragment or credentials".into());
    }
    Ok(url.origin().ascii_serialization())
}

/// Construct a strict HTTPS-only WebAuthn verifier from the exact browser origin. An IP origin
/// cannot form an RP ID and is rejected by `webauthn-rs`.
pub fn verifier(origin: &str) -> Result<Webauthn, String> {
    let canonical = canonical_origin(origin)?;
    let url = Url::parse(&canonical).map_err(|_| "invalid WebAuthn origin".to_string())?;
    let rp_id = url.host_str().ok_or("invalid WebAuthn RP ID")?;
    WebauthnBuilder::new(rp_id, &url)
        .map_err(|_| "invalid WebAuthn RP ID/origin".to_string())?
        .rp_name("LLM Bridge")
        .credential_algorithms(vec![COSEAlgorithm::ES256, COSEAlgorithm::EDDSA, COSEAlgorithm::RS256])
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

/// Read an AAGUID from a registration response after `finish_registration` has validated it.
/// `none` attestation commonly supplies the all-zero AAGUID; preserve that value because it
/// means the authenticator intentionally withheld its model identity.
pub fn aaguid_from_attestation_object(attestation_object: &[u8]) -> Option<String> {
    const MAX_ATTESTATION_BYTES: usize = 4096;
    const ATT_DATA_FLAG: u8 = 0x40;
    if attestation_object.len() > MAX_ATTESTATION_BYTES {
        return None;
    }
    let decoded: serde_cbor_2::Value = serde_cbor_2::from_slice(attestation_object).ok()?;
    let map = match decoded {
        serde_cbor_2::Value::Map(map) => map,
        _ => return None,
    };
    let auth_data = match map.get(&serde_cbor_2::Value::Text("authData".into()))? {
        serde_cbor_2::Value::Bytes(bytes) => bytes,
        _ => return None,
    };
    if auth_data.len() < 55 || auth_data.get(32).is_none_or(|flags| flags & ATT_DATA_FLAG == 0) {
        return None;
    }
    let raw = &auth_data[37..53];
    let hex = hex::encode(raw);
    Some(format!("{}-{}-{}-{}-{}", &hex[0..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..32]))
}

pub fn begin_authentication(wa: &Webauthn, credentials: &[Passkey]) -> Result<(RequestChallengeResponse, CeremonyState), String> {
    let _ = credentials;
    let (options, state) = wa.start_discoverable_authentication().map_err(|e| e.to_string())?;
    Ok((options, CeremonyState::DiscoverableAuthentication(state)))
}

pub fn finish_authentication(
    wa: &Webauthn,
    state: &CeremonyState,
    credential: &PublicKeyCredential,
    credentials: &[Passkey],
) -> Result<webauthn_rs::prelude::AuthenticationResult, String> {
    match state {
        CeremonyState::Authentication(state) => wa.finish_passkey_authentication(credential, state).map_err(|e| e.to_string()),
        CeremonyState::DiscoverableAuthentication(state) => {
            let (user_id, credential_id) = wa.identify_discoverable_authentication(credential).map_err(|e| e.to_string())?;
            if user_id != Uuid::from_u128(1) {
                return Err("passkey belongs to a different user".into());
            }
            let passkey = credentials
                .iter()
                .find(|passkey| passkey.cred_id().as_slice() == credential_id)
                .ok_or_else(|| "discoverable credential is not registered".to_string())?;
            wa.finish_discoverable_authentication_v2(credential, state.clone(), passkey)
                .map_err(|e| e.to_string())
        }
        CeremonyState::Registration(_) => Err("wrong WebAuthn ceremony type".into()),
    }
}

#[cfg(test)]
mod aaguid_tests {
    use super::*;
    use std::collections::BTreeMap;

    fn attestation(auth_data: Vec<u8>) -> Vec<u8> {
        let mut map = BTreeMap::new();
        map.insert(serde_cbor_2::Value::Text("authData".into()), serde_cbor_2::Value::Bytes(auth_data));
        serde_cbor_2::to_vec(&serde_cbor_2::Value::Map(map)).unwrap()
    }

    #[test]
    fn reads_the_aaguid_from_attested_authenticator_data() {
        let mut auth_data = vec![0; 32];
        auth_data.push(0x41);
        auth_data.extend([0; 4]);
        auth_data.extend([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff]);
        auth_data.extend([0, 1, 0x42]);
        assert_eq!(
            aaguid_from_attestation_object(&attestation(auth_data)),
            Some("00112233-4455-6677-8899-aabbccddeeff".into())
        );
    }

    #[test]
    fn preserves_zero_aaguid_and_rejects_missing_or_unbounded_attestation_data() {
        let mut auth_data = vec![0; 32];
        auth_data.push(0x41);
        auth_data.extend([0; 4 + 16 + 3]);
        assert_eq!(
            aaguid_from_attestation_object(&attestation(auth_data)),
            Some("00000000-0000-0000-0000-000000000000".into())
        );

        let mut no_attested_data = vec![0; 32];
        no_attested_data.push(0x01);
        no_attested_data.extend([0; 4 + 16 + 3]);
        assert_eq!(aaguid_from_attestation_object(&attestation(no_attested_data)), None);
        assert_eq!(aaguid_from_attestation_object(&[0; 4097]), None);
        assert_eq!(aaguid_from_attestation_object(&[0xff]), None);
    }
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
    use base64::{
        engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
        Engine as _,
    };
    use ring::{
        digest,
        signature::{self, KeyPair},
    };
    use url::Url;
    use webauthn_rs::prelude::{Webauthn, WebauthnBuilder};

    // A fixed P-256 PKCS#8 key makes these real signed ceremony vectors reproducible. The
    // challenge remains generated by webauthn-rs, as it is in production; the response builder
    // copies that challenge into clientDataJSON before signing the exact browser payload.
    const TEST_PKCS8_B64: &str = "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgmBDcYjv+w9vYwFdlCx6Pv7kXGigf2FS5g+xJOXizU0agCgYIKoZIzj0DAQehRANCAAT/d1Y5absP9qfWxS5mBDDx5WxpBp/VBISyzNSfsIJ6eeU8G8QG2ghhcQKRNqhU/5+dYqFcTvqLky35teBLmiO7";
    const CREDENTIAL_ID: &[u8] = b"fixed-test-credential-id";
    const FIXED_CHALLENGE: [u8; 32] = [0x42; 32];
    // Signatures are fixed vectors over fixed authenticatorData and clientDataJSON. The second
    // vector deliberately has UP set and UV clear, so it isolates the UV policy check.
    const ASSERTION_SIGNATURE_UV: &[u8] = &[
        0x30, 0x44, 0x02, 0x20, 0x7e, 0x01, 0x97, 0x96, 0x8b, 0xb9, 0x15, 0x2d, 0x40, 0x78, 0x40, 0x48, 0x32, 0x51, 0x20, 0x78, 0x8e, 0x3a, 0x89, 0x7e, 0x99,
        0xbc, 0x0e, 0xd3, 0x65, 0xbb, 0xdf, 0x82, 0xf7, 0xde, 0x29, 0xbf, 0x02, 0x20, 0x29, 0x58, 0x26, 0xb9, 0x0f, 0xb5, 0x57, 0xc8, 0x60, 0xd4, 0x52, 0x52,
        0xf8, 0x5f, 0xaa, 0xb7, 0x26, 0x04, 0xfc, 0xce, 0xdb, 0x0e, 0x3d, 0x76, 0x18, 0x9a, 0x34, 0x9d, 0xf1, 0xdd, 0xda, 0x57,
    ];
    const ASSERTION_SIGNATURE_NO_UV: &[u8] = &[
        0x30, 0x45, 0x02, 0x21, 0x00, 0xb8, 0x82, 0x8c, 0xbe, 0x5b, 0xe6, 0x95, 0x52, 0x6d, 0x65, 0x8f, 0x17, 0x46, 0x54, 0x2f, 0x91, 0x46, 0xb6, 0x47, 0x93,
        0xa3, 0x92, 0xf3, 0x32, 0x06, 0xd9, 0x84, 0x0d, 0xea, 0x35, 0x00, 0xca, 0x02, 0x20, 0x02, 0xe4, 0xb3, 0xc8, 0x9e, 0xb0, 0xa4, 0x3d, 0x6c, 0x40, 0xea,
        0xd4, 0x92, 0x81, 0x47, 0x8a, 0x80, 0xaa, 0x81, 0x9b, 0x14, 0x29, 0x71, 0x6a, 0x36, 0x65, 0xdf, 0xbd, 0x76, 0xf4, 0xdb, 0x1a,
    ];

    fn cbor_int(value: i64) -> serde_cbor_2::Value {
        serde_cbor_2::Value::Integer(value.into())
    }

    fn public_key() -> (signature::EcdsaKeyPair, Vec<u8>, Vec<u8>) {
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = STANDARD.decode(TEST_PKCS8_B64).expect("fixed PKCS#8 base64");
        let key = signature::EcdsaKeyPair::from_pkcs8(&signature::ECDSA_P256_SHA256_FIXED_SIGNING, &pkcs8, &rng).expect("fixed test key is valid");
        let point = key.public_key().as_ref();
        assert_eq!(point.len(), 65);
        let x = point[1..33].to_vec();
        let y = point[33..65].to_vec();
        (key, x, y)
    }

    fn cose_public_key(x: Vec<u8>, y: Vec<u8>) -> serde_cbor_2::Value {
        use serde_cbor_2::Value::{Bytes, Map};
        let mut map = std::collections::BTreeMap::new();
        map.insert(cbor_int(1), cbor_int(2)); // kty: EC2
        map.insert(cbor_int(3), cbor_int(-7)); // alg: ES256
        map.insert(cbor_int(-1), cbor_int(1)); // crv: P-256
        map.insert(cbor_int(-2), Bytes(x));
        map.insert(cbor_int(-3), Bytes(y));
        Map(map)
    }

    fn client_data(kind: &str, challenge: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "type": kind,
            "challenge": challenge,
            "origin": "https://bridge.example.test",
            "crossOrigin": false
        }))
        .unwrap()
    }

    fn fixed_state(mut state: CeremonyState) -> CeremonyState {
        fn replace_challenge(value: &mut serde_json::Value) {
            match value {
                serde_json::Value::Object(object) => {
                    for (name, child) in object.iter_mut() {
                        if name == "challenge" {
                            *child = serde_json::Value::String(URL_SAFE_NO_PAD.encode(FIXED_CHALLENGE));
                        } else {
                            replace_challenge(child);
                        }
                    }
                }
                serde_json::Value::Array(items) => items.iter_mut().for_each(replace_challenge),
                _ => {}
            }
        }
        let mut value = serde_json::to_value(&state).unwrap();
        replace_challenge(&mut value);
        state = serde_json::from_value(value).unwrap();
        state
    }

    fn fix_creation_challenge(options: &mut CreationChallengeResponse) {
        options.public_key.challenge = FIXED_CHALLENGE.to_vec();
    }

    fn fix_request_challenge(options: &mut RequestChallengeResponse) {
        options.public_key.challenge = FIXED_CHALLENGE.to_vec();
    }

    fn registration_response(options: &CreationChallengeResponse, x: Vec<u8>, y: Vec<u8>) -> RegisterPublicKeyCredential {
        let challenge = URL_SAFE_NO_PAD.encode(&options.public_key.challenge);
        let rp_hash = digest::digest(&digest::SHA256, b"bridge.example.test");
        let mut auth_data = rp_hash.as_ref().to_vec();
        auth_data.push(0x4d); // UP | UV | AT | BE, with BS clear (synced-capable credential).
        auth_data.extend(0_u32.to_be_bytes());
        auth_data.extend([0; 16]); // none-attestation AAGUID
        auth_data.extend((CREDENTIAL_ID.len() as u16).to_be_bytes());
        auth_data.extend(CREDENTIAL_ID);
        auth_data.extend(serde_cbor_2::to_vec(&cose_public_key(x, y)).unwrap());
        let mut attestation = std::collections::BTreeMap::new();
        attestation.insert(serde_cbor_2::Value::Text("fmt".into()), serde_cbor_2::Value::Text("none".into()));
        attestation.insert(serde_cbor_2::Value::Text("attStmt".into()), serde_cbor_2::Value::Map(Default::default()));
        attestation.insert(serde_cbor_2::Value::Text("authData".into()), serde_cbor_2::Value::Bytes(auth_data));
        let encoded_id = URL_SAFE_NO_PAD.encode(CREDENTIAL_ID);
        serde_json::from_value(serde_json::json!({
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "attestationObject": URL_SAFE_NO_PAD.encode(serde_cbor_2::to_vec(&serde_cbor_2::Value::Map(attestation)).unwrap()),
                "clientDataJSON": URL_SAFE_NO_PAD.encode(client_data("webauthn.create", &challenge)),
                "transports": ["internal"]
            }
        }))
        .unwrap()
    }

    fn assertion_response(options: &RequestChallengeResponse, flags: u8) -> PublicKeyCredential {
        assert_eq!(options.public_key.challenge, FIXED_CHALLENGE);
        let mut auth_data = digest::digest(&digest::SHA256, b"bridge.example.test").as_ref().to_vec();
        auth_data.push(flags);
        auth_data.extend(1_u32.to_be_bytes());
        let client_data =
            br#"{"type":"webauthn.get","challenge":"QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI","origin":"https://bridge.example.test","crossOrigin":false}"#
                .to_vec();
        let signature = if flags & 0x04 == 0 {
            ASSERTION_SIGNATURE_NO_UV
        } else {
            ASSERTION_SIGNATURE_UV
        };
        let encoded_id = URL_SAFE_NO_PAD.encode(CREDENTIAL_ID);
        serde_json::from_value(serde_json::json!({
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "authenticatorData": URL_SAFE_NO_PAD.encode(auth_data),
                "clientDataJSON": URL_SAFE_NO_PAD.encode(client_data),
                "signature": URL_SAFE_NO_PAD.encode(signature),
                "userHandle": URL_SAFE_NO_PAD.encode(Uuid::from_u128(1).as_bytes())
            }
        }))
        .unwrap()
    }

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

    #[test]
    fn canonical_origin_requires_exact_https_dns_origin() {
        assert_eq!(canonical_origin("https://bridge.example.test").unwrap(), "https://bridge.example.test");
        assert_eq!(
            canonical_origin("https://BRIDGE.example.test:8443").unwrap(),
            "https://bridge.example.test:8443"
        );
        for invalid in [
            "http://bridge.example.test",
            "https://192.0.2.1:8443",
            "https://bridge.example.test/path",
            "https://user@bridge.example.test",
            "https://bridge.example.test?next=/",
        ] {
            assert!(canonical_origin(invalid).is_err(), "accepted {invalid}");
        }
    }

    fn registered_test_passkey(wa: &Webauthn) -> (Passkey, signature::EcdsaKeyPair) {
        let (key, x, y) = public_key();
        let (mut options, state) = begin_registration(wa, "admin", "admin", "fixed vector").unwrap();
        fix_creation_challenge(&mut options);
        let state = fixed_state(state);
        let response = registration_response(&options, x, y);
        let passkey = finish_registration(wa, &state, &response).expect("valid fixed registration vector");
        (passkey, key)
    }

    #[test]
    fn fixed_key_registration_and_discoverable_assertion_verify() {
        let wa = verifier("https://bridge.example.test").unwrap();
        let (passkey, _key) = registered_test_passkey(&wa);
        assert_eq!(passkey.cred_id(), CREDENTIAL_ID);

        let (mut options, state) = begin_authentication(&wa, std::slice::from_ref(&passkey)).unwrap();
        fix_request_challenge(&mut options);
        let state = fixed_state(state);
        let response = assertion_response(&options, 0x0d); // UP, UV and BE; BS clear
        let result = finish_authentication(&wa, &state, &response, &[passkey]).expect("valid fixed assertion vector");
        assert!(result.user_verified());
        assert_eq!(result.counter(), 1);
    }

    #[test]
    fn fixed_key_assertion_without_user_verification_is_rejected() {
        let wa = verifier("https://bridge.example.test").unwrap();
        let (passkey, _key) = registered_test_passkey(&wa);
        let (mut options, state) = begin_authentication(&wa, std::slice::from_ref(&passkey)).unwrap();
        fix_request_challenge(&mut options);
        let state = fixed_state(state);
        let response = assertion_response(&options, 0x09); // valid signature, UP and BE, but no UV
        assert!(finish_authentication(&wa, &state, &response, &[passkey]).is_err());
    }
}
