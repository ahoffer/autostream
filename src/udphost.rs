//! Shared DNS knowledge; status and request handlers never perform lookups.

use std::collections::{BTreeSet, HashMap};
use std::net::ToSocketAddrs;

/// The DNS boundary: refreshed by the supervisor, consulted before a push starts.
pub trait HostResolver: Send {
    fn refresh(&mut self, hosts: &BTreeSet<String>);
    fn start_blocker(&self, host: &str) -> Option<String>;
}

#[derive(Default)]
pub struct UdpHostResolver {
    // Keyed by host name because the cache answers "did this host resolve last time".
    resolved: HashMap<String, bool>,
}

impl UdpHostResolver {
    pub fn new() -> Self {
        Self::default()
    }
}

impl HostResolver for UdpHostResolver {
    fn refresh(&mut self, hosts: &BTreeSet<String>) {
        for host in hosts {
            let resolves = (host.as_str(), 0u16).to_socket_addrs().is_ok();
            self.resolved.insert(host.clone(), resolves);
        }
    }

    fn start_blocker(&self, host: &str) -> Option<String> {
        if self.resolved.get(host).copied().unwrap_or(false) {
            None
        } else {
            Some(format!("{host} DNS unresolved; waiting"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_hosts_block_until_refreshed() {
        let mut resolver = UdpHostResolver::new();
        assert_eq!(
            resolver.start_blocker("localhost"),
            Some("localhost DNS unresolved; waiting".into())
        );
        resolver.refresh(&BTreeSet::from([
            "localhost".to_string(),
            "127.0.0.1".to_string(),
        ]));
        assert_eq!(resolver.start_blocker("localhost"), None);
        assert_eq!(resolver.start_blocker("127.0.0.1"), None);
    }
}
