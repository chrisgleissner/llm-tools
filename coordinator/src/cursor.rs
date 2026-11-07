//! Authenticated and encrypted route- and filter-bound opaque list cursors.

use base64::Engine;
use rand::TryRng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const MAX_CURSOR_BYTES: usize = 2048;
const NONCE_BYTES: usize = 12;
const TAG_BYTES: usize = 16;

#[derive(Debug, PartialEq, Eq)]
pub struct CursorError;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Claims {
    v: u8,
    route: String,
    filters: String,
    offset: usize,
}

fn filter_fingerprint(filters: &str) -> String {
    hex::encode(Sha256::digest(filters.as_bytes()))
}

fn encryption_key(key: &str) -> [u8; 32] {
    Sha256::digest(key.as_bytes()).into()
}

pub fn issue(key: &str, route: &str, filters: &str, offset: usize) -> String {
    let claims = Claims {
        v: 1,
        route: route.to_string(),
        filters: filter_fingerprint(filters),
        offset,
    };
    let payload = serde_json::to_vec(&claims).expect("cursor claims serialize");
    let mut encrypted = payload;
    let mut nonce = [0u8; NONCE_BYTES];
    rand::rngs::SysRng.try_fill_bytes(&mut nonce).expect("operating system random source");
    let sealing_key =
        ring::aead::LessSafeKey::new(ring::aead::UnboundKey::new(&ring::aead::AES_256_GCM, &encryption_key(key)).expect("AES key length is fixed"));
    sealing_key
        .seal_in_place_append_tag(ring::aead::Nonce::assume_unique_for_key(nonce), ring::aead::Aad::empty(), &mut encrypted)
        .expect("cursor encryption cannot fail for bounded claims");
    let mut signed = nonce.to_vec();
    signed.extend_from_slice(&encrypted);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(signed)
}

pub fn read(key: &str, route: &str, filters: &str, cursor: &str) -> Result<usize, CursorError> {
    if cursor.is_empty() || cursor.len() > MAX_CURSOR_BYTES {
        return Err(CursorError);
    }
    let mut signed = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(cursor).map_err(|_| CursorError)?;
    if signed.len() <= NONCE_BYTES + TAG_BYTES {
        return Err(CursorError);
    }
    let nonce: [u8; NONCE_BYTES] = signed[..NONCE_BYTES].try_into().map_err(|_| CursorError)?;
    let opening_key = ring::aead::LessSafeKey::new(ring::aead::UnboundKey::new(&ring::aead::AES_256_GCM, &encryption_key(key)).map_err(|_| CursorError)?);
    let payload = opening_key
        .open_in_place(
            ring::aead::Nonce::assume_unique_for_key(nonce),
            ring::aead::Aad::empty(),
            &mut signed[NONCE_BYTES..],
        )
        .map_err(|_| CursorError)?;
    let claims: Claims = serde_json::from_slice(payload).map_err(|_| CursorError)?;
    if claims.v != 1 || claims.route != route || claims.filters != filter_fingerprint(filters) {
        return Err(CursorError);
    }
    Ok(claims.offset)
}

#[cfg(test)]
mod tests {
    use super::{issue, read};
    use base64::Engine;

    #[test]
    fn cursors_are_bound_to_route_filters_and_signature() {
        let cursor = issue("persistent test key", "missions", "status=RUNNING", 25);
        let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(&cursor).unwrap();
        assert!(!decoded.windows(6).any(|window| window == b"offset"));
        assert!(!decoded.windows(7).any(|window| window == b"RUNNING"));
        assert_eq!(read("persistent test key", "missions", "status=RUNNING", &cursor), Ok(25));
        assert!(read("persistent test key", "workers", "status=RUNNING", &cursor).is_err());
        assert!(read("persistent test key", "missions", "status=FAILED", &cursor).is_err());
        assert!(read("different key", "missions", "status=RUNNING", &cursor).is_err());
        let mut forged = cursor.into_bytes();
        let first = forged.first_mut().expect("base64 cursor is not empty");
        *first = if *first == b'A' { b'B' } else { b'A' };
        assert!(read("persistent test key", "missions", "status=RUNNING", std::str::from_utf8(&forged).unwrap()).is_err());
        assert!(read("persistent test key", "missions", "status=RUNNING", &"x".repeat(2049)).is_err());
    }
}
