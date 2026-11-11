//! Discover the host LAN address and refresh the choices offered by the API.

use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

use crate::boundedcommand::run_bounded;
use crate::stopsignal::StopSignal;

/// Host addresses offered as advertised-host shortcuts.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct HostAddresses {
    pub hostname: String,
    pub lan_ip: String,
}

/// Refresh only when sharing the host network; a bridge exposes the container's address.
pub fn watch_lan_ip(addresses: Arc<Mutex<HostAddresses>>, stop: StopSignal) {
    refresh_loop(&addresses, &stop, Duration::from_secs(5), || {
        lan_ip(|argv| {
            let argv: Vec<String> = argv.iter().map(|part| part.to_string()).collect();
            match run_bounded(&argv, Duration::from_secs(1), Stdio::null()) {
                Ok(output) if output.status.success() => {
                    String::from_utf8_lossy(&output.stdout).trim().to_string()
                }
                _ => String::new(),
            }
        })
    });
}

fn refresh_loop(
    addresses: &Mutex<HostAddresses>,
    stop: &StopSignal,
    interval: Duration,
    mut discover: impl FnMut() -> String,
) {
    while !stop.is_set() {
        // Discovery can block; never hold the API's lock while it runs.
        let lan_ip = discover();
        {
            let mut addresses = addresses.lock().unwrap_or_else(|error| error.into_inner());
            if addresses.lan_ip != lan_ip {
                log::info!(
                    "LAN address changed from {:?} to {:?}",
                    addresses.lan_ip,
                    lan_ip
                );
                addresses.lan_ip = lan_ip;
            }
        }
        if stop.wait(interval) {
            break;
        }
    }
}

/// Use the IPv4 address of the lowest-metric default-route interface.
pub fn lan_ip(output: impl Fn(&[&str]) -> String) -> String {
    preferred_route_address(&output).unwrap_or_default()
}

fn preferred_route_address(output: &impl Fn(&[&str]) -> String) -> Option<String> {
    let routes = parse_json_array(&output(&["ip", "-j", "-4", "route", "show", "default"]))?;
    // Iterator::min_by keeps the first of equal metrics, as Python's min does.
    let route = routes
        .iter()
        .filter(|route| non_empty_text(route.get("dev")).is_some())
        .min_by(|first, second| route_metric(first).total_cmp(&route_metric(second)))?;
    if let Some(preferred_source) = non_empty_text(route.get("prefsrc")) {
        return Some(preferred_source.to_string());
    }
    let device = non_empty_text(route.get("dev"))?;
    let interfaces = parse_json_array(&output(&[
        "ip", "-j", "-4", "address", "show", "dev", device,
    ]))?;
    // Only the first global address counts, so one without a local field means no address.
    let global_address = interfaces
        .iter()
        .flat_map(|interface| {
            interface
                .get("addr_info")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
        })
        .find(|address| address.get("scope").and_then(Value::as_str) == Some("global"))?;
    global_address
        .get("local")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Parse `ip -j` output, where empty output means no entries and anything unparsable means failure.
fn parse_json_array(text: &str) -> Option<Vec<Value>> {
    if text.is_empty() {
        return Some(Vec::new());
    }
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Array(entries)) => Some(entries),
        _ => None,
    }
}

fn route_metric(route: &Value) -> f64 {
    route.get("metric").and_then(Value::as_f64).unwrap_or(0.0)
}

fn non_empty_text(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_source_is_used_without_an_address_lookup() {
        assert_eq!(
            lan_ip(|argv| {
                assert_eq!(argv, ["ip", "-j", "-4", "route", "show", "default"]);
                r#"[{"dev":"wifi","prefsrc":"172.31.50.72","metric":600}]"#.into()
            }),
            "172.31.50.72"
        );
    }

    #[test]
    fn unavailable_or_invalid_routes_clear_the_address() {
        for output in ["", "[]", "not json", "{}", r#"[{"metric":10}]"#] {
            assert_eq!(lan_ip(|_| output.into()), "");
        }
    }

    #[test]
    fn refresh_follows_network_changes_loss_and_recovery() {
        let addresses = Mutex::new(HostAddresses {
            hostname: "publisher".into(),
            lan_ip: "192.168.0.227".into(),
        });
        let stop = StopSignal::new();
        let mut expected_previous = "192.168.0.227";
        let mut results = ["172.31.50.72", "", "192.168.0.228"].into_iter().peekable();
        refresh_loop(&addresses, &stop, Duration::ZERO, || {
            // This also proves detection does not hold the API lock.
            assert_eq!(addresses.try_lock().unwrap().lan_ip, expected_previous);
            let next = results.next().unwrap();
            expected_previous = next;
            if results.peek().is_none() {
                stop.set();
            }
            next.into()
        });
        let final_addresses = addresses.lock().unwrap();
        assert_eq!(final_addresses.lan_ip, "192.168.0.228");
        assert_eq!(final_addresses.hostname, "publisher");
    }
}
