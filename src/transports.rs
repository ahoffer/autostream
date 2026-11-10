//! The transport kinds a stream can be delivered over: one row per kind, and one rule per row.
//!
//! Templates take these fields: destination_host, multicast_group, srtp_key,
//! port, name, rtsp_port, srt_port, hls_port, rtmp_port, api_port, hls_dir,
//! and host (the advertised host, chosen per request).

use serde::Serialize;

use crate::runtimesettings::{PortSetting, RequiredSetting};

/// push: we send to the consumer; listen: it connects to us; serve: MediaMTX or files.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Direction {
    Push,
    Listen,
    Serve,
}

/// The ffmpeg output format.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Muxer {
    Rtsp,
    Mpegts,
    RtpMpegts,
    Hls,
}

impl Muxer {
    pub fn as_str(self) -> &'static str {
        match self {
            Muxer::Rtsp => "rtsp",
            Muxer::Mpegts => "mpegts",
            Muxer::RtpMpegts => "rtp_mpegts",
            Muxer::Hls => "hls",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TransportKind {
    pub id: &'static str,
    pub label: &'static str,
    pub direction: Direction,
    pub carries_klv: bool,
    pub muxer: Muxer,
    /// The settings field holding this kind's first port; None when none applies.
    pub first_port_setting: Option<PortSetting>,
    /// Settings that must be set before the stream can start.
    pub needs: &'static [RequiredSetting],
    /// Template for the ffmpeg output URL or path.
    pub delivery: &'static str,
    /// (label, template) pairs: what the consumer registers.
    pub register: &'static [(&'static str, &'static str)],
    /// Per-kind ffmpeg output options, templated.
    pub extra_output_args: &'static [&'static str],
    /// Takes a random high port instead of one from a first port.
    pub random_port: bool,
    /// The timestamp filter buffers sparse packets; local SRT does not need it.
    pub clamp_data_dts: bool,
}

impl TransportKind {
    pub fn takes_port(&self) -> bool {
        self.first_port_setting.is_some() || self.random_port
    }

    pub fn delivery_label(&self) -> &'static str {
        match self.direction {
            Direction::Push => "sends to",
            Direction::Listen => "listens on",
            Direction::Serve => {
                if self.muxer == Muxer::Hls {
                    "writes to"
                } else {
                    "publishes to"
                }
            }
        }
    }
}

/// One table row; the arguments are the columns every kind fills in.
#[allow(clippy::too_many_arguments)]
const fn kind(
    id: &'static str,
    label: &'static str,
    direction: Direction,
    muxer: Muxer,
    first_port_setting: Option<PortSetting>,
    needs: &'static [RequiredSetting],
    delivery: &'static str,
    register: &'static [(&'static str, &'static str)],
) -> TransportKind {
    TransportKind {
        id,
        label,
        direction,
        carries_klv: true,
        muxer,
        first_port_setting,
        needs,
        delivery,
        register,
        extra_output_args: &[],
        random_port: false,
        clamp_data_dts: true,
    }
}

pub const KINDS: [TransportKind; 10] = [
    // FFmpeg cannot publish KLV over RTSP. MPEG-TS over local SRT preserves it
    // for MediaMTX's RTSP output; its HLS and RTMP outputs remain video/audio.
    TransportKind {
        clamp_data_dts: false,
        ..kind(
            "rtsp",
            "RTSP / HLS / RTMP via MediaMTX",
            Direction::Serve,
            Muxer::Mpegts,
            None,
            &[],
            "srt://127.0.0.1:{srt_port}?streamid=publish:{name}",
            &[
                ("rtsp", "rtsp://{host}:{rtsp_port}/{name}"),
                ("hls", "http://{host}:{hls_port}/{name}/index.m3u8"),
                ("rtmp", "rtmp://{host}:{rtmp_port}/{name}"),
            ],
        )
    },
    kind(
        "udp",
        "UDP",
        Direction::Push,
        Muxer::Mpegts,
        Some(PortSetting::Udp),
        &[RequiredSetting::DestinationHost],
        "udp://{destination_host}:{port}?pkt_size=1316",
        &[("register", "udp://0.0.0.0:{port}")],
    ),
    // The consumer's redirect service joins the group on its host network, so
    // any port works there; a random one proves that and keeps clear of the
    // unicast range the consumer forwards into its container.
    TransportKind {
        random_port: true,
        ..kind(
            "multicast",
            "Multicast",
            Direction::Push,
            Muxer::Mpegts,
            None,
            &[RequiredSetting::MulticastGroup],
            "udp://{multicast_group}:{port}?pkt_size=1316&ttl=1",
            &[("register", "udp://{multicast_group}:{port}")],
        )
    },
    kind(
        "rtp",
        "RTP / TS",
        Direction::Push,
        Muxer::RtpMpegts,
        Some(PortSetting::Rtp),
        &[RequiredSetting::DestinationHost],
        "rtp://{destination_host}:{port}",
        &[("register", "rtp://0.0.0.0:{port}")],
    ),
    kind(
        "srt_listen",
        "SRT listen",
        Direction::Listen,
        Muxer::Mpegts,
        Some(PortSetting::SrtListen),
        &[],
        "srt://0.0.0.0:{port}?mode=listener",
        &[("register", "srt://{host}:{port}")],
    ),
    kind(
        "srt_call",
        "SRT call",
        Direction::Push,
        Muxer::Mpegts,
        Some(PortSetting::SrtCall),
        &[RequiredSetting::DestinationHost],
        "srt://{destination_host}:{port}",
        &[("register", "srt://0.0.0.0:{port}?mode=listener")],
    ),
    kind(
        "tcp_listen",
        "TCP listen",
        Direction::Listen,
        Muxer::Mpegts,
        Some(PortSetting::Tcp),
        &[],
        "tcp://0.0.0.0:{port}?listen=1",
        &[("register", "tcp://{host}:{port}")],
    ),
    TransportKind {
        extra_output_args: &["-listen", "1"],
        ..kind(
            "http",
            "HTTP TS",
            Direction::Listen,
            Muxer::Mpegts,
            Some(PortSetting::Http),
            &[],
            "http://0.0.0.0:{port}",
            &[("register", "http://{host}:{port}")],
        )
    },
    kind(
        "hls",
        "HLS (TS segments)",
        Direction::Serve,
        Muxer::Hls,
        None,
        &[],
        "{hls_dir}/{name}/index.m3u8",
        &[("register", "http://{host}:{api_port}/hls/{name}/index.m3u8")],
    ),
    TransportKind {
        extra_output_args: &[
            "-srtp_out_suite",
            "AES_CM_128_HMAC_SHA1_80",
            "-srtp_out_params",
            "{srtp_key}",
        ],
        ..kind(
            "srtp",
            "SRTP",
            Direction::Push,
            Muxer::RtpMpegts,
            Some(PortSetting::Srtp),
            &[RequiredSetting::DestinationHost, RequiredSetting::SrtpKey],
            "srtp://{destination_host}:{port}",
            &[("register", "srtp://0.0.0.0:{port}")],
        )
    },
];

/// Lookup by id: the API and the state file name kinds by id.
pub fn kind_by_id(id: &str) -> Option<&'static TransportKind> {
    KINDS.iter().find(|kind| kind.id == id)
}

/// Everything a delivery or register template may name.
#[derive(Clone, Debug, Default)]
pub struct TemplateFields<'a> {
    pub destination_host: Option<&'a str>,
    pub multicast_group: &'a str,
    pub srtp_key: &'a str,
    pub port: Option<u16>,
    pub name: &'a str,
    pub rtsp_port: u16,
    pub srt_port: u16,
    pub hls_port: u16,
    pub rtmp_port: u16,
    pub api_port: u16,
    pub hls_dir: &'a str,
    pub host: &'a str,
}

impl TemplateFields<'_> {
    fn value(&self, placeholder: &str) -> Option<String> {
        Some(match placeholder {
            "destination_host" => self.destination_host.unwrap_or("None").to_string(),
            "multicast_group" => self.multicast_group.to_string(),
            "srtp_key" => self.srtp_key.to_string(),
            "port" => self
                .port
                .map_or_else(|| "None".to_string(), |port| port.to_string()),
            "name" => self.name.to_string(),
            "rtsp_port" => self.rtsp_port.to_string(),
            "srt_port" => self.srt_port.to_string(),
            "hls_port" => self.hls_port.to_string(),
            "rtmp_port" => self.rtmp_port.to_string(),
            "api_port" => self.api_port.to_string(),
            "hls_dir" => self.hls_dir.to_string(),
            "host" => self.host.to_string(),
            _ => return None,
        })
    }

    /// Replace every `{field}` in the template; an unknown placeholder stays as written.
    pub fn fill(&self, template: &str) -> String {
        let mut rendered = String::with_capacity(template.len());
        let mut rest = template;
        while let Some(start) = rest.find('{') {
            rendered.push_str(&rest[..start]);
            let after_brace = &rest[start + 1..];
            match after_brace.find('}') {
                Some(end) => {
                    let placeholder = &after_brace[..end];
                    match self.value(placeholder) {
                        Some(value) => rendered.push_str(&value),
                        None => {
                            rendered.push('{');
                            rendered.push_str(placeholder);
                            rendered.push('}');
                        }
                    }
                    rest = &after_brace[end + 1..];
                }
                None => {
                    rendered.push_str(&rest[start..]);
                    rest = "";
                }
            }
        }
        rendered.push_str(rest);
        rendered
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_fields() -> TemplateFields<'static> {
        TemplateFields {
            destination_host: Some("receiver"),
            multicast_group: "239.1.1.2",
            srtp_key: "a2V5",
            port: Some(40001),
            name: "boats",
            rtsp_port: 8554,
            srt_port: 8890,
            hls_port: 8888,
            rtmp_port: 1935,
            api_port: 8080,
            hls_dir: "/app/hls",
            host: "publisher",
        }
    }

    #[test]
    fn ids_are_unique_and_every_template_formats_from_the_known_fields() {
        let fields = sample_fields();
        for (index, kind) in KINDS.iter().enumerate() {
            assert!(
                KINDS[..index].iter().all(|other| other.id != kind.id),
                "{}",
                kind.id
            );
            assert!(!fields.fill(kind.delivery).contains('{'), "{}", kind.id);
            for (_, template) in kind.register {
                assert!(!fields.fill(template).contains('{'), "{}", kind.id);
            }
            for argument in kind.extra_output_args {
                assert!(!fields.fill(argument).contains('{'), "{}", kind.id);
            }
        }
        assert_eq!(
            fields.fill("udp://{destination_host}:{port}?x"),
            "udp://receiver:40001?x"
        );
        assert_eq!(fields.fill("{unknown} {name"), "{unknown} {name");
    }

    #[test]
    fn ingest_preserves_klv_and_push_kinds_name_their_target() {
        assert!(KINDS.iter().all(|kind| kind.carries_klv));
        let random: Vec<_> = KINDS
            .iter()
            .filter(|kind| kind.random_port)
            .map(|kind| kind.id)
            .collect();
        assert_eq!(random, ["multicast"]);
        let portless: Vec<_> = KINDS
            .iter()
            .filter(|kind| !kind.takes_port())
            .map(|kind| kind.id)
            .collect();
        assert_eq!(portless, ["rtsp", "hls"]);
        for kind in &KINDS {
            if kind.direction == Direction::Push {
                assert!(
                    kind.needs.iter().any(|need| matches!(
                        need,
                        RequiredSetting::DestinationHost | RequiredSetting::MulticastGroup
                    )),
                    "{}",
                    kind.id
                );
            } else {
                assert!(
                    !kind.needs.contains(&RequiredSetting::DestinationHost),
                    "{}",
                    kind.id
                );
            }
        }
        assert_eq!(kind_by_id("hls").unwrap().delivery_label(), "writes to");
        assert_eq!(kind_by_id("rtsp").unwrap().delivery_label(), "publishes to");
        assert_eq!(kind_by_id("udp").unwrap().delivery_label(), "sends to");
        assert_eq!(kind_by_id("http").unwrap().delivery_label(), "listens on");
        assert!(kind_by_id("pigeon").is_none());
    }
}
