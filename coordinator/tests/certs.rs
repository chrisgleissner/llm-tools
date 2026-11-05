//! The local CA is name-constrained: it must not be usable to vouch for other host names.
mod common;
use bridge_coordinator::certs::{self, Sans, VerifyFailure};
use common::*;
use std::process::Command;

fn init_with(hostname: &str, ip: &str) -> std::path::PathBuf {
    let dir = tmp_dir();
    let sans = Sans {
        hostnames: vec![hostname.to_string()],
        ips: vec![ip.parse().unwrap()],
    };
    bridge_coordinator::init::init(&dir, &sans).unwrap();
    dir
}

fn issue(dir: &std::path::Path, name: &str) -> String {
    let key = std::fs::read_to_string(dir.join("ca.key")).unwrap();
    certs::issue_unchecked(&key, vec![name.to_string()]).unwrap().0
}

fn accepted(dir: &std::path::Path, name: &str) -> bool {
    let ca = std::fs::read(dir.join("ca.pem")).unwrap();
    match certs::verify_names(&ca, issue(dir, name).as_bytes(), &[name.to_string()]) {
        Ok(()) => true,
        Err(VerifyFailure::NameConstraint) => false,
        Err(VerifyFailure::Other(e)) => panic!("{name}: unexpected failure: {e}"),
    }
}

/// A certificate signed by the CA key for a name outside the constraints is rejected by the
/// path validation rustls clients use; names inside the constraints are accepted.
#[test]
fn certificate_for_a_foreign_name_is_rejected_by_a_verifier() {
    let dir = init_with("coord.example.test", "192.0.2.9");
    for inside in [
        "coord.example.test",
        "sub.coord.example.test",
        "localhost",
        "nas.home.arpa",
        "192.0.2.9",
        "127.0.0.1",
        "127.0.0.7",
        "::1",
    ] {
        assert!(accepted(&dir, inside), "{inside} should be accepted");
    }
    for outside in [
        "evil.example",
        "example.test",
        "bank.com",
        "192.0.2.10",
        "10.0.0.5",
        "::2",
        "home.arpa.evil.com",
    ] {
        assert!(!accepted(&dir, outside), "{outside} must be rejected");
    }
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn renew_refuses_a_name_outside_the_constraints_and_names_it() {
    let dir = init_with("coord.example.test", "192.0.2.9");
    let before = std::fs::read(dir.join("server.pem")).unwrap();
    let sans = Sans {
        hostnames: vec!["coord.example.test".into(), "other.example".into()],
        ips: vec!["192.0.2.9".parse().unwrap()],
    };
    let err = certs::renew(&dir, &sans).unwrap_err();
    assert!(err.contains("other.example") && err.contains("cert new-ca"), "{err}");
    assert!(!err.contains("coord.example.test"), "only the offending names are listed: {err}");
    assert_eq!(before, std::fs::read(dir.join("server.pem")).unwrap(), "nothing changed");
    // The same names within the constraints renew fine.
    let ok = Sans {
        hostnames: vec!["coord.example.test".into(), "nas.home.arpa".into()],
        ips: vec!["192.0.2.9".parse().unwrap()],
    };
    certs::renew(&dir, &ok).unwrap();
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn new_ca_replaces_the_ca_and_keeps_the_old_one() {
    let dir = init_with("coord.example.test", "192.0.2.9");
    let old = std::fs::read(dir.join("ca.pem")).unwrap();
    let sans = Sans {
        hostnames: vec!["other.example".into()],
        ips: vec![],
    };
    let stamp = certs::new_ca(&dir, &sans).unwrap();
    assert_eq!(old, std::fs::read(dir.join(format!("ca.pem.old-{stamp}"))).unwrap());
    assert!(dir.join(format!("ca.key.old-{stamp}")).exists());
    assert_ne!(old, std::fs::read(dir.join("ca.pem")).unwrap());
    assert!(accepted(&dir, "other.example"));
    assert!(!accepted(&dir, "coord.example.test"), "the new CA covers the new names only");
    // The server loads and the files are private.
    certs::load_server_config(&dir).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(dir.join("ca.key")).unwrap().permissions().mode() & 0o777, 0o600);
    }
    let _ = std::fs::remove_dir_all(dir);
}

/// The extensions as OpenSSL reads them: basicConstraints with pathlen 0 and a critical
/// nameConstraints extension with exactly the expected permitted subtrees; validity of at most 5 years.
#[test]
fn ca_extensions_as_openssl_sees_them() {
    // The text format below is OpenSSL 3's. Linux must have it; other platforms may ship LibreSSL
    // (macOS) or no command line tool (Windows) and skip this one cross-check. The certificate
    // properties are also asserted without OpenSSL in the other tests of this file.
    let version = Command::new("openssl").arg("version").output();
    let has_openssl = matches!(&version, Ok(o) if o.status.success() && String::from_utf8_lossy(&o.stdout).starts_with("OpenSSL "));
    if !has_openssl {
        if cfg!(target_os = "linux") {
            panic!("the openssl command line tool is needed for this test on Linux");
        }
        eprintln!("skipped: no OpenSSL command line tool on this platform");
        return;
    }
    let dir = init_with("coord.example.test", "192.0.2.9");
    let out = Command::new("openssl")
        .args(["x509", "-in"])
        .arg(dir.join("ca.pem"))
        .args(["-noout", "-text", "-enddate", "-startdate"])
        .output()
        .expect("the openssl command line tool is needed for this test");
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let text = String::from_utf8_lossy(&out.stdout).into_owned();
    assert!(
        text.contains("X509v3 Basic Constraints: critical\n") && text.contains("CA:TRUE, pathlen:0"),
        "{text}"
    );
    let nc = text
        .split("X509v3 Name Constraints: critical")
        .nth(1)
        .unwrap_or_else(|| panic!("name constraints missing or not critical:\n{text}"));
    let permitted: Vec<&str> = nc
        .lines()
        .skip(1)
        .take_while(|l| l.starts_with("                "))
        .map(str::trim)
        .filter(|l| l.starts_with("DNS:") || l.starts_with("IP:"))
        .collect();
    let mut expected = vec![
        "DNS:localhost",
        "DNS:home.arpa",
        "DNS:coord.example.test",
        "IP:127.0.0.0/255.0.0.0",
        "IP:0:0:0:0:0:0:0:1/FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF",
        "IP:192.0.2.9/255.255.255.255",
    ];
    let mut got = permitted.clone();
    expected.sort();
    got.sort();
    assert_eq!(got, expected, "{nc}");
    assert!(!nc.contains("Excluded"), "{nc}");
    // Not more than five years (1826 days allows the day of backdating).
    let days = |flag: &str| -> i64 {
        let o = Command::new("openssl")
            .args(["x509", "-in"])
            .arg(dir.join("ca.pem"))
            .args(["-noout", flag])
            .output()
            .unwrap();
        let s = String::from_utf8_lossy(&o.stdout).trim().split('=').nth(1).unwrap().to_string();
        days_since_epoch(&s)
    };
    let validity = days("-enddate") - days("-startdate");
    assert!((1000..=1826).contains(&validity), "validity {validity} days");
    assert!(certs::ca_is_constrained(&dir));
    let _ = std::fs::remove_dir_all(dir);
}

/// Whole days since 1970-01-01 for an OpenSSL date such as `Nov  5 22:01:59 2031 GMT`.
fn days_since_epoch(text: &str) -> i64 {
    let parts: Vec<&str> = text.split_whitespace().collect();
    let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let month = months.iter().position(|m| *m == parts[0]).unwrap() as i64 + 1;
    let day: i64 = parts[1].parse().unwrap();
    let year: i64 = parts[3].parse().unwrap();
    // Days from civil (Howard Hinnant's algorithm).
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}
