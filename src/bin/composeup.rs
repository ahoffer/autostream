//! Pass Docker host addresses to Compose for the advertised-host shortcuts.

use std::collections::HashMap;
use std::process::{Command, Stdio};
use std::time::Duration;

use autostream::boundedcommand::run_bounded;
use serde_json::Value;

/// How long each discovery command may run before it is killed.
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(10);

/// The environment handed to Compose, keyed by variable name.
///
/// A map, because Compose receives every inherited variable unchanged and this
/// helper only fills in a few named entries.
type Environment = HashMap<String, String>;

/// Runs a discovery command and returns its trimmed standard output.
trait CommandRunner {
    /// Return the trimmed output, or an empty string when the command fails in any way.
    fn output(&self, argv: &[&str]) -> String;
}

/// Runs discovery commands as real child processes with a deadline.
struct BoundedRunner;

impl CommandRunner for BoundedRunner {
    fn output(&self, argv: &[&str]) -> String {
        let owned_argv: Vec<String> = argv.iter().map(|part| part.to_string()).collect();
        match run_bounded(&owned_argv, DISCOVERY_TIMEOUT, Stdio::null()) {
            Ok(finished) if finished.status.success() => {
                String::from_utf8_lossy(&finished.stdout).trim().to_string()
            }
            _ => String::new(),
        }
    }
}

/// Use the IPv4 address of the lowest-metric default-route interface.
fn lan_ip(runner: &impl CommandRunner) -> String {
    preferred_route_address(runner).unwrap_or_default()
}

fn preferred_route_address(runner: &impl CommandRunner) -> Option<String> {
    let routes = parse_json_array(&runner.output(&["ip", "-j", "-4", "route", "show", "default"]))?;
    // Iterator::min_by keeps the first of equal metrics, as Python's min does.
    let route = routes
        .iter()
        .filter(|route| non_empty_text(route.get("dev")).is_some())
        .min_by(|first, second| route_metric(first).total_cmp(&route_metric(second)))?;
    if let Some(preferred_source) = non_empty_text(route.get("prefsrc")) {
        return Some(preferred_source.to_string());
    }
    let device = non_empty_text(route.get("dev"))?;
    let interfaces =
        parse_json_array(&runner.output(&["ip", "-j", "-4", "address", "show", "dev", device]))?;
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

fn is_set(environment: &Environment, name: &str) -> bool {
    environment.get(name).is_some_and(|value| !value.is_empty())
}

/// Fill in the advertised hostname and LAN address unless the caller set them.
fn host_environment(
    environ: Environment,
    runner: &impl CommandRunner,
    lan_ip: impl FnOnce() -> String,
) -> Environment {
    let mut environment = environ;
    if !is_set(&environment, "AUTOSTREAM_HOSTNAME") {
        let daemon_name = runner.output(&["docker", "info", "--format", "{{.Name}}"]);
        environment.insert("AUTOSTREAM_HOSTNAME".to_string(), daemon_name);
    }
    // A Docker context takes precedence over DOCKER_HOST, so ask Docker for the endpoint then.
    let mut endpoint = if is_set(&environment, "DOCKER_CONTEXT") {
        String::new()
    } else {
        environment.get("DOCKER_HOST").cloned().unwrap_or_default()
    };
    if endpoint.is_empty() {
        endpoint = runner.output(&[
            "docker",
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ]);
    }
    if !is_set(&environment, "AUTOSTREAM_LAN_IP") {
        // Never advertise this client's LAN address for a remote Docker daemon.
        let address = if endpoint.starts_with("unix://") {
            lan_ip()
        } else {
            String::new()
        };
        environment.insert("AUTOSTREAM_LAN_IP".to_string(), address);
    }
    environment
}

fn main() {
    let runner = BoundedRunner;
    let mut environment = host_environment(std::env::vars().collect(), &runner, || lan_ip(&runner));
    // Compose tags the image with VERSION, so default it to the version in Cargo.toml.
    if !is_set(&environment, "VERSION") {
        environment.insert("VERSION".to_string(), env!("CARGO_PKG_VERSION").to_string());
    }
    let compose_status = Command::new("docker")
        .args(["compose", "up", "-d"])
        .env_clear()
        .envs(&environment)
        .status();
    let exit_code = match compose_status {
        Ok(status) => status.code().unwrap_or(1),
        Err(error) => {
            eprintln!("composeup: cannot run docker compose: {error}");
            1
        }
    };
    std::process::exit(exit_code);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;
    use std::collections::VecDeque;

    /// Returns scripted outputs in order, then a fallback, and records every argv.
    struct ScriptedRunner {
        scripted_outputs: RefCell<VecDeque<String>>,
        fallback_output: String,
        calls: RefCell<Vec<Vec<String>>>,
    }

    impl ScriptedRunner {
        fn in_order(outputs: &[String]) -> Self {
            ScriptedRunner {
                scripted_outputs: RefCell::new(outputs.iter().cloned().collect()),
                fallback_output: String::new(),
                calls: RefCell::new(Vec::new()),
            }
        }

        fn always(output: &str) -> Self {
            ScriptedRunner {
                scripted_outputs: RefCell::new(VecDeque::new()),
                fallback_output: output.to_string(),
                calls: RefCell::new(Vec::new()),
            }
        }

        fn last_call(&self) -> Vec<String> {
            self.calls.borrow().last().cloned().unwrap_or_default()
        }
    }

    impl CommandRunner for ScriptedRunner {
        fn output(&self, argv: &[&str]) -> String {
            self.calls
                .borrow_mut()
                .push(argv.iter().map(|part| part.to_string()).collect());
            self.scripted_outputs
                .borrow_mut()
                .pop_front()
                .unwrap_or_else(|| self.fallback_output.clone())
        }
    }

    fn environment_of(entries: &[(&str, &str)]) -> Environment {
        entries
            .iter()
            .map(|(name, value)| (name.to_string(), value.to_string()))
            .collect()
    }

    fn lan_ip_must_not_run() -> String {
        panic!("lan_ip should not be called");
    }

    #[test]
    fn lan_address_comes_from_preferred_default_route() {
        let routes = json!([{"dev": "wifi", "metric": 600}, {"dev": "ethernet", "metric": 100}]);
        let addresses = json!([{"addr_info": [{"scope": "global", "local": "192.168.1.20"}]}]);
        let runner = ScriptedRunner::in_order(&[routes.to_string(), addresses.to_string()]);
        assert_eq!(lan_ip(&runner), "192.168.1.20");
        assert_eq!(
            runner.last_call(),
            ["ip", "-j", "-4", "address", "show", "dev", "ethernet"]
        );
    }

    #[test]
    fn missing_route_leaves_address_unavailable() {
        let runner = ScriptedRunner::always("");
        assert_eq!(lan_ip(&runner), "");
    }

    #[test]
    fn remote_docker_never_uses_client_lan_address() {
        let runner = ScriptedRunner::always("remote-host");
        let environment = host_environment(
            environment_of(&[("DOCKER_HOST", "ssh://remote-host")]),
            &runner,
            lan_ip_must_not_run,
        );
        assert_eq!(environment["AUTOSTREAM_HOSTNAME"], "remote-host");
        assert_eq!(environment["AUTOSTREAM_LAN_IP"], "");
    }

    #[test]
    fn explicit_addresses_are_preserved() {
        let runner = ScriptedRunner::always("unix:///var/run/docker.sock");
        let environment = host_environment(
            environment_of(&[
                ("AUTOSTREAM_HOSTNAME", "publisher"),
                ("AUTOSTREAM_LAN_IP", "192.168.1.20"),
            ]),
            &runner,
            lan_ip_must_not_run,
        );
        assert_eq!(environment["AUTOSTREAM_HOSTNAME"], "publisher");
        assert_eq!(environment["AUTOSTREAM_LAN_IP"], "192.168.1.20");
    }
}
