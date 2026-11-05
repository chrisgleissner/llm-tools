//! Local CA and server certificate handling, and the rustls server configuration.

use crate::{write_new, write_replace};
use rcgen::{BasicConstraints, Certificate, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, KeyPair, KeyUsagePurpose};
use rustls::crypto::CryptoProvider;
use rustls::ServerConfig;
use std::net::IpAddr;
use std::path::Path;
use std::sync::Arc;

pub struct Sans {
    pub hostnames: Vec<String>,
    pub ips: Vec<IpAddr>,
}

pub(crate) fn civil_from_days(z: i64) -> (i32, u8, u8) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    ((y + i64::from(m <= 2)) as i32, m as u8, d as u8)
}

/// (year, month, day) of today plus `days`.
fn ymd_after(days: i64) -> (i32, u8, u8) {
    civil_from_days(crate::now() / 86_400 + days)
}

fn set_validity(params: &mut CertificateParams, valid_days: i64) {
    let (y, m, d) = ymd_after(-1);
    params.not_before = rcgen::date_time_ymd(y, m, d);
    let (y, m, d) = ymd_after(valid_days);
    params.not_after = rcgen::date_time_ymd(y, m, d);
}

/// The CA subject and key usages are fixed, so the CA certificate can be rebuilt from its
/// stored key when signing a renewed server certificate (same issuer name and key).
fn ca_certificate(ca_key: &KeyPair) -> Result<Certificate, String> {
    let mut params = CertificateParams::new(Vec::<String>::new()).map_err(|e| e.to_string())?;
    params.distinguished_name.push(DnType::CommonName, "Bridge Coordinator CA");
    params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    set_validity(&mut params, 3650);
    params.self_signed(ca_key).map_err(|e| e.to_string())
}

fn server_pem(ca_key: &KeyPair, sans: &Sans) -> Result<(String, String), String> {
    let mut names: Vec<String> = vec!["localhost".into(), "127.0.0.1".into(), "::1".into()];
    for h in &sans.hostnames {
        if h.is_empty() || !h.is_ascii() || h.contains(char::is_whitespace) {
            return Err(format!("invalid hostname {h:?}"));
        }
        names.push(h.clone());
    }
    names.extend(sans.ips.iter().map(|i| i.to_string()));
    names.sort();
    names.dedup();
    let mut params = CertificateParams::new(names).map_err(|e| e.to_string())?;
    params.distinguished_name.push(DnType::CommonName, "Bridge Coordinator");
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    set_validity(&mut params, 825);
    let key = KeyPair::generate().map_err(|e| e.to_string())?;
    let ca = ca_certificate(ca_key)?;
    let cert = params.signed_by(&key, &ca, ca_key).map_err(|e| e.to_string())?;
    Ok((cert.pem(), key.serialize_pem()))
}

/// Create the CA and server certificate when missing. Existing files are never overwritten.
pub fn init_certs(dir: &Path, sans: &Sans) -> Result<(), String> {
    let ca_pem_path = dir.join("ca.pem");
    let ca_key_path = dir.join("ca.key");
    let ca_key = if ca_key_path.exists() {
        let text = std::fs::read_to_string(&ca_key_path).map_err(|e| e.to_string())?;
        KeyPair::from_pem(&text).map_err(|e| e.to_string())?
    } else {
        let key = KeyPair::generate().map_err(|e| e.to_string())?;
        write_new(&ca_key_path, key.serialize_pem().as_bytes(), 0o600).map_err(|e| e.to_string())?;
        key
    };
    if !ca_pem_path.exists() {
        let ca = ca_certificate(&ca_key)?;
        write_new(&ca_pem_path, ca.pem().as_bytes(), 0o644).map_err(|e| e.to_string())?;
    }
    if !dir.join("server.pem").exists() && !dir.join("server.key").exists() {
        let (cert, key) = server_pem(&ca_key, sans)?;
        write_new(&dir.join("server.key"), key.as_bytes(), 0o600).map_err(|e| e.to_string())?;
        write_new(&dir.join("server.pem"), cert.as_bytes(), 0o644).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Replace the server certificate with a new one signed by the existing CA.
pub fn renew(dir: &Path, sans: &Sans) -> Result<(), String> {
    let text = std::fs::read_to_string(dir.join("ca.key")).map_err(|e| format!("cannot read ca.key: {e}"))?;
    let ca_key = KeyPair::from_pem(&text).map_err(|e| e.to_string())?;
    let (cert, key) = server_pem(&ca_key, sans)?;
    write_replace(&dir.join("server.key"), key.as_bytes(), 0o600).map_err(|e| e.to_string())?;
    write_replace(&dir.join("server.pem"), cert.as_bytes(), 0o644).map_err(|e| e.to_string())
}

/// Install administrator-provided files after checking that they form a usable pair.
pub fn install(dir: &Path, cert: &Path, key: &Path) -> Result<(), String> {
    let cert_bytes = std::fs::read(cert).map_err(|e| format!("cannot read {}: {e}", cert.display()))?;
    let key_bytes = std::fs::read(key).map_err(|e| format!("cannot read {}: {e}", key.display()))?;
    build_config(&cert_bytes, &key_bytes)?;
    write_replace(&dir.join("server.key"), &key_bytes, 0o600).map_err(|e| e.to_string())?;
    write_replace(&dir.join("server.pem"), &cert_bytes, 0o644).map_err(|e| e.to_string())
}

/// ring provider restricted to AEAD suites (TLS 1.3 suites and TLS 1.2 ECDHE-GCM/CHACHA20).
pub fn provider() -> Arc<CryptoProvider> {
    let mut p = rustls::crypto::ring::default_provider();
    p.cipher_suites.retain(|s| {
        let name = format!("{:?}", s.suite());
        name.contains("GCM") || name.contains("CHACHA20")
    });
    Arc::new(p)
}

pub fn build_config(cert_pem: &[u8], key_pem: &[u8]) -> Result<ServerConfig, String> {
    let certs = rustls_pemfile::certs(&mut &*cert_pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    if certs.is_empty() {
        return Err("no certificate found in PEM".into());
    }
    let key = rustls_pemfile::private_key(&mut &*key_pem)
        .map_err(|e| e.to_string())?
        .ok_or("no private key found in PEM")?;
    let mut cfg = ServerConfig::builder_with_provider(provider())
        .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])
        .map_err(|e| e.to_string())?
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| e.to_string())?;
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(cfg)
}

pub fn load_server_config(dir: &Path) -> Result<Arc<ServerConfig>, String> {
    let cert = std::fs::read(dir.join("server.pem")).map_err(|e| format!("cannot read server.pem: {e}"))?;
    let key = std::fs::read(dir.join("server.key")).map_err(|e| format!("cannot read server.key: {e}"))?;
    Ok(Arc::new(build_config(&cert, &key)?))
}
