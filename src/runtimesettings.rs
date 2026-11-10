//! Operator-editable topology and policy, persisted in the state file; immutable in memory.

use std::net::IpAddr;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::queryform::QueryForm;

/// Administratively scoped, local to the site.
pub const DEFAULT_MULTICAST_GROUP: &str = "239.255.0.1";
/// A test tool's shared secret, readable as text: AES_CM_128_HMAC_SHA1_80 takes
/// a 16-byte key and 14-byte salt, so ffmpeg wants exactly 40 base64
/// characters, any 40.
pub const DEFAULT_SRTP_KEY: &str = "autostreamautostreamautostreamautostream";

/// The two settings that name a host; blank means unset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HostSetting {
    DestinationHost,
    AdvertisedHost,
}

impl HostSetting {
    pub const ALL: [HostSetting; 2] = [HostSetting::DestinationHost, HostSetting::AdvertisedHost];

    pub fn name(self) -> &'static str {
        match self {
            HostSetting::DestinationHost => "destination_host",
            HostSetting::AdvertisedHost => "advertised_host",
        }
    }
}

/// First port per transport kind; a new stream takes the lowest free port from there.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PortSetting {
    Udp,
    Rtp,
    SrtCall,
    Srtp,
    SrtListen,
    Tcp,
    Http,
}

impl PortSetting {
    pub const ALL: [PortSetting; 7] = [
        PortSetting::Udp,
        PortSetting::Rtp,
        PortSetting::SrtCall,
        PortSetting::Srtp,
        PortSetting::SrtListen,
        PortSetting::Tcp,
        PortSetting::Http,
    ];

    pub fn name(self) -> &'static str {
        match self {
            PortSetting::Udp => "udp_first_port",
            PortSetting::Rtp => "rtp_first_port",
            PortSetting::SrtCall => "srt_call_first_port",
            PortSetting::Srtp => "srtp_first_port",
            PortSetting::SrtListen => "srt_listen_first_port",
            PortSetting::Tcp => "tcp_first_port",
            PortSetting::Http => "http_first_port",
        }
    }
}

/// A setting a transport kind needs filled in before its streams can start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RequiredSetting {
    DestinationHost,
    MulticastGroup,
    SrtpKey,
}

impl RequiredSetting {
    pub fn name(self) -> &'static str {
        match self {
            RequiredSetting::DestinationHost => "destination_host",
            RequiredSetting::MulticastGroup => "multicast_group",
            RequiredSetting::SrtpKey => "srtp_key",
        }
    }
}

/// Bits per second for text such as '3M', '3000K' or '3000000'; empty text means no cap.
pub fn parse_bitrate(bitrate_text: &str) -> Result<Option<i64>, String> {
    if bitrate_text.is_empty() {
        return Ok(None);
    }
    let text = bitrate_text.trim().to_uppercase();
    let invalid = || format!("invalid bitrate {bitrate_text:?}");
    let scaled = |digits: &str, factor: f64| -> Result<Option<i64>, String> {
        let value: f64 = digits.parse().map_err(|_| invalid())?;
        if !value.is_finite() {
            return Err(invalid());
        }
        Ok(Some((value * factor).trunc() as i64))
    };
    if let Some(digits) = text.strip_suffix('M') {
        return scaled(digits, 1_000_000.0);
    }
    if let Some(digits) = text.strip_suffix('K') {
        return scaled(digits, 1_000.0);
    }
    text.parse().map(Some).map_err(|_| invalid())
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RuntimeSettings {
    /// Where push transports send.
    pub destination_host: Option<String>,
    /// The group the multicast transport sends to.
    pub multicast_group: String,
    /// Host in the URLs the consumer registers; None means the host the client used.
    pub advertised_host: Option<String>,
    /// Transcode cap such as '2M'; empty means no cap.
    pub max_video_bitrate: String,
    /// Base64 SRTP master key shared with the consumer.
    pub srtp_key: String,
    // Push kinds must land inside the range the consumer forwards into its container.
    pub udp_first_port: u16,
    pub rtp_first_port: u16,
    pub srt_call_first_port: u16,
    pub srtp_first_port: u16,
    pub srt_listen_first_port: u16,
    pub tcp_first_port: u16,
    pub http_first_port: u16,
}

impl Default for RuntimeSettings {
    fn default() -> Self {
        Self {
            destination_host: None,
            multicast_group: DEFAULT_MULTICAST_GROUP.to_string(),
            advertised_host: None,
            max_video_bitrate: String::new(),
            srtp_key: DEFAULT_SRTP_KEY.to_string(),
            udp_first_port: 40000,
            rtp_first_port: 40020,
            srt_call_first_port: 40040,
            srtp_first_port: 40060,
            srt_listen_first_port: 41000,
            tcp_first_port: 42000,
            http_first_port: 43000,
        }
    }
}

impl RuntimeSettings {
    pub fn max_video_bps(&self) -> Option<i64> {
        parse_bitrate(&self.max_video_bitrate).ok().flatten()
    }

    pub fn host(&self, setting: HostSetting) -> Option<&str> {
        match setting {
            HostSetting::DestinationHost => self.destination_host.as_deref(),
            HostSetting::AdvertisedHost => self.advertised_host.as_deref(),
        }
    }

    pub fn first_port(&self, setting: PortSetting) -> u16 {
        match setting {
            PortSetting::Udp => self.udp_first_port,
            PortSetting::Rtp => self.rtp_first_port,
            PortSetting::SrtCall => self.srt_call_first_port,
            PortSetting::Srtp => self.srtp_first_port,
            PortSetting::SrtListen => self.srt_listen_first_port,
            PortSetting::Tcp => self.tcp_first_port,
            PortSetting::Http => self.http_first_port,
        }
    }

    fn set_first_port(&mut self, setting: PortSetting, port: u16) {
        match setting {
            PortSetting::Udp => self.udp_first_port = port,
            PortSetting::Rtp => self.rtp_first_port = port,
            PortSetting::SrtCall => self.srt_call_first_port = port,
            PortSetting::Srtp => self.srtp_first_port = port,
            PortSetting::SrtListen => self.srt_listen_first_port = port,
            PortSetting::Tcp => self.tcp_first_port = port,
            PortSetting::Http => self.http_first_port = port,
        }
    }

    /// Whether a setting a transport needs has a value.
    pub fn satisfies(&self, need: RequiredSetting) -> bool {
        match need {
            RequiredSetting::DestinationHost => self
                .destination_host
                .as_deref()
                .is_some_and(|host| !host.is_empty()),
            RequiredSetting::MulticastGroup => !self.multicast_group.is_empty(),
            RequiredSetting::SrtpKey => !self.srtp_key.is_empty(),
        }
    }

    pub fn to_document(&self) -> Map<String, Value> {
        match serde_json::to_value(self) {
            Ok(Value::Object(document)) => document,
            _ => Map::new(),
        }
    }

    /// Settings saved in the state file; absent keys keep their defaults.
    pub fn from_document(document: &Map<String, Value>) -> Result<Self, String> {
        SettingsCandidate::from_document(document)
            .validated()
            .map_err(|error| format!("Invalid saved settings: {error}"))
    }

    /// Settings from a ?key=value form; a blank host or bitrate cap means unset,
    /// any other blank means the default.
    pub fn from_query(query: &QueryForm) -> Result<Self, String> {
        let text = |name: &str| query.first_or_blank(name).trim().to_string();
        let mut candidate = SettingsCandidate::default();
        for setting in HostSetting::ALL {
            let host = text(setting.name());
            candidate.set_host(setting, (!host.is_empty()).then_some(host));
        }
        candidate.max_video_bitrate = text("max_video_bitrate");
        let multicast_group = text("multicast_group");
        if !multicast_group.is_empty() {
            candidate.multicast_group = multicast_group;
        }
        let srtp_key = text("srtp_key");
        if !srtp_key.is_empty() {
            candidate.srtp_key = srtp_key;
        }
        for (index, setting) in PortSetting::ALL.into_iter().enumerate() {
            let port_text = text(setting.name());
            if port_text.is_empty() {
                continue;
            }
            candidate.ports[index] = port_text
                .parse()
                .map_err(|_| format!("{} must be a whole number", setting.name()))?;
        }
        candidate.validated()
    }
}

/// The one rule for a valid settings value: a message, or None when valid.
pub fn validation_error(settings: &RuntimeSettings) -> Option<String> {
    SettingsCandidate::from_settings(settings).validation_error()
}

/// Settings before validation: ports may still be out of range here.
#[derive(Clone, Debug)]
struct SettingsCandidate {
    destination_host: Option<String>,
    advertised_host: Option<String>,
    multicast_group: String,
    max_video_bitrate: String,
    /// False when a saved document held something other than text for the cap.
    bitrate_is_text: bool,
    srtp_key: String,
    ports: [i64; 7],
}

impl Default for SettingsCandidate {
    fn default() -> Self {
        Self::from_settings(&RuntimeSettings::default())
    }
}

impl SettingsCandidate {
    fn from_settings(settings: &RuntimeSettings) -> Self {
        Self {
            destination_host: settings.destination_host.clone(),
            advertised_host: settings.advertised_host.clone(),
            multicast_group: settings.multicast_group.clone(),
            max_video_bitrate: settings.max_video_bitrate.clone(),
            bitrate_is_text: true,
            srtp_key: settings.srtp_key.clone(),
            ports: PortSetting::ALL.map(|setting| i64::from(settings.first_port(setting))),
        }
    }

    fn set_host(&mut self, setting: HostSetting, host: Option<String>) {
        match setting {
            HostSetting::DestinationHost => self.destination_host = host,
            HostSetting::AdvertisedHost => self.advertised_host = host,
        }
    }

    fn host(&self, setting: HostSetting) -> Option<&str> {
        match setting {
            HostSetting::DestinationHost => self.destination_host.as_deref(),
            HostSetting::AdvertisedHost => self.advertised_host.as_deref(),
        }
    }

    /// Read a saved document; a value of the wrong type fails the same check,
    /// at the same point in the order, that a wrong value of the right type would.
    fn from_document(document: &Map<String, Value>) -> Self {
        let mut candidate = Self::default();
        for setting in HostSetting::ALL {
            match document.get(setting.name()) {
                None | Some(Value::Null) => candidate.set_host(setting, None),
                Some(Value::String(host)) => candidate.set_host(setting, Some(host.clone())),
                Some(_) => candidate.set_host(setting, Some(" ".to_string())),
            }
        }
        match document.get("multicast_group") {
            None => {}
            Some(Value::String(group)) => candidate.multicast_group = group.clone(),
            Some(_) => candidate.multicast_group = String::new(),
        }
        for (index, setting) in PortSetting::ALL.into_iter().enumerate() {
            match document.get(setting.name()) {
                None => {}
                Some(Value::Number(number)) if number.is_i64() || number.is_u64() => {
                    candidate.ports[index] = number.as_i64().unwrap_or(i64::MAX);
                }
                Some(_) => candidate.ports[index] = 0,
            }
        }
        match document.get("max_video_bitrate") {
            None => {}
            Some(Value::String(text)) => candidate.max_video_bitrate = text.clone(),
            Some(_) => candidate.bitrate_is_text = false,
        }
        match document.get("srtp_key") {
            None => {}
            Some(Value::String(key)) => candidate.srtp_key = key.clone(),
            Some(_) => candidate.srtp_key = String::new(),
        }
        candidate
    }

    fn validation_error(&self) -> Option<String> {
        for setting in HostSetting::ALL {
            if let Some(host) = self.host(setting)
                && !is_host_text(host)
            {
                return Some(host_message(setting));
            }
        }
        let is_multicast = self
            .multicast_group
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_multicast());
        if !is_multicast {
            return Some(MULTICAST_MESSAGE.to_string());
        }
        for (index, setting) in PortSetting::ALL.into_iter().enumerate() {
            if !(1..=65535).contains(&self.ports[index]) {
                return Some(port_message(setting));
            }
        }
        if !self.bitrate_is_text {
            return Some("max_video_bitrate must be text such as 2M".to_string());
        }
        if !self.max_video_bitrate.is_empty() {
            match parse_bitrate(&self.max_video_bitrate) {
                Err(_) => {
                    return Some("max_video_bitrate must be like 2M, 3000K or 3000000".to_string());
                }
                Ok(Some(bits_per_second)) if bits_per_second <= 0 => {
                    return Some("max_video_bitrate must be positive".to_string());
                }
                Ok(_) => {}
            }
        }
        if self.srtp_key.is_empty() || self.srtp_key.chars().any(char::is_whitespace) {
            return Some(SRTP_KEY_MESSAGE.to_string());
        }
        None
    }

    fn validated(self) -> Result<RuntimeSettings, String> {
        if let Some(error) = self.validation_error() {
            return Err(error);
        }
        let mut settings = RuntimeSettings {
            destination_host: self.destination_host,
            multicast_group: self.multicast_group,
            advertised_host: self.advertised_host,
            max_video_bitrate: self.max_video_bitrate,
            srtp_key: self.srtp_key,
            ..RuntimeSettings::default()
        };
        for (index, setting) in PortSetting::ALL.into_iter().enumerate() {
            settings.set_first_port(setting, self.ports[index] as u16);
        }
        Ok(settings)
    }
}

const MULTICAST_MESSAGE: &str = "multicast_group must be a multicast address such as 239.1.1.2";
const SRTP_KEY_MESSAGE: &str = "srtp_key must be the base64 key text with no spaces";

fn host_message(setting: HostSetting) -> String {
    format!("{} must be a host name or address", setting.name())
}

fn port_message(setting: PortSetting) -> String {
    format!("{} must be a whole number from 1 to 65535", setting.name())
}

fn is_host_text(host: &str) -> bool {
    !host.is_empty() && !host.chars().any(char::is_whitespace) && !host.contains('/')
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testsupport::fixture;

    #[test]
    fn defaults_carry_first_ports_a_group_and_a_key_but_no_hosts() {
        let settings = RuntimeSettings::default();
        assert_eq!(settings.destination_host, None);
        assert_eq!(settings.advertised_host, None);
        assert_eq!(settings.max_video_bitrate, "");
        assert_eq!(settings.multicast_group, "239.255.0.1");
        assert_eq!(settings.srtp_key.len(), 40);
        assert_eq!(
            (settings.udp_first_port, settings.srt_listen_first_port),
            (40000, 41000)
        );
        assert_eq!(settings.max_video_bps(), None);
        assert_eq!(validation_error(&settings), None);
        assert_eq!(
            Value::Object(settings.to_document()),
            fixture("settings.json")["defaults"]
        );
    }

    #[test]
    fn document_round_trip_and_absent_keys_take_defaults() {
        assert_eq!(
            RuntimeSettings::from_document(&Map::new()).unwrap(),
            RuntimeSettings::default()
        );
        let settings = RuntimeSettings {
            destination_host: Some("receiver".into()),
            multicast_group: "239.1.1.2".into(),
            advertised_host: Some("publisher".into()),
            max_video_bitrate: "2M".into(),
            srtp_key: "a2V5".into(),
            tcp_first_port: 45000,
            ..RuntimeSettings::default()
        };
        assert_eq!(
            RuntimeSettings::from_document(&settings.to_document()).unwrap(),
            settings
        );
        assert_eq!(settings.max_video_bps(), Some(2_000_000));
    }

    #[test]
    fn query_and_document_parsing_match_the_python_fixture() {
        let fixture = fixture("settings.json");
        for case in fixture["from_query"].as_array().unwrap() {
            let query = QueryForm::from_pairs(case["query"].as_object().unwrap().iter().flat_map(
                |(key, values)| {
                    values
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(move |value| (key.as_str(), value.as_str().unwrap()))
                },
            ));
            let outcome = RuntimeSettings::from_query(&query);
            let expected_error = case["error"].as_str();
            match (&outcome, expected_error) {
                (Ok(settings), None) => {
                    assert_eq!(
                        Value::Object(settings.to_document()),
                        case["settings"],
                        "{case}"
                    )
                }
                (Err(error), Some(expected)) => assert_eq!(error, expected, "{case}"),
                _ => panic!("query {case} gave {outcome:?}"),
            }
        }
        for case in fixture["from_document"].as_array().unwrap() {
            let document = case["document"].as_object().unwrap();
            let outcome = RuntimeSettings::from_document(document);
            match (&outcome, case["error"].as_str()) {
                (Ok(settings), None) => {
                    assert_eq!(
                        Value::Object(settings.to_document()),
                        case["settings"],
                        "{case}"
                    );
                    assert_eq!(
                        serde_json::to_value(settings.max_video_bps()).unwrap(),
                        case["max_video_bps"]
                    );
                }
                (Err(error), Some(expected)) => assert_eq!(error, expected, "{case}"),
                _ => panic!("document {case} gave {outcome:?}"),
            }
        }
        for case in fixture["parse_bitrate"].as_array().unwrap() {
            let outcome = parse_bitrate(case["text"].as_str().unwrap());
            match (outcome, case["error"].is_null()) {
                (Ok(value), true) => assert_eq!(
                    serde_json::to_value(value).unwrap(),
                    case["value"],
                    "{case}"
                ),
                (Err(_), false) => {}
                (outcome, _) => panic!("bitrate {case} gave {outcome:?}"),
            }
        }
    }

    #[test]
    fn needs_follow_blank_hosts_and_keys() {
        let settings = RuntimeSettings::default();
        assert!(!settings.satisfies(RequiredSetting::DestinationHost));
        assert!(settings.satisfies(RequiredSetting::MulticastGroup));
        assert!(settings.satisfies(RequiredSetting::SrtpKey));
        let with_host = RuntimeSettings {
            destination_host: Some("receiver".into()),
            ..RuntimeSettings::default()
        };
        assert!(with_host.satisfies(RequiredSetting::DestinationHost));
    }
}
