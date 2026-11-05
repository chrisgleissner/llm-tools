//! Discovery of the names and addresses a freshly initialised certificate should cover (NET-009).
//!
//! Only the standard library and `/proc` are used:
//! - hostname: `/proc/sys/kernel/hostname`, then `/etc/hostname`, then `$HOSTNAME`;
//! - IPv4 addresses: the `LOCAL` entries of `/proc/net/fib_trie`, plus the source address the
//!   kernel would pick for a private destination (`UdpSocket::connect` only selects a route; no
//!   packet is sent);
//! - IPv6 addresses: global-scope entries of `/proc/net/if_inet6`.
//!
//! Loopback and link-local addresses are skipped (loopback is always in the certificate; a
//! link-local address needs a zone id and cannot be used in a URL reliably).

use crate::certs::Sans;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, UdpSocket};

/// Keep only characters that are valid in a DNS name; the certificate builder rejects the rest.
fn clean_hostname(raw: &str) -> Option<String> {
    let h = raw.trim().trim_end_matches('.').to_ascii_lowercase();
    let ok = !h.is_empty() && h.len() <= 253 && h.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '.');
    (ok && h != "localhost" && !h.starts_with('.') && !h.starts_with('-')).then_some(h)
}

pub fn machine_hostname() -> Option<String> {
    ["/proc/sys/kernel/hostname", "/etc/hostname"]
        .iter()
        .find_map(|p| std::fs::read_to_string(p).ok().and_then(|t| clean_hostname(&t)))
        .or_else(|| std::env::var("HOSTNAME").ok().and_then(|t| clean_hostname(&t)))
}

/// `<host>` and `<host>.home.arpa` (RFC 8375 home network name).
pub fn names_for_host(host: &str) -> Vec<String> {
    let mut v = vec![host.to_string()];
    if !host.contains('.') {
        v.push(format!("{host}.home.arpa"));
    }
    v
}

pub fn parse_fib_trie(text: &str) -> Vec<Ipv4Addr> {
    let mut out = vec![];
    let mut last: Option<Ipv4Addr> = None;
    for line in text.lines() {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("|-- ").or_else(|| t.strip_prefix("+-- ")) {
            last = rest.split_whitespace().next().and_then(|s| s.parse().ok());
        } else if t.contains("/32 host LOCAL") {
            if let Some(ip) = last {
                out.push(ip);
            }
        }
    }
    out
}

pub fn parse_if_inet6(text: &str) -> Vec<Ipv6Addr> {
    text.lines()
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            // fields: address, ifindex, prefix len, scope, flags, ifname; scope 0x00 = global
            if f.len() < 6 || f[3] != "00" || f[0].len() != 32 {
                return None;
            }
            let bytes = hex::decode(f[0]).ok()?;
            let arr: [u8; 16] = bytes.try_into().ok()?;
            Some(Ipv6Addr::from(arr))
        })
        .collect()
}

fn routed_source(dest: &str, bind: &str) -> Option<IpAddr> {
    let s = UdpSocket::bind(bind).ok()?;
    s.connect(dest).ok()?;
    s.local_addr().ok().map(|a| a.ip())
}

fn usable(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => !a.is_loopback() && !a.is_link_local() && !a.is_unspecified(),
        IpAddr::V6(a) => !a.is_loopback() && !a.is_unspecified() && (a.segments()[0] & 0xffc0) != 0xfe80,
    }
}

pub fn local_ips() -> Vec<IpAddr> {
    let mut ips: Vec<IpAddr> = vec![];
    if let Ok(t) = std::fs::read_to_string("/proc/net/fib_trie") {
        ips.extend(parse_fib_trie(&t).into_iter().map(IpAddr::V4));
    }
    if let Ok(t) = std::fs::read_to_string("/proc/net/if_inet6") {
        ips.extend(parse_if_inet6(&t).into_iter().map(IpAddr::V6));
    }
    for dest in ["192.168.255.255:9", "10.255.255.255:9"] {
        ips.extend(routed_source(dest, "0.0.0.0:0"));
    }
    ips.extend(routed_source("[fd00::1]:9", "[::]:0"));
    ips.retain(usable);
    ips.sort();
    ips.dedup();
    ips
}

/// Names and addresses of this machine for the default certificate.
pub fn discovered_sans() -> Sans {
    Sans {
        hostnames: machine_hostname().map(|h| names_for_host(&h)).unwrap_or_default(),
        ips: local_ips(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fib_trie_local_addresses() {
        let text = "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n     +-- 127.0.0.0/8 2 0 2\n        |-- 127.0.0.1\n           /32 host LOCAL\n     +-- 192.168.1.0/24 2 0 2\n        |-- 192.168.1.0\n           /32 link BROADCAST\n        |-- 192.168.1.20\n           /32 host LOCAL\n";
        assert_eq!(parse_fib_trie(text), vec![Ipv4Addr::new(127, 0, 0, 1), Ipv4Addr::new(192, 168, 1, 20)]);
    }

    #[test]
    fn inet6_global_only() {
        let text = "fd000000000000000000000000000001 02 40 00 80 eth0\nfe800000000000000000000000000001 02 40 20 80 eth0\n00000000000000000000000000000001 01 80 10 80 lo\n";
        assert_eq!(parse_if_inet6(text), vec!["fd00::1".parse::<Ipv6Addr>().unwrap()]);
    }

    #[test]
    fn hostname_names() {
        assert_eq!(clean_hostname("Mickey\n").as_deref(), Some("mickey"));
        assert_eq!(clean_hostname("bad name"), None);
        assert_eq!(clean_hostname("localhost"), None);
        assert_eq!(names_for_host("pi"), vec!["pi", "pi.home.arpa"]);
        assert_eq!(names_for_host("pi.lan"), vec!["pi.lan"]);
    }
}
