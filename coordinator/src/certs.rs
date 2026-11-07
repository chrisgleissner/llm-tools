//! Local CA and server certificate handling, and the rustls server configuration.

use crate::{write_new, write_replace};
use rcgen::{
    BasicConstraints, Certificate, CertificateParams, CidrSubnet, DnType, ExtendedKeyUsagePurpose, GeneralSubtree, IsCa, Issuer, KeyPair, KeyUsagePurpose,
    NameConstraints,
};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::PrivateKeyDer;
use rustls::ServerConfig;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct WorkerClientIdentity {
    pub cert_pem: String,
    pub key_pem: String,
    pub fingerprint_sha256: String,
}

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

/// CA validity. Browsers and workers import the CA as a trust anchor, so it is kept short.
pub const CA_VALID_DAYS: i64 = 5 * 365;

/// DNS suffix that is always permitted besides `localhost` and the names given at init time
/// (RFC 8375: reserved for home networks, never delegated in the public DNS).
pub const HOME_ARPA: &str = "home.arpa";

/// The names the server certificate carries: `localhost`, loopback, the given host names and addresses.
fn san_list(sans: &Sans) -> Result<Vec<String>, String> {
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
    Ok(names)
}

/// Name constraints for the CA: exactly the server certificate's names, `localhost`, the
/// `home.arpa` subtree, the IPv4 loopback range and `::1`. Each address is a single-host range.
fn constraints_for(names: &[String]) -> NameConstraints {
    let mut permitted = vec![GeneralSubtree::DnsName("localhost".into()), GeneralSubtree::DnsName(HOME_ARPA.into())];
    permitted.push(GeneralSubtree::IpAddress(CidrSubnet::from_v4_prefix([127, 0, 0, 0], 8)));
    permitted.push(GeneralSubtree::IpAddress(CidrSubnet::from_addr_prefix(
        IpAddr::from([0u16, 0, 0, 0, 0, 0, 0, 1]),
        128,
    )));
    for n in names {
        let subtree = match n.parse::<IpAddr>() {
            Ok(ip) if ip.is_loopback() => continue, // covered by the loopback entries above
            Ok(ip) => GeneralSubtree::IpAddress(CidrSubnet::from_addr_prefix(ip, if ip.is_ipv4() { 32 } else { 128 })),
            Err(_) => GeneralSubtree::DnsName(n.clone()),
        };
        if !permitted.contains(&subtree) {
            permitted.push(subtree);
        }
    }
    NameConstraints {
        permitted_subtrees: permitted,
        excluded_subtrees: vec![],
    }
}

/// The CA certificate. `constrain` carries the server names for a new CA; the CA certificate is
/// also rebuilt from its stored key (with `None`) as the issuer when signing a renewed server
/// certificate: signing needs only the subject name and key, so the stored `ca.pem` stays the
/// certificate clients trust and its constraints are checked afterwards by [`verify_names`].
fn ca_params(constrain: Option<&[String]>) -> Result<CertificateParams, String> {
    let mut params = CertificateParams::new(Vec::<String>::new()).map_err(|e| e.to_string())?;
    params.distinguished_name.push(DnType::CommonName, "Bridge Coordinator CA");
    // pathLenConstraint = 0: the CA signs server certificates only, never another CA.
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    params.name_constraints = constrain.map(constraints_for);
    set_validity(&mut params, CA_VALID_DAYS);
    Ok(params)
}

fn ca_certificate(ca_key: &KeyPair, constrain: Option<&[String]>) -> Result<Certificate, String> {
    ca_params(constrain)?.self_signed(ca_key).map_err(|e| e.to_string())
}

fn server_pem(ca_key: &KeyPair, names: Vec<String>) -> Result<(String, String), String> {
    let mut params = CertificateParams::new(names).map_err(|e| e.to_string())?;
    params.distinguished_name.push(DnType::CommonName, "Bridge Coordinator");
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    // Strict X.509 verification (Python 3.13+ VERIFY_X509_STRICT, RFC 5280) needs the authority key
    // identifier on a CA-issued certificate. ExplicitNoCa also writes the subject key identifier
    // and a critical basicConstraints CA:FALSE.
    params.is_ca = IsCa::ExplicitNoCa;
    params.use_authority_key_identifier_extension = true;
    set_validity(&mut params, 825);
    let key = KeyPair::generate().map_err(|e| e.to_string())?;
    let issuer_params = ca_params(None)?;
    let issuer = Issuer::from_params(&issuer_params, ca_key);
    let cert = params.signed_by(&key, &issuer).map_err(|e| e.to_string())?;
    Ok((cert.pem(), key.serialize_pem()))
}

/// Issue one coordinator-CA client certificate for a named worker. The private key is returned
/// only to the enrollment caller; the coordinator stores only the certificate fingerprint.
pub fn issue_worker_client_identity(dir: &Path, worker_name: &str) -> Result<WorkerClientIdentity, String> {
    if !crate::protocol::valid_name(worker_name) {
        return Err("invalid worker name".into());
    }
    let ca_key_text = std::fs::read_to_string(dir.join("ca.key")).map_err(|e| format!("cannot read ca.key: {e}"))?;
    let ca_key = KeyPair::from_pem(&ca_key_text).map_err(|e| format!("invalid ca.key: {e}"))?;
    let mut params = CertificateParams::new(Vec::<String>::new()).map_err(|e| e.to_string())?;
    params.distinguished_name.push(DnType::CommonName, format!("Bridge Worker {worker_name}"));
    params.subject_alt_names.push(rcgen::SanType::URI(
        format!("spiffe://llm-bridge/worker/{worker_name}")
            .try_into()
            .map_err(|e: rcgen::Error| e.to_string())?,
    ));
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
    params.is_ca = IsCa::ExplicitNoCa;
    params.use_authority_key_identifier_extension = true;
    set_validity(&mut params, 365);
    let key = KeyPair::generate().map_err(|e| e.to_string())?;
    let issuer_params = ca_params(None)?;
    let issuer = Issuer::from_params(&issuer_params, &ca_key);
    let cert = params.signed_by(&key, &issuer).map_err(|e| e.to_string())?;
    use sha2::Digest;
    let fingerprint_sha256 = hex::encode(sha2::Sha256::digest(cert.der().as_ref()));
    Ok(WorkerClientIdentity {
        cert_pem: cert.pem(),
        key_pem: key.serialize_pem(),
        fingerprint_sha256,
    })
}

/// Sign a server certificate for exactly `names` with the CA key, without checking the CA's name
/// constraints. Tests use this to build a certificate the CA must not be trusted for.
pub fn issue_unchecked(ca_key_pem: &str, names: Vec<String>) -> Result<(String, String), String> {
    server_pem(&KeyPair::from_pem(ca_key_pem).map_err(|e| e.to_string())?, names)
}

/// Why [`verify_names`] refused a certificate.
#[derive(Debug)]
pub enum VerifyFailure {
    /// A name in the certificate is outside the CA's name constraints.
    NameConstraint,
    Other(String),
}

/// Verify `cert_pem` for every name in `names` against the CA in `ca_pem` with the same path
/// validation (webpki, including name constraints) that rustls clients use. Name constraints
/// apply to every name in the certificate, so one name outside them fails all of the checks.
pub fn verify_names(ca_pem: &[u8], cert_pem: &[u8], names: &[String]) -> Result<(), VerifyFailure> {
    use rustls::client::danger::ServerCertVerifier;
    let other = |e: &dyn std::fmt::Display| VerifyFailure::Other(e.to_string());
    let mut roots = rustls::RootCertStore::empty();
    for c in rustls::pki_types::CertificateDer::pem_slice_iter(ca_pem) {
        roots.add(c.map_err(|e| other(&e))?).map_err(|e| other(&e))?;
    }
    let verifier = rustls::client::WebPkiServerVerifier::builder_with_provider(Arc::new(roots), Arc::new(rustls::crypto::ring::default_provider()))
        .build()
        .map_err(|e| other(&e))?;
    let leaf = rustls::pki_types::CertificateDer::pem_slice_iter(cert_pem)
        .next()
        .ok_or_else(|| VerifyFailure::Other("no certificate found in PEM".into()))?
        .map_err(|e| other(&e))?;
    for n in names {
        let server_name = rustls::pki_types::ServerName::try_from(n.clone()).map_err(|e| other(&format!("{n}: {e}")))?;
        verifier
            .verify_server_cert(&leaf, &[], &server_name, &[], rustls::pki_types::UnixTime::now())
            .map_err(|e| {
                if format!("{e:?}").contains("NameConstraint") {
                    VerifyFailure::NameConstraint
                } else {
                    VerifyFailure::Other(format!("the new server certificate does not validate against ca.pem for {n}: {e}"))
                }
            })?;
    }
    Ok(())
}

/// True when `ca.pem` carries a NameConstraints extension (OID 2.5.29.30). A CA from before name
/// constraints were introduced vouches for any name; `cert new-ca` replaces it.
pub fn ca_is_constrained(dir: &Path) -> bool {
    let Ok(pem) = std::fs::read(dir.join("ca.pem")) else { return true };
    let Some(Ok(der)) = rustls::pki_types::CertificateDer::pem_slice_iter(&pem).next() else {
        return true;
    };
    der.as_ref().windows(5).any(|w| w == [0x06, 0x03, 0x55, 0x1d, 0x1e])
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
    let names = san_list(sans)?;
    if !ca_pem_path.exists() {
        let ca = ca_certificate(&ca_key, Some(&names))?;
        write_new(&ca_pem_path, ca.pem().as_bytes(), 0o644).map_err(|e| e.to_string())?;
    }
    if !dir.join("server.pem").exists() && !dir.join("server.key").exists() {
        let (cert, key) = server_pem(&ca_key, names)?;
        write_new(&dir.join("server.key"), key.as_bytes(), 0o600).map_err(|e| e.to_string())?;
        write_new(&dir.join("server.pem"), cert.as_bytes(), 0o644).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Replace the server certificate with a new one signed by the existing CA. The new certificate
/// is checked against the CA certificate in `ca.pem` first; a name outside the CA's name
/// constraints is refused without changing any file.
pub fn renew(dir: &Path, sans: &Sans) -> Result<(), String> {
    let text = std::fs::read_to_string(dir.join("ca.key")).map_err(|e| format!("cannot read ca.key: {e}"))?;
    let ca_key = KeyPair::from_pem(&text).map_err(|e| e.to_string())?;
    let names = san_list(sans)?;
    let (cert, key) = server_pem(&ca_key, names.clone())?;
    let ca_pem = std::fs::read(dir.join("ca.pem")).map_err(|e| format!("cannot read ca.pem: {e}"))?;
    match verify_names(&ca_pem, cert.as_bytes(), &names) {
        Ok(()) => {}
        Err(VerifyFailure::Other(e)) => return Err(e),
        Err(VerifyFailure::NameConstraint) => {
            // Name constraints apply to the whole certificate: probe each name alone to name the offenders.
            let outside: Vec<&String> = names
                .iter()
                .filter(|n| {
                    server_pem(&ca_key, vec![(*n).clone()]).map_or(true, |(c, _)| verify_names(&ca_pem, c.as_bytes(), std::slice::from_ref(n)).is_err())
                })
                .collect();
            return Err(format!(
                "not renewed: {} outside the name constraints of the CA in ca.pem. That CA only vouches for the names it was created \
                 with, localhost, {HOME_ARPA} and its subdomains, and loopback addresses. Run `cert new-ca` with the full list of names \
                 to create a new constrained CA and server certificate; browsers must then import the new ca.pem and workers must be re-enrolled",
                outside.iter().map(|n| n.as_str()).collect::<Vec<_>>().join(", ")
            ));
        }
    }
    write_replace(&dir.join("server.key"), key.as_bytes(), 0o600).map_err(|e| e.to_string())?;
    write_replace(&dir.join("server.pem"), cert.as_bytes(), 0o644).map_err(|e| e.to_string())
}

/// Create a new name-constrained CA and a server certificate for `sans`. The previous CA files
/// are kept as `ca.pem.old-<UTC>` and `ca.key.old-<UTC>`. Returns the stamp used.
pub fn new_ca(dir: &Path, sans: &Sans) -> Result<String, String> {
    let names = san_list(sans)?;
    let ca_key = KeyPair::generate().map_err(|e| e.to_string())?;
    let ca = ca_certificate(&ca_key, Some(&names))?;
    let (cert, key) = server_pem(&ca_key, names.clone())?;
    verify_names(ca.pem().as_bytes(), cert.as_bytes(), &names).map_err(|e| format!("new certificate does not validate against the new CA: {e:?}"))?;
    let stamp = crate::utc_stamp();
    for f in ["ca.pem", "ca.key"] {
        let from = dir.join(f);
        if from.exists() {
            std::fs::rename(&from, dir.join(format!("{f}.old-{stamp}"))).map_err(|e| format!("cannot keep the old {f}: {e}"))?;
        }
    }
    write_replace(&dir.join("ca.key"), ca_key.serialize_pem().as_bytes(), 0o600).map_err(|e| e.to_string())?;
    write_replace(&dir.join("ca.pem"), ca.pem().as_bytes(), 0o644).map_err(|e| e.to_string())?;
    write_replace(&dir.join("server.key"), key.as_bytes(), 0o600).map_err(|e| e.to_string())?;
    write_replace(&dir.join("server.pem"), cert.as_bytes(), 0o644).map_err(|e| e.to_string())?;
    Ok(stamp)
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
    build_config_with_min_tls(cert_pem, key_pem, "1.3")
}

pub fn build_config_with_min_tls(cert_pem: &[u8], key_pem: &[u8], min_tls: &str) -> Result<ServerConfig, String> {
    let certs = rustls::pki_types::CertificateDer::pem_slice_iter(cert_pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    if certs.is_empty() {
        return Err("no certificate found in PEM".into());
    }
    let key = PrivateKeyDer::from_pem_slice(key_pem).map_err(|e| format!("no private key found in PEM: {e}"))?;
    let versions = tls_versions(min_tls)?;
    let mut cfg = ServerConfig::builder_with_provider(provider())
        .with_protocol_versions(&versions)
        .map_err(|e| e.to_string())?
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| e.to_string())?;
    disable_resumption_and_early_data(&mut cfg);
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(cfg)
}

fn tls_versions(min_tls: &str) -> Result<Vec<&'static rustls::SupportedProtocolVersion>, String> {
    match min_tls {
        "1.3" => Ok(vec![&rustls::version::TLS13]),
        "1.2" => Ok(vec![&rustls::version::TLS13, &rustls::version::TLS12]),
        _ => Err(format!("unsupported min_tls {min_tls:?}; choose \"1.3\" or \"1.2\"")),
    }
}

/// Rebuild the selected server identity with coordinator-CA client authentication. `required`
/// is true only on a split worker-channel listener; a combined legacy listener requests and
/// validates presented certificates but lets ordinary browser/API clients omit one.
pub fn server_config_with_client_auth(
    cert_resolver: Arc<dyn rustls::server::ResolvesServerCert>,
    ca_pem: &[u8],
    min_tls: &str,
    required: bool,
) -> Result<ServerConfig, String> {
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls::pki_types::CertificateDer::pem_slice_iter(ca_pem) {
        roots
            .add(cert.map_err(|e| format!("invalid coordinator CA certificate: {e}"))?)
            .map_err(|e| format!("invalid coordinator CA certificate: {e}"))?;
    }
    if roots.is_empty() {
        return Err("coordinator CA has no certificates".into());
    }
    let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider());
    let verifier = if required {
        verifier.build()
    } else {
        verifier.allow_unauthenticated().build()
    }
    .map_err(|e| format!("cannot configure worker client certificate verification: {e}"))?;
    let mut config = ServerConfig::builder_with_provider(provider())
        .with_protocol_versions(&tls_versions(min_tls)?)
        .map_err(|e| e.to_string())?
        .with_client_cert_verifier(verifier)
        .with_cert_resolver(cert_resolver);
    disable_resumption_and_early_data(&mut config);
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(config)
}

fn disable_resumption_and_early_data(config: &mut ServerConfig) {
    config.session_storage = Arc::new(rustls::server::NoServerSessionStorage {});
    config.send_tls13_tickets = 0;
    config.max_early_data_size = 0;
}

pub fn load_server_config(dir: &Path, min_tls: &str) -> Result<Arc<ServerConfig>, String> {
    let cert = std::fs::read(dir.join("server.pem")).map_err(|e| format!("cannot read server.pem: {e}"))?;
    let key = std::fs::read(dir.join("server.key")).map_err(|e| format!("cannot read server.key: {e}"))?;
    Ok(Arc::new(build_config_with_min_tls(&cert, &key, min_tls)?))
}

/// Certificate source boundary. Future ACME provisioning can implement this interface without
/// changing listener startup; no ACME client or project-operated certificate service is included.
pub trait CertificateProvider: Send + Sync {
    fn load(&self, data_dir: &Path, min_tls: &str) -> Result<(Arc<ServerConfig>, Option<Arc<ReloadingFileCertResolver>>), String>;
}

/// The coordinator's local CA and its generated server certificate.
pub struct InternalCaProvider;

impl CertificateProvider for InternalCaProvider {
    fn load(&self, data_dir: &Path, min_tls: &str) -> Result<(Arc<ServerConfig>, Option<Arc<ReloadingFileCertResolver>>), String> {
        Ok((load_server_config(data_dir, min_tls)?, None))
    }
}

/// Operator supplied certificate files, with the internal CA retained for other hostnames.
pub struct FileCertificateProvider {
    pub cert_path: PathBuf,
    pub key_path: PathBuf,
    pub exact_name: String,
}

impl CertificateProvider for FileCertificateProvider {
    fn load(&self, data_dir: &Path, min_tls: &str) -> Result<(Arc<ServerConfig>, Option<Arc<ReloadingFileCertResolver>>), String> {
        let (config, resolver) = load_reloading_file_server_config(self.cert_path.clone(), self.key_path.clone(), self.exact_name.clone(), data_dir, min_tls)?;
        Ok((config, Some(resolver)))
    }
}

/// TLS certificate resolver for operator-managed files. Only a complete,
/// key-matched pair replaces the active key; partial writes keep the last good one.
pub struct ReloadingFileCertResolver {
    cert_path: PathBuf,
    key_path: PathBuf,
    external_name: String,
    internal: Arc<rustls::sign::CertifiedKey>,
    external: std::sync::RwLock<Option<Arc<rustls::sign::CertifiedKey>>>,
    fingerprint: std::sync::RwLock<[u8; 32]>,
}

impl std::fmt::Debug for ReloadingFileCertResolver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReloadingFileCertResolver").finish_non_exhaustive()
    }
}

impl ReloadingFileCertResolver {
    pub fn load(cert_path: PathBuf, key_path: PathBuf, external_name: String, fallback_dir: &Path) -> Result<Arc<Self>, String> {
        let pair = read_tls_file(&cert_path).ok().zip(read_tls_file(&key_path).ok());
        let external_pair = pair.and_then(|(cert, key)| certified_key(&cert, &key).ok().map(|loaded| (fingerprint(&cert, &key), Arc::new(loaded))));
        let internal_cert = read_tls_file(&fallback_dir.join("server.pem")).map_err(|e| format!("cannot read fallback server certificate: {e}"))?;
        let internal_key = read_tls_file(&fallback_dir.join("server.key")).map_err(|e| format!("cannot read fallback server key: {e}"))?;
        let internal = Arc::new(certified_key(&internal_cert, &internal_key)?);
        let (fingerprint, external) =
            external_pair.map_or_else(|| (fingerprint(&internal_cert, &internal_key), None), |(digest, loaded)| (digest, Some(loaded)));
        Ok(Arc::new(Self {
            cert_path,
            key_path,
            external_name: external_name.to_ascii_lowercase(),
            internal,
            external: std::sync::RwLock::new(external),
            fingerprint: std::sync::RwLock::new(fingerprint),
        }))
    }

    /// Read both files on every poll. Atomic replacements are detected, while
    /// temporarily mismatched cert/key writes leave the active identity intact.
    pub fn reload_if_changed(&self) -> Result<bool, String> {
        let cert = read_tls_file(&self.cert_path).map_err(|e| format!("cannot read TLS certificate file: {e}"))?;
        let key = read_tls_file(&self.key_path).map_err(|e| format!("cannot read TLS key file: {e}"))?;
        let fingerprint = fingerprint(&cert, &key);
        if *self.fingerprint.read().map_err(|_| "TLS resolver lock poisoned")? == fingerprint {
            return Ok(false);
        }
        let next = Arc::new(certified_key(&cert, &key)?);
        *self.external.write().map_err(|_| "TLS resolver lock poisoned")? = Some(next);
        *self.fingerprint.write().map_err(|_| "TLS resolver lock poisoned")? = fingerprint;
        Ok(true)
    }
}

impl rustls::server::ResolvesServerCert for ReloadingFileCertResolver {
    fn resolve(&self, client_hello: rustls::server::ClientHello<'_>) -> Option<Arc<rustls::sign::CertifiedKey>> {
        let use_external = client_hello.server_name().is_some_and(|name| name.eq_ignore_ascii_case(&self.external_name));
        if use_external {
            if let Some(key) = self.external.read().ok()?.as_ref() {
                return Some(Arc::clone(key));
            }
        }
        Some(Arc::clone(&self.internal))
    }
}

fn certified_key(cert_pem: &[u8], key_pem: &[u8]) -> Result<rustls::sign::CertifiedKey, String> {
    let certs = rustls::pki_types::CertificateDer::pem_slice_iter(cert_pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    if certs.is_empty() {
        return Err("no certificate found in PEM".into());
    }
    let key = PrivateKeyDer::from_pem_slice(key_pem).map_err(|e| format!("no private key found in PEM: {e}"))?;
    rustls::sign::CertifiedKey::from_der(certs, key, &provider()).map_err(|e| e.to_string())
}

fn read_tls_file(path: &Path) -> Result<Vec<u8>, String> {
    let metadata = std::fs::metadata(path).map_err(|e| e.to_string())?;
    if metadata.len() > 1024 * 1024 {
        return Err("TLS certificate and key files must be at most 1 MiB".into());
    }
    std::fs::read(path).map_err(|e| e.to_string())
}

fn fingerprint(cert: &[u8], key: &[u8]) -> [u8; 32] {
    use sha2::Digest;
    let mut digest = sha2::Sha256::new();
    digest.update((cert.len() as u64).to_be_bytes());
    digest.update(cert);
    digest.update((key.len() as u64).to_be_bytes());
    digest.update(key);
    digest.finalize().into()
}

pub fn load_reloading_file_server_config(
    cert_path: PathBuf,
    key_path: PathBuf,
    external_name: String,
    fallback_dir: &Path,
    min_tls: &str,
) -> Result<(Arc<ServerConfig>, Arc<ReloadingFileCertResolver>), String> {
    let resolver = ReloadingFileCertResolver::load(cert_path, key_path, external_name, fallback_dir)?;
    let mut cfg = ServerConfig::builder_with_provider(provider())
        .with_protocol_versions(&tls_versions(min_tls)?)
        .map_err(|e| e.to_string())?
        .with_no_client_auth()
        .with_cert_resolver(resolver.clone());
    disable_resumption_and_early_data(&mut cfg);
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok((Arc::new(cfg), resolver))
}
