//! Describe detached stream snapshots for the web UI and API, with URLs composed per request.

use serde::Serialize;

use crate::runtimesettings::RuntimeSettings;
use crate::streamregistry::is_play_stream;
use crate::transports::{Direction, KINDS, TemplateFields, kind_by_id};

/// The ffmpeg lifecycle word the UI shows for a stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum StreamState {
    Running,
    Crashing,
    Restarting,
    Starting,
    Waiting,
    Failed,
    Stopped,
    Stopping,
    Completed,
}

/// What the last preparation learned about the source; absent until a command was prepared.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct PreparedFacts {
    pub video_codecs: Vec<String>,
    pub audio_codecs: Vec<String>,
    pub data_codecs: Vec<String>,
    pub effective_mode: &'static str,
    pub encoder: &'static str,
    pub passthrough_eligible: Option<bool>,
    pub ineligible_codecs: Vec<String>,
    pub source_frame_rate: Option<f64>,
}

/// One detached status row, as published by the supervisor.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct StreamStatus {
    #[serde(flatten)]
    pub facts: Option<PreparedFacts>,
    pub mode: &'static str,
    pub frame_rate: Option<f64>,
    pub frame_rate_explicit: bool,
    pub gop_frames: i64,
    pub gop_explicit: bool,
    pub enabled: bool,
    pub loop_count: i64,
    pub filename: String,
    pub transport: String,
    pub port: Option<u16>,
    pub name: String,
    pub state: StreamState,
    pub reason: Option<String>,
    pub running: bool,
    pub stopping: bool,
    pub command: Option<String>,
    pub error: Option<String>,
    pub delivery: Option<String>,
}

/// What the supervisor last published: detached status rows, the settings in force, the videos on offer.
#[derive(Clone, Debug, PartialEq)]
pub struct StatusSnapshot {
    pub statuses: Vec<StreamStatus>,
    pub settings: RuntimeSettings,
    pub videos: Vec<String>,
}

/// Start-up facts needed to advertise streams; the host is chosen per request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct UrlFacts {
    pub rtsp_port: u16,
    pub hls_port: u16,
    pub rtmp_port: u16,
    pub api_port: u16,
}

impl UrlFacts {
    pub fn host_for(&self, advertised_host: Option<&str>, host_header: Option<&str>) -> String {
        advertised_host
            .filter(|host| !host.is_empty())
            .map(str::to_string)
            .or_else(|| host_without_port(host_header))
            .unwrap_or_else(|| "localhost".to_string())
    }
}

/// The host part of an HTTP Host header; IPv6 literals keep their brackets.
pub fn host_without_port(host_header: Option<&str>) -> Option<String> {
    let header = host_header.filter(|header| !header.is_empty())?;
    if header.starts_with('[') {
        return match header.find(']') {
            Some(closing) if closing > 0 => Some(header[..=closing].to_string()),
            _ => None,
        };
    }
    let host = header.split(':').next().unwrap_or("");
    (!host.is_empty()).then(|| host.to_string())
}

/// A status row plus what its transport kind means and the URLs the consumer registers.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct DescribedStream {
    #[serde(flatten)]
    pub status: StreamStatus,
    pub transport_label: &'static str,
    pub direction: Direction,
    pub carries_klv: bool,
    pub delivery_label: &'static str,
    pub register: Vec<(&'static str, String)>,
}

pub fn describe_streams(
    statuses: &[StreamStatus],
    settings: &RuntimeSettings,
    host: &str,
    url_facts: &UrlFacts,
) -> Vec<DescribedStream> {
    let multicast_group = if settings.multicast_group.is_empty() {
        "<multicast group>"
    } else {
        settings.multicast_group.as_str()
    };
    statuses
        .iter()
        .map(|status| {
            let kind = kind_by_id(&status.transport).unwrap_or_else(|| {
                panic!("status row names unknown transport {}", status.transport)
            });
            let fields = TemplateFields {
                rtsp_port: url_facts.rtsp_port,
                hls_port: url_facts.hls_port,
                rtmp_port: url_facts.rtmp_port,
                api_port: url_facts.api_port,
                host,
                port: status.port,
                name: &status.name,
                multicast_group,
                ..TemplateFields::default()
            };
            DescribedStream {
                status: status.clone(),
                transport_label: kind.label,
                direction: kind.direction,
                carries_klv: kind.carries_klv,
                delivery_label: kind.delivery_label(),
                register: kind
                    .register
                    .iter()
                    .map(|(label, template)| (*label, fields.fill(template)))
                    .collect(),
            }
        })
        .collect()
}

/// One video as the play page shows it: its play stream's state and the address to register.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct PlayRow {
    pub file: String,
    pub state: Option<StreamState>,
    pub reason: Option<String>,
    pub selected: Option<PlayChoice>,
    pub url: Option<String>,
}

/// The play page button that describes what a video's play stream is doing.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PlayChoice {
    Once,
    Forever,
    Stop,
}

impl PlayChoice {
    /// None once a finite play ends, or for a loop count other than once or forever.
    fn of(status: &StreamStatus) -> Option<PlayChoice> {
        if !status.enabled {
            return Some(PlayChoice::Stop);
        }
        if status.state == StreamState::Completed {
            return None;
        }
        match status.loop_count {
            0 => Some(PlayChoice::Once),
            -1 => Some(PlayChoice::Forever),
            _ => None,
        }
    }
}

/// Every offered video with its play stream and its address on `host`.
pub fn describe_play_rows(
    snapshot: &StatusSnapshot,
    host: &str,
    url_facts: &UrlFacts,
) -> Vec<PlayRow> {
    snapshot
        .videos
        .iter()
        .map(|file| {
            let stream = snapshot
                .statuses
                .iter()
                .find(|status| is_play_stream(&status.filename, &status.transport, file));
            let url = stream.and_then(|status| {
                describe_streams(
                    std::slice::from_ref(status),
                    &snapshot.settings,
                    host,
                    url_facts,
                )
                .remove(0)
                .register
                .into_iter()
                .find_map(|(label, url)| (label == "rtsp").then_some(url))
            });
            PlayRow {
                file: file.clone(),
                state: stream.map(|status| status.state),
                reason: stream.and_then(|status| status.reason.clone()),
                selected: stream.and_then(PlayChoice::of),
                url,
            }
        })
        .collect()
}

/// The kinds table as the UI needs it to offer and explain choices.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TransportDescription {
    pub id: &'static str,
    pub label: &'static str,
    pub direction: Direction,
    pub carries_klv: bool,
    pub first_port_setting: Option<&'static str>,
    pub random_port: bool,
    pub needs: Vec<&'static str>,
}

pub fn describe_transports() -> Vec<TransportDescription> {
    KINDS
        .iter()
        .map(|kind| TransportDescription {
            id: kind.id,
            label: kind.label,
            direction: kind.direction,
            carries_klv: kind.carries_klv,
            first_port_setting: kind.first_port_setting.map(|setting| setting.name()),
            random_port: kind.random_port,
            needs: kind.needs.iter().map(|need| need.name()).collect(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testsupport::fixture;

    #[test]
    fn transports_table_matches_the_python_fixture() {
        assert_eq!(
            serde_json::to_value(describe_transports()).unwrap(),
            fixture("transports.json")
        );
    }

    #[test]
    fn host_header_parsing_matches_the_python_fixture() {
        let fixture = fixture("queries.json");
        for case in fixture["host_without_port"].as_array().unwrap() {
            let header = case["host_header"].as_str();
            assert_eq!(
                serde_json::to_value(host_without_port(header)).unwrap(),
                case["host"],
                "{case}"
            );
        }
        let facts = UrlFacts {
            rtsp_port: 8554,
            hls_port: 8888,
            rtmp_port: 1935,
            api_port: 8080,
        };
        for case in fixture["host_for"].as_array().unwrap() {
            let host = facts.host_for(
                case["advertised_host"].as_str(),
                case["host_header"].as_str(),
            );
            assert_eq!(Value::String(host), case["host"], "{case}");
        }
    }

    use serde_json::Value;
}
