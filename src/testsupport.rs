//! Shared fixtures and helpers for the behavioral tests.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;

use crate::ffmpegcmd::{Delivery, MediaProbeResult};
use crate::transports::Muxer;

/// A parity fixture recorded from the Python implementation.
pub fn fixture(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    let text =
        fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

static STATE_FILES: AtomicUsize = AtomicUsize::new(0);

/// A unique state file name inside a test's directory; the file does not exist yet.
pub fn state_path(directory: &tempfile::TempDir) -> PathBuf {
    let ordinal = STATE_FILES.fetch_add(1, Ordering::Relaxed);
    directory.path().join(format!("state-{ordinal}.json"))
}

/// Make a directory read-only so every write under it fails, or writable again.
pub fn block_writes(directory: &tempfile::TempDir, blocked: bool) {
    use std::os::unix::fs::PermissionsExt;
    let mode = if blocked { 0o500 } else { 0o700 };
    fs::set_permissions(directory.path(), fs::Permissions::from_mode(mode)).unwrap();
}

pub fn profile_from_fixture(document: &Value) -> MediaProbeResult {
    let names = |value: &Value| {
        value.as_array().map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
    };
    MediaProbeResult {
        video_codecs: names(&document["video_codecs"]),
        audio_codecs: names(&document["audio_codecs"]),
        audio_copyable: document["audio_copyable"].as_bool().unwrap_or(false),
        pix_fmt: document["pix_fmt"].as_str().map(str::to_string),
        bitrate_bps: document["bitrate_bps"].as_i64(),
        frame_rate: document["frame_rate"].as_f64(),
        data_codecs: names(&document["data_codecs"]),
    }
}

pub fn delivery_from_fixture(document: &Value) -> Delivery {
    let muxer = match document["muxer"].as_str().unwrap_or("mpegts") {
        "rtsp" => Muxer::Rtsp,
        "rtp_mpegts" => Muxer::RtpMpegts,
        "hls" => Muxer::Hls,
        _ => Muxer::Mpegts,
    };
    Delivery {
        url: document["url"].as_str().map(str::to_string),
        muxer,
        carries_klv: document["carries_klv"].as_bool().unwrap_or(true),
        extra_args: document["extra_args"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default(),
        resolve_host: document["resolve_host"].as_str().map(str::to_string),
        client_bound: document["client_bound"].as_bool().unwrap_or(false),
        blocker: document["blocker"].as_str().map(str::to_string),
        clamp_data_dts: document["clamp_data_dts"].as_bool().unwrap_or(true),
    }
}
