//! Everything ffmpeg: probing a file and building argv.
//!
//! `MediaStream` decides whether delivery is enabled. This module probes media,
//! resolves encoding choices, and renders the resulting command line. Each stream
//! is its own ffmpeg process, so `build_argv` produces one complete command per
//! stream, shaped by its `Delivery`:
//!
//! - rtsp muxer: direct FFmpeg RTSP output maps video/audio only because
//!   FFmpeg's RTP publisher does not support KLV.
//! - other muxers: MPEG-TS, plain or inside RTP, or as HLS segments, with EVERY
//!   stream mapped and data/KLV copied through untouched. These include local
//!   SRT ingest into MediaMTX for RTSP KLV output.
//!
//! In transcode mode video is re-encoded to a clean GOP structure to fix looping
//! artifacts and to honor the bitrate cap, using libx264 in software. In
//! passthrough mode the source video is sent unmodified: no decode or encode.
//!
//! Every flag is explained once, in the flag explanations at the bottom.

use std::collections::BTreeSet;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

use crate::boundedcommand::run_bounded;
use crate::streamrecord::{StreamChoices, VideoMode};
use crate::transports::Muxer;

/// GOP fallback in frames when the file's frame rate is unknown. The natural
/// default is one second of frames at the file's own rate (`natural_gop_frames`);
/// each stream can override it in the UI.
pub const DEFAULT_GOP_FRAMES: i64 = 30;

/// One second of frames at the output rate, the GOP used unless overridden.
pub fn natural_gop_frames(frame_rate: Option<f64>) -> i64 {
    match frame_rate {
        Some(rate) if rate != 0.0 => (rate.round_ties_even() as i64).max(1),
        _ => DEFAULT_GOP_FRAMES,
    }
}

/// What ffprobe says about a video file; the inputs to every encode decision.
///
/// None in a field means the probe failed or the file has no such track.
/// `audio_copyable` is true only when each audio track is AAC that already
/// carries global headers (extradata): ADTS AAC, which the packaging MPEG-TS
/// sources use, has none, and ffmpeg's RTSP muxer needs the headers at SDP
/// time, before the first packet flows, so the aac_adtstoasc bitstream filter
/// cannot rescue a copy. Such audio is re-encoded instead.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct MediaProbeResult {
    pub video_codecs: Option<Vec<String>>,
    pub audio_codecs: Option<Vec<String>>,
    pub audio_copyable: bool,
    pub pix_fmt: Option<String>,
    pub bitrate_bps: Option<i64>,
    pub frame_rate: Option<f64>,
    /// KLV shows up here as 'klv'.
    pub data_codecs: Option<Vec<String>>,
}

impl MediaProbeResult {
    pub const UNKNOWN: MediaProbeResult = MediaProbeResult {
        video_codecs: None,
        audio_codecs: None,
        audio_copyable: false,
        pix_fmt: None,
        bitrate_bps: None,
        frame_rate: None,
        data_codecs: None,
    };
}

/// ffprobe's "30000/1001" rational (or "0/0" for unknown) as a number, or None.
fn parse_frame_rate(value: Option<&Value>) -> Option<f64> {
    let text = value?.as_str()?;
    let (numerator, denominator) = text.split_once('/')?;
    if numerator.contains('/') || denominator.contains('/') {
        return None;
    }
    let numerator: i64 = numerator.trim().parse().ok()?;
    let denominator: i64 = denominator.trim().parse().ok()?;
    (numerator > 0 && denominator > 0).then(|| numerator as f64 / denominator as f64)
}

/// Decode an ffprobe JSON report; None when the report is not JSON.
///
/// The codec lists are index-ordered and cover every track, because the
/// generated commands map every track. Bitrate comes from the first video
/// stream, falling back to the container: ffprobe reports N/A for both on
/// some MPEG-TS files, which is simply an unknown bitrate.
///
/// JSON output rather than csv: csv emits fields in ffprobe's internal order,
/// not the requested order, and MPEG-TS inputs repeat streams under a programs
/// section. Reading only the top-level streams dodges both traps.
pub fn parse_probe_report(stdout: &str) -> Option<MediaProbeResult> {
    let report: Value = serde_json::from_str(stdout).ok()?;
    let mut ordered: Vec<&serde_json::Map<String, Value>> = report
        .get("streams")
        .and_then(Value::as_array)
        .map(|streams| streams.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default();
    ordered.sort_by_key(|entry| entry.get("index").and_then(Value::as_i64).unwrap_or(0));
    let of_type = |codec_type: &str| -> Vec<&serde_json::Map<String, Value>> {
        ordered
            .iter()
            .copied()
            .filter(|entry| entry.get("codec_type").and_then(Value::as_str) == Some(codec_type))
            .collect()
    };
    let video_streams = of_type("video");
    let audio_streams = of_type("audio");
    let data_streams = of_type("data");
    let codec_name = |entry: &serde_json::Map<String, Value>| -> String {
        entry
            .get("codec_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let digits = |value: Option<&Value>| -> Option<i64> {
        let text = value?.as_str()?;
        (!text.is_empty() && text.chars().all(|character| character.is_ascii_digit()))
            .then(|| text.parse().ok())
            .flatten()
    };
    let bitrate = video_streams
        .first()
        .and_then(|entry| digits(entry.get("bit_rate")))
        .or_else(|| {
            digits(
                report
                    .get("format")
                    .and_then(|format| format.get("bit_rate")),
            )
        });
    Some(MediaProbeResult {
        video_codecs: Some(
            video_streams
                .iter()
                .map(|entry| codec_name(entry))
                .collect(),
        ),
        audio_codecs: Some(
            audio_streams
                .iter()
                .map(|entry| codec_name(entry))
                .collect(),
        ),
        audio_copyable: audio_streams.iter().all(|entry| {
            codec_name(entry) == "aac"
                && entry
                    .get("extradata_size")
                    .and_then(Value::as_i64)
                    .unwrap_or(0)
                    > 0
        }),
        pix_fmt: video_streams
            .first()
            .and_then(|entry| entry.get("pix_fmt"))
            .and_then(Value::as_str)
            .map(str::to_string),
        bitrate_bps: bitrate,
        frame_rate: video_streams
            .first()
            .and_then(|entry| parse_frame_rate(entry.get("avg_frame_rate"))),
        data_codecs: Some(data_streams.iter().map(|entry| codec_name(entry)).collect()),
    })
}

/// Probe the file with ffprobe; `MediaProbeResult::UNKNOWN` when the probe fails.
pub fn probe_file(video_path: &Path) -> MediaProbeResult {
    let argv: Vec<String> = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,pix_fmt,extradata_size,bit_rate,avg_frame_rate:format=bit_rate",
        "-of",
        "json",
    ]
    .into_iter()
    .map(str::to_string)
    .chain(std::iter::once(video_path.to_string_lossy().into_owned()))
    .collect();
    let output = match run_bounded(&argv, Duration::from_secs(10), Stdio::piped()) {
        Ok(output) => output,
        Err(error) => {
            log::warn!(
                "Error probing {}: {}",
                video_path.file_name().unwrap_or_default().to_string_lossy(),
                error
            );
            return MediaProbeResult::UNKNOWN;
        }
    };
    if !output.status.success() {
        return MediaProbeResult::UNKNOWN;
    }
    parse_probe_report(&String::from_utf8_lossy(&output.stdout))
        .unwrap_or(MediaProbeResult::UNKNOWN)
}

/// How ffmpeg delivers one stream; when `url` is None, `blocker` says what the registry still lacks.
#[derive(Clone, Debug, PartialEq)]
pub struct Delivery {
    pub url: Option<String>,
    pub muxer: Muxer,
    pub carries_klv: bool,
    pub extra_args: Vec<String>,
    /// A name that must resolve before pushing.
    pub resolve_host: Option<String>,
    /// ffmpeg serves one client and exits when it leaves.
    pub client_bound: bool,
    pub blocker: Option<String>,
    pub clamp_data_dts: bool,
}

impl Delivery {
    /// A transport stream delivery to the URL with every other field at its default.
    pub fn to_url(url: &str) -> Self {
        Self {
            url: Some(url.to_string()),
            ..Self::blocked("")
        }
    }

    /// A delivery that cannot start yet, with the reason.
    pub fn blocked(blocker: &str) -> Self {
        Self {
            url: None,
            muxer: Muxer::Mpegts,
            carries_klv: true,
            extra_args: Vec::new(),
            resolve_host: None,
            client_bound: false,
            blocker: (!blocker.is_empty()).then(|| blocker.to_string()),
            clamp_data_dts: true,
        }
    }
}

/// Immutable encoding decisions for one command, after media inspection.
#[derive(Clone, Debug, PartialEq)]
pub struct FFmpegCommandSettings {
    pub loop_count: i64,
    pub gop_frames: i64,
    /// Output rate under transcode; None keeps the source's.
    pub frame_rate: Option<f64>,
    pub passthrough: bool,
    pub audio_copy: bool,
    pub bitrate_cap: String,
}

impl FFmpegCommandSettings {
    pub fn encoder_label(&self) -> &'static str {
        if self.passthrough {
            "copy (passthrough)"
        } else {
            "libx264 (software)"
        }
    }
}

/// The shortest decimal text for a rate, as C's `%g` prints it: six significant digits, no trailing zeros.
pub fn general_format(value: f64) -> String {
    if value == 0.0 {
        return "0".to_string();
    }
    let exponent = value.abs().log10().floor() as i32;
    if !(-5..6).contains(&exponent) {
        let scientific = format!("{:.5e}", value);
        let (mantissa, exponent_text) = scientific.split_once('e').unwrap_or((&scientific, "0"));
        let exponent: i32 = exponent_text.parse().unwrap_or(0);
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{}e{sign}{:02}", trim_fraction(mantissa), exponent.abs());
    }
    let decimals = (5 - exponent).max(0) as usize;
    trim_fraction(&format!("{value:.decimals$}"))
}

fn trim_fraction(text: &str) -> String {
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_string()
    } else {
        text.to_string()
    }
}

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| part.to_string()).collect()
}

/// Render the prepared decisions as one complete FFmpeg command for one delivery.
pub fn build_argv(
    source_path: &Path,
    spec: &FFmpegCommandSettings,
    delivery: &Delivery,
) -> Vec<String> {
    let video_opts;
    let bitrate_opts;
    let vsync_opts;
    let ts_fix;
    if spec.passthrough {
        // The supervisor verified every video track is one MediaMTX carries: send the
        // source bits unmodified. A bitrate cap cannot apply to copied packets
        // (the supervisor passes none), -vsync cfr is an encode-path operation
        // ffmpeg rejects with copy, and +igndts is dropped because copied
        // B-frame packets need their input DTS to be interleaved correctly.
        video_opts = strings(&["-c:v", "copy"]);
        bitrate_opts = Vec::new();
        vsync_opts = Vec::new();
        ts_fix = strings(&[
            "-fflags",
            "+genpts",
            "-avoid_negative_ts",
            "make_zero",
            "-max_muxing_queue_size",
            "1024",
        ]);
    } else {
        let gop = spec.gop_frames.to_string();
        let mut opts = strings(&[
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            &gop,
            "-keyint_min",
            &gop,
            "-sc_threshold",
            "0",
            "-bf",
            "0",
            "-x264-params",
            "ref=1",
        ]);
        if let Some(rate) = spec.frame_rate.filter(|rate| *rate != 0.0) {
            opts.push("-r".to_string());
            opts.push(general_format(rate));
        }
        video_opts = opts;
        bitrate_opts = if spec.bitrate_cap.is_empty() {
            Vec::new()
        } else {
            strings(&[
                "-b:v",
                &spec.bitrate_cap,
                "-maxrate",
                &spec.bitrate_cap,
                "-bufsize",
                &spec.bitrate_cap,
            ])
        };
        vsync_opts = strings(&["-vsync", "cfr"]);
        ts_fix = strings(&[
            "-fflags",
            "+genpts+igndts",
            "-avoid_negative_ts",
            "make_zero",
            "-max_muxing_queue_size",
            "1024",
        ]);
    }
    let audio_opts = if spec.audio_copy {
        strings(&["-c:a", "copy"])
    } else {
        strings(&["-c:a", "aac", "-b:a", "128k"])
    };

    let mut argv = strings(&["ffmpeg", "-hide_banner", "-nostats", "-re", "-stream_loop"]);
    argv.push(spec.loop_count.to_string());
    argv.push("-i".to_string());
    argv.push(source_path.to_string_lossy().into_owned());
    if delivery.carries_klv {
        // Inside RTP the transport stream is re-muxed once more, and that outer
        // muxer insists on monotonic timestamps, so there the muxer must be
        // allowed to interleave the sparse KLV packets properly.
        let interleave = if delivery.muxer == Muxer::RtpMpegts {
            Vec::new()
        } else {
            strings(&["-max_interleave_delta", "1000"])
        };
        // setts buffers a packet until the next arrives. Avoid delaying sparse,
        // untimestamped KLV on MediaMTX's local SRT feed; it accepts native DTS.
        let data_ts = if delivery.clamp_data_dts {
            strings(&["-bsf:d", "setts=dts=max(DTS\\,PREV_OUTDTS)"])
        } else {
            Vec::new()
        };
        argv.extend(strings(&["-map", "0", "-copy_unknown"]));
        argv.extend(video_opts);
        argv.extend(bitrate_opts);
        argv.extend(audio_opts);
        argv.extend(strings(&["-c:d", "copy"]));
        argv.extend(interleave);
        argv.extend(data_ts);
        argv.extend(ts_fix);
    } else {
        argv.extend(strings(&["-map", "0:v?", "-map", "0:a?"]));
        argv.extend(video_opts);
        argv.extend(bitrate_opts);
        argv.extend(audio_opts);
        argv.extend(ts_fix);
        argv.extend(vsync_opts);
    }
    argv.extend(delivery.extra_args.iter().cloned());
    let url = delivery.url.clone().unwrap_or_default();
    match delivery.muxer {
        Muxer::Rtsp => {
            argv.extend(strings(&["-f", "rtsp", "-rtsp_transport", "tcp"]));
            argv.push(url);
        }
        Muxer::Hls => {
            let segments = Path::new(&url)
                .with_file_name("seg%05d.ts")
                .to_string_lossy()
                .into_owned();
            argv.extend(strings(&[
                "-f",
                "hls",
                "-hls_time",
                "2",
                "-hls_list_size",
                "6",
                "-hls_flags",
                "delete_segments",
                "-hls_segment_type",
                "mpegts",
                "-hls_segment_filename",
                &segments,
            ]));
            argv.push(url);
        }
        muxer => {
            argv.extend(strings(&["-f", muxer.as_str()]));
            argv.push(url);
        }
    }
    argv
}

const PASSTHROUGH_CODECS: [&str; 5] = ["h264", "hevc", "mpeg2video", "mpeg4", "mjpeg"];

/// Everything a preparation decided, for the command and the status row.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct PreparedCommand {
    pub argv: Vec<String>,
    pub error: Option<String>,
    pub video_codecs: Vec<String>,
    pub audio_codecs: Vec<String>,
    pub data_codecs: Vec<String>,
    pub mode: VideoMode,
    pub effective_mode: VideoMode,
    pub encoder: &'static str,
    pub passthrough_eligible: Option<bool>,
    pub ineligible_codecs: Vec<String>,
    pub source_frame_rate: Option<f64>,
    pub frame_rate: Option<f64>,
    pub frame_rate_explicit: bool,
    pub gop_frames: i64,
    pub gop_explicit: bool,
}

/// Keep decisions reproducible from a probe and explicit command inputs.
pub fn command_for_profile(
    source_path: &Path,
    choices: &StreamChoices,
    delivery: &Delivery,
    profile: &MediaProbeResult,
    max_video_bps: Option<i64>,
) -> PreparedCommand {
    // MJPEG can travel directly over RTSP, but not through our MPEG-TS muxers.
    let codecs: BTreeSet<&str> = PASSTHROUGH_CODECS
        .into_iter()
        .filter(|codec| delivery.muxer == Muxer::Rtsp || *codec != "mjpeg")
        .collect();
    let video_codecs = profile.video_codecs.clone().unwrap_or_default();
    let has_video = !video_codecs.is_empty();
    let eligible = has_video
        && video_codecs
            .iter()
            .all(|codec| codecs.contains(codec.as_str()));
    let mode = choices.mode.unwrap_or(VideoMode::Passthrough);
    let passthrough = mode == VideoMode::Passthrough && eligible;
    let fps = choices.frame_rate;
    let gop = choices
        .gop_frames
        .unwrap_or_else(|| natural_gop_frames(fps.or(profile.frame_rate)));
    let cap = match (max_video_bps, profile.bitrate_bps) {
        (Some(limit), Some(bitrate)) if limit != 0 && bitrate > limit && !passthrough => {
            limit.to_string()
        }
        _ => String::new(),
    };
    let spec = FFmpegCommandSettings {
        loop_count: choices.loop_count,
        gop_frames: gop,
        frame_rate: fps,
        passthrough,
        audio_copy: profile.audio_copyable,
        bitrate_cap: cap,
    };
    let ineligible: Vec<String> = video_codecs
        .iter()
        .filter(|codec| !codecs.contains(codec.as_str()))
        .cloned()
        .collect::<BTreeSet<String>>()
        .into_iter()
        .collect();
    PreparedCommand {
        argv: if delivery.url.is_some() {
            build_argv(source_path, &spec, delivery)
        } else {
            Vec::new()
        },
        error: (!has_video)
            .then(|| "No video track found; waiting for the file to change".to_string()),
        video_codecs,
        audio_codecs: profile.audio_codecs.clone().unwrap_or_default(),
        data_codecs: profile.data_codecs.clone().unwrap_or_default(),
        mode,
        effective_mode: if passthrough {
            VideoMode::Passthrough
        } else {
            VideoMode::Transcode
        },
        encoder: spec.encoder_label(),
        passthrough_eligible: profile.video_codecs.as_ref().map(|_| eligible),
        ineligible_codecs: ineligible,
        source_frame_rate: profile.frame_rate,
        frame_rate: fps.or(profile.frame_rate),
        frame_rate_explicit: fps.is_some(),
        gop_frames: gop,
        gop_explicit: choices.gop_frames.is_some(),
    }
}

/// Join argv for display the way a POSIX shell would need it typed.
pub fn shell_join(argv: &[String]) -> String {
    argv.iter()
        .map(|argument| shell_quote(argument))
        .collect::<Vec<_>>()
        .join(" ")
}

fn shell_quote(argument: &str) -> String {
    if argument.is_empty() {
        return "''".to_string();
    }
    let plain = argument
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || "_@%+=:,./-".contains(character));
    if plain {
        argument.to_string()
    } else {
        format!("'{}'", argument.replace('\'', "'\"'\"'"))
    }
}

// FLAG EXPLANATIONS
//
// -hide_banner                   No version/build banner, so the captured log is signal
// -nostats                       No periodic progress line; it would grow the per-stream
//                                log file the supervisor captures stderr into forever
// ffmpeg                         Every command must start with ffmpeg; the supervisor
//                                runs argv directly, no shell
// -re                            Read input at native frame rate (real-time streaming)
// -stream_loop N                 Loop the input N times; -1 = forever
// -i <video-file>                Input video file
//
// STREAM SELECTION:
// -map 0:v? -map 0:a?            (RTSP output) Keep every video and audio track,
//                                not just the single "best" of each that ffmpeg
//                                picks by default. The trailing ? makes each
//                                optional so files with no audio (or no video)
//                                still stream. Data/KLV and subtitles are NOT
//                                mapped here: ffmpeg's RTP muxer cannot carry them
//                                so mapping them would make the RTSP header fail.
// -map 0 -copy_unknown           (UDP output) Keep ALL streams, including data
//                                and tracks ffmpeg cannot identify. MPEG-TS over
//                                UDP carries KLV/MISB timed metadata natively.
// -c:d copy                      (UDP output) Copy data streams (KLV) through untouched.
//
// MODE SELECTION (passthrough / audio copy, decided by the supervisor):
// -c:v copy                      (passthrough) Send the source video unmodified. The
//                                supervisor only requests this after verifying every
//                                video track is one MediaMTX carries over RTSP.
//                                No GOP normalization happens, so loop recovery and
//                                HLS segmenting follow the source's keyframe cadence,
//                                and the bitrate cap is inert.
// -c:a copy                      (audio-mode copy) All mapped audio tracks were
//                                already AAC; re-encoding would burn CPU to lose
//                                quality. Decided by the supervisor per start.
//
// VIDEO ENCODING (fixes GOP and B-frame issues):
// -c:v libx264                   Encode to H.264 (re-encode to fix structure)
// -preset ultrafast              Fastest encoding preset (low CPU usage)
// -tune zerolatency              Optimize for low-latency streaming
// -x264-params ref=1             Use only 1 reference frame (reduces loop boundary complexity)
// -sc_threshold 0                Disable scene detection (prevents unexpected keyframes)
//
// VIDEO STRUCTURE AND BITRATE:
// -g <gop>                       GOP size in frames, the stream's GOP setting
//                                (default 30 = 1s @ 30fps) for quick recovery
// -keyint_min <gop>              Minimum keyframe interval, same value
// -bf 0                          Disable B-frames (eliminates reference frame issues at loop)
// -b:v/-maxrate/-bufsize <cap>   Cap the video bitrate (transcode only, when a cap is passed)
//
// AUDIO ENCODING (audio-mode encode):
// -c:a aac                       Encode to AAC
// -b:a 128k                      Audio bitrate: 128 kbps
//
// TIMESTAMP FIXES (eliminates negative DTS and discontinuities):
// -fflags +genpts                Regenerate presentation timestamps (fixes loop discontinuities)
// -fflags +igndts                (transcode only) Ignore input DTS (eliminates negative
//                                -0.067s DTS). Under passthrough the copied packets'
//                                input DTS are the only correct interleaving order for
//                                B-frame sources, so there it is dropped.
// -avoid_negative_ts make_zero   Shift all timestamps to start at 0 (prevents negative values)
// -r <fps>                       (transcode only, when the stream's FPS setting is set)
//                                Output frame rate; frames are dropped or duplicated
//                                to hit it. Absent, the source rate is kept
// -vsync cfr                     (RTSP output, transcode only) Constant frame rate (even
//                                frame spacing at loop point); an encode-path operation
//                                ffmpeg rejects when combined with -c:v copy
// -max_interleave_delta 1000     (transport stream outputs except RTP) Bound how long the
//                                muxer waits to interleave the sparse KLV data stream
//                                against video/audio. Omitted for rtp_mpegts: its outer
//                                RTP muxer rejects the out-of-order timestamps that an
//                                early flush produces, so the default wait applies there.
// -bsf:d setts=dts=max(DTS,PREV_OUTDTS)
//                                (UDP output) Force copied data-stream DTS to stay monotonic
//                                after the video transcode retimes the program, and as a
//                                backstop at loop seams under passthrough. max() is a
//                                no-op while DTS are already monotonic. The comma is
//                                escaped as \, for ffmpeg's filter parser.
//
// STREAM RELIABILITY:
// -max_muxing_queue_size 1024    Prevent buffer overflows during encoding
//
// OUTPUTS (one per transport kind; see transports.rs for the URL of each):
// -f rtsp rtsp://localhost:<rtsp-port>/<stream-name>   Publish to MediaMTX (serves RTSP, HLS, RTMP)
// -rtsp_transport tcp            (RTSP output) Send RTP over the RTSP TCP connection. Over
//                                the default UDP, ffmpeg keeps streaming into the void if
//                                MediaMTX drops the session; over TCP the write fails,
//                                ffmpeg exits, and the supervisor's dead-process check
//                                restarts the stream.
// -f mpegts <url>                KLV-preserving MPEG-TS over UDP (?pkt_size=1316 keeps 7
//                                TS packets per datagram), multicast (&ttl=1 stays on the
//                                LAN), SRT (?mode=listener waits for the consumer), TCP
//                                (?listen=1) or HTTP (-listen 1, one client at a time)
// -f rtp_mpegts <url>            The same transport stream wrapped in RTP, plain or over
//                                SRTP (-srtp_out_suite/-srtp_out_params carry the cipher
//                                and the shared key)
// -f hls ...                     The transport stream cut into 2-second segments, 6 kept,
//                                old ones deleted; the API server serves the directory

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testsupport::{delivery_from_fixture, fixture, profile_from_fixture};

    fn spec(passthrough: bool) -> FFmpegCommandSettings {
        FFmpegCommandSettings {
            loop_count: -1,
            gop_frames: 30,
            frame_rate: None,
            passthrough,
            audio_copy: true,
            bitrate_cap: "2M".into(),
        }
    }

    fn rtsp_delivery() -> Delivery {
        Delivery {
            muxer: Muxer::Rtsp,
            carries_klv: false,
            ..Delivery::to_url("rtsp://localhost:8554/boats")
        }
    }

    fn position(argv: &[String], flag: &str) -> usize {
        argv.iter()
            .position(|argument| argument == flag)
            .unwrap_or_else(|| panic!("{flag} missing"))
    }

    #[test]
    fn looping_transcode_uses_software_decode_and_selected_encoder() {
        for delivery in [rtsp_delivery(), Delivery::to_url("udp://receiver:40002")] {
            let argv = build_argv(Path::new("boats.avi"), &spec(false), &delivery);
            let input_opts = &argv[..position(&argv, "-i")];
            assert!(!input_opts.contains(&"-c:v".to_string()));
            assert!(!argv.contains(&"-hwaccel".to_string()));
            assert_eq!(argv[position(&argv, "-stream_loop") + 1], "-1");
            assert_eq!(argv[position(&argv, "-c:v") + 1], "libx264");
            assert_eq!(spec(false).encoder_label(), "libx264 (software)");
        }
    }

    #[test]
    fn rtsp_maps_video_and_audio_only_while_transport_streams_carry_everything() {
        let rtsp = build_argv(Path::new("boats.avi"), &spec(true), &rtsp_delivery());
        assert_eq!(rtsp[position(&rtsp, "-c:v") + 1], "copy");
        assert!(!rtsp.contains(&"-copy_unknown".to_string()));
        assert_eq!(
            rtsp[rtsp.len() - 5..],
            strings(&[
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                "rtsp://localhost:8554/boats"
            ])
        );
        let udp = build_argv(
            Path::new("boats.avi"),
            &spec(true),
            &Delivery::to_url("udp://receiver:40002?pkt_size=1316"),
        );
        assert!(udp.contains(&"-copy_unknown".to_string()));
        assert_eq!(udp[position(&udp, "-c:d") + 1], "copy");
        assert_eq!(
            udp[udp.len() - 3..],
            strings(&["-f", "mpegts", "udp://receiver:40002?pkt_size=1316"])
        );
    }

    #[test]
    fn each_muxer_gets_its_own_tail() {
        let rtp = build_argv(
            Path::new("boats.avi"),
            &spec(false),
            &Delivery {
                muxer: Muxer::RtpMpegts,
                ..Delivery::to_url("rtp://receiver:40022")
            },
        );
        assert_eq!(
            rtp[rtp.len() - 3..],
            strings(&["-f", "rtp_mpegts", "rtp://receiver:40022"])
        );
        assert!(!rtp.contains(&"-max_interleave_delta".to_string()));
        assert!(
            build_argv(
                Path::new("boats.avi"),
                &spec(false),
                &Delivery::to_url("udp://receiver:40002")
            )
            .contains(&"-max_interleave_delta".to_string())
        );
        let http = build_argv(
            Path::new("boats.avi"),
            &spec(false),
            &Delivery {
                extra_args: strings(&["-listen", "1"]),
                ..Delivery::to_url("http://0.0.0.0:43000")
            },
        );
        assert_eq!(
            http[http.len() - 5..],
            strings(&["-listen", "1", "-f", "mpegts", "http://0.0.0.0:43000"])
        );
        let hls = build_argv(
            Path::new("boats.avi"),
            &spec(false),
            &Delivery {
                muxer: Muxer::Hls,
                ..Delivery::to_url("/app/hls/boats/index.m3u8")
            },
        );
        assert_eq!(hls[position(&hls, "-f") + 1], "hls");
        assert_eq!(hls[position(&hls, "-hls_segment_type") + 1], "mpegts");
        assert_eq!(
            hls[position(&hls, "-hls_segment_filename") + 1],
            "/app/hls/boats/seg%05d.ts"
        );
        assert_eq!(hls[hls.len() - 1], "/app/hls/boats/index.m3u8");
        assert!(hls.contains(&"-copy_unknown".to_string()));
    }

    #[test]
    fn mjpeg_is_transcoded_for_mpegts_ingest() {
        let profile = MediaProbeResult {
            video_codecs: Some(vec!["mjpeg".into()]),
            audio_codecs: Some(vec![]),
            audio_copyable: true,
            pix_fmt: Some("yuvj420p".into()),
            bitrate_bps: Some(4000000),
            frame_rate: Some(30.0),
            data_codecs: Some(vec![]),
        };
        let prepared = command_for_profile(
            Path::new("camera.avi"),
            &StreamChoices::default(),
            &Delivery::to_url("srt://127.0.0.1:8890?streamid=publish:camera"),
            &profile,
            None,
        );
        assert_eq!(prepared.effective_mode, VideoMode::Transcode);
        assert_eq!(prepared.ineligible_codecs, ["mjpeg"]);
        assert_eq!(prepared.passthrough_eligible, Some(false));
    }

    #[test]
    fn probe_reports_match_the_python_fixture() {
        for case in fixture("probe.json").as_array().unwrap() {
            let profile = if case["returncode"].as_i64() == Some(0) {
                parse_probe_report(case["stdout"].as_str().unwrap())
                    .unwrap_or(MediaProbeResult::UNKNOWN)
            } else {
                MediaProbeResult::UNKNOWN
            };
            assert_eq!(
                serde_json::to_value(&profile).unwrap(),
                case["profile"],
                "{}",
                case["name"]
            );
        }
    }

    #[test]
    fn prepared_commands_match_the_python_fixture() {
        let fixture = fixture("prepare.json");
        for case in fixture["cases"].as_array().unwrap() {
            let profile =
                profile_from_fixture(&fixture["profiles"][case["profile"].as_str().unwrap()]);
            let delivery =
                delivery_from_fixture(&fixture["deliveries"][case["delivery"].as_str().unwrap()]);
            let choices = StreamChoices {
                enabled: true,
                mode: case["choices"]["mode"].as_str().and_then(VideoMode::parse),
                loop_count: case["choices"]["loop_count"].as_i64().unwrap(),
                frame_rate: case["choices"]["frame_rate"].as_f64(),
                gop_frames: case["choices"]["gop_frames"].as_i64(),
            };
            let prepared = command_for_profile(
                Path::new("/videos/boats.mp4"),
                &choices,
                &delivery,
                &profile,
                case["max_video_bps"].as_i64(),
            );
            assert_eq!(
                serde_json::to_value(&prepared).unwrap(),
                case["prepared"],
                "{} {} {}",
                case["profile"],
                case["delivery"],
                case["choices"]
            );
        }
        for case in fixture["frame_rate_text"].as_array().unwrap() {
            let argv = build_argv(
                Path::new("/videos/a.mp4"),
                &FFmpegCommandSettings {
                    frame_rate: case["frame_rate"].as_f64(),
                    bitrate_cap: String::new(),
                    ..spec(false)
                },
                &Delivery::to_url("udp://r:1"),
            );
            assert_eq!(serde_json::to_value(&argv).unwrap(), case["argv"], "{case}");
        }
        for case in fixture["natural_gop_frames"].as_array().unwrap() {
            assert_eq!(
                natural_gop_frames(case["frame_rate"].as_f64()),
                case["gop_frames"].as_i64().unwrap(),
                "{case}"
            );
        }
    }

    #[test]
    fn shell_join_quotes_like_shlex() {
        assert_eq!(
            shell_join(&strings(&[
                "ffmpeg",
                "-i",
                "/v/a b.mp4",
                "srt://h:1?x=y",
                "it's",
                "",
                "a,b=c"
            ])),
            "ffmpeg -i '/v/a b.mp4' 'srt://h:1?x=y' 'it'\"'\"'s' '' a,b=c"
        );
    }

    #[test]
    fn general_format_matches_printf_g() {
        for (value, text) in [
            (12.0, "12"),
            (29.97, "29.97"),
            (23.976, "23.976"),
            (100.5, "100.5"),
            (0.1, "0.1"),
            (1e-7, "1e-07"),
            (1234567.0, "1.23457e+06"),
            (30000.0 / 1001.0, "29.97"),
        ] {
            assert_eq!(general_format(value), text);
        }
    }
}
