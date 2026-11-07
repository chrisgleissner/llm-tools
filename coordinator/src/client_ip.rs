//! Client address selection. Forwarding headers are honored only when the TCP peer is
//! explicitly trusted, and the chain is walked from the nearest proxy toward the client.

use std::net::IpAddr;

const MAX_PROXY_CHAIN_BYTES: usize = 2048;
const MAX_PROXY_HOPS: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Network {
    V4(u32, u8),
    V6(u128, u8),
}

fn parse_network(value: &str) -> Option<Network> {
    let (address, prefix) = value.split_once('/')?;
    let address = address.parse::<IpAddr>().ok()?;
    let prefix = prefix.parse::<u8>().ok()?;
    match address {
        IpAddr::V4(address) if prefix <= 32 => Some(Network::V4(u32::from(address), prefix)),
        IpAddr::V6(address) if prefix <= 128 => Some(Network::V6(u128::from(address), prefix)),
        _ => None,
    }
}

pub fn valid_cidr(value: &str) -> bool {
    parse_network(value).is_some()
}

fn contains(network: Network, address: IpAddr) -> bool {
    match (network, address) {
        (Network::V4(base, prefix), IpAddr::V4(address)) => {
            let mask = if prefix == 0 { 0 } else { u32::MAX << (32 - prefix) };
            base & mask == u32::from(address) & mask
        }
        (Network::V6(base, prefix), IpAddr::V6(address)) => {
            let mask = if prefix == 0 { 0 } else { u128::MAX << (128 - prefix) };
            base & mask == u128::from(address) & mask
        }
        _ => false,
    }
}

fn trusted(address: IpAddr, networks: &[String]) -> bool {
    networks
        .iter()
        .filter_map(|network| parse_network(network))
        .any(|network| contains(network, address))
}

fn parse_node(value: &str) -> Option<IpAddr> {
    let value = value.trim().trim_matches('"');
    if let Ok(address) = value.parse() {
        return Some(address);
    }
    if let Some(rest) = value.strip_prefix('[') {
        let (address, port) = rest.split_once("]:")?;
        port.parse::<u16>().ok()?;
        return address.parse().ok();
    }
    if let Some((address, port)) = value.rsplit_once(':') {
        if address.contains('.') {
            port.parse::<u16>().ok()?;
            return address.parse().ok();
        }
    }
    None
}

fn parse_xff(value: &str) -> Option<Vec<IpAddr>> {
    if value.len() > MAX_PROXY_CHAIN_BYTES {
        return None;
    }
    let mut addresses = Vec::new();
    for node in value.split(',') {
        if addresses.len() == MAX_PROXY_HOPS {
            return None;
        }
        addresses.push(parse_node(node)?);
    }
    (!addresses.is_empty()).then_some(addresses)
}

fn parse_forwarded(value: &str) -> Option<Vec<IpAddr>> {
    if value.len() > MAX_PROXY_CHAIN_BYTES {
        return None;
    }
    let mut addresses = Vec::new();
    for element in value.split(',') {
        if addresses.len() == MAX_PROXY_HOPS {
            return None;
        }
        let mut node = None;
        for parameter in element.split(';') {
            let (key, value) = parameter.trim().split_once('=')?;
            if key.trim().eq_ignore_ascii_case("for") {
                if node.is_some() {
                    return None;
                }
                node = Some(parse_node(value.trim())?);
            }
        }
        addresses.push(node?);
    }
    (!addresses.is_empty()).then_some(addresses)
}

fn walk_chain(peer: IpAddr, chain: &[IpAddr], networks: &[String]) -> IpAddr {
    let mut current = peer;
    for hop in chain.iter().rev() {
        if !trusted(current, networks) {
            break;
        }
        current = *hop;
    }
    current
}

/// Resolve the address used for limits and audit. X-Forwarded-For is preferred when present;
/// Forwarded is used only when X-Forwarded-For is absent. Any malformed chain falls back to
/// the actual peer address. Both formats are ignored unless that peer is in `trusted_proxies`.
pub fn resolve(peer: IpAddr, forwarded: Option<&str>, x_forwarded_for: Option<&str>, trusted_proxies: &[String]) -> IpAddr {
    if !trusted(peer, trusted_proxies) {
        return peer;
    }
    let chain = match x_forwarded_for {
        Some(value) => parse_xff(value),
        None => forwarded.and_then(parse_forwarded),
    };
    chain.map_or(peer, |chain| walk_chain(peer, &chain, trusted_proxies))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(value: &str) -> IpAddr {
        value.parse().unwrap()
    }

    #[test]
    fn untrusted_peers_cannot_spoof_forwarding_headers() {
        let peer = ip("198.51.100.8");
        let trusted = vec!["10.0.0.0/8".into()];
        assert_eq!(resolve(peer, Some("for=192.0.2.1"), Some("192.0.2.1"), &trusted), peer);
    }

    #[test]
    fn trusted_xff_chain_is_walked_right_to_left_and_ignores_forged_prefix() {
        let peer = ip("10.0.0.3");
        let trusted = vec!["10.0.0.0/8".into()];
        assert_eq!(resolve(peer, None, Some("192.0.2.99, 198.51.100.7, 10.0.0.2"), &trusted), ip("198.51.100.7"));
    }

    #[test]
    fn forwarded_header_supports_ipv6_and_proxy_ports() {
        let peer = ip("10.0.0.3");
        let trusted = vec!["10.0.0.0/8".into()];
        assert_eq!(
            resolve(peer, Some("for=\"[2001:db8::7]:443\";proto=https, for=10.0.0.2"), None, &trusted),
            ip("2001:db8::7")
        );
    }

    #[test]
    fn invalid_or_unbounded_proxy_chains_fall_back_to_the_peer() {
        let peer = ip("10.0.0.3");
        let trusted = vec!["10.0.0.0/8".into()];
        assert_eq!(resolve(peer, None, Some("unknown"), &trusted), peer);
        assert_eq!(resolve(peer, None, Some(&"192.0.2.1,".repeat(MAX_PROXY_HOPS)), &trusted), peer);
        assert_eq!(resolve(peer, None, Some(&"x".repeat(MAX_PROXY_CHAIN_BYTES + 1)), &trusted), peer);
    }

    #[test]
    fn cidr_validation_checks_prefix_and_family() {
        assert!(valid_cidr("10.0.0.0/8"));
        assert!(valid_cidr("2001:db8::/32"));
        assert!(!valid_cidr("10.0.0.0/33"));
        assert!(!valid_cidr("2001:db8::/129"));
        assert!(!valid_cidr("10.0.0.1"));
    }
}
