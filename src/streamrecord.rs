//! The saved shape of one stream: which file, transport and port carry it, and the user's choices.

use serde::{Deserialize, Serialize};

/// Passthrough copies the source video; transcode re-encodes it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VideoMode {
    Passthrough,
    Transcode,
}

impl VideoMode {
    pub fn as_str(self) -> &'static str {
        match self {
            VideoMode::Passthrough => "passthrough",
            VideoMode::Transcode => "transcode",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "passthrough" => Some(VideoMode::Passthrough),
            "transcode" => Some(VideoMode::Transcode),
            _ => None,
        }
    }
}

/// Which file a stream plays and which transport and port carry it; saved beside the choices.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StreamBinding {
    pub filename: String,
    pub transport: String,
    pub port: Option<u16>,
}

/// The user's choices and their defaults in one place.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StreamChoices {
    pub enabled: bool,
    pub mode: Option<VideoMode>,
    pub loop_count: i64,
    pub frame_rate: Option<f64>,
    pub gop_frames: Option<i64>,
}

impl Default for StreamChoices {
    fn default() -> Self {
        Self {
            enabled: true,
            mode: None,
            loop_count: -1,
            frame_rate: None,
            gop_frames: None,
        }
    }
}

/// What the state file keeps per stream, in its saved key order.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StreamRecord {
    pub filename: String,
    pub transport: String,
    #[serde(default)]
    pub port: Option<u16>,
    pub enabled: bool,
    #[serde(default)]
    pub mode: Option<VideoMode>,
    pub loop_count: i64,
    #[serde(default)]
    pub frame_rate: Option<f64>,
    #[serde(default)]
    pub gop_frames: Option<i64>,
}

impl StreamRecord {
    pub fn new(binding: &StreamBinding, choices: &StreamChoices) -> Self {
        Self {
            filename: binding.filename.clone(),
            transport: binding.transport.clone(),
            port: binding.port,
            enabled: choices.enabled,
            mode: choices.mode,
            loop_count: choices.loop_count,
            frame_rate: choices.frame_rate,
            gop_frames: choices.gop_frames,
        }
    }

    pub fn binding(&self) -> StreamBinding {
        StreamBinding {
            filename: self.filename.clone(),
            transport: self.transport.clone(),
            port: self.port,
        }
    }

    /// Only the user's choices come back from state; delivery follows the current settings.
    pub fn choices(&self) -> StreamChoices {
        StreamChoices {
            enabled: self.enabled,
            mode: self.mode,
            loop_count: self.loop_count,
            frame_rate: self.frame_rate,
            gop_frames: self.gop_frames,
        }
    }
}
