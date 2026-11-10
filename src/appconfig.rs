//! Validated start-up configuration, loaded explicitly from the environment.

use std::collections::HashMap;
use std::fmt;
use std::path::PathBuf;

pub const LOG_LEVEL_NAMES: [&str; 4] = ["debug", "info", "warning", "error"];

/// Deployment mistakes: a missing or unusable start-up setting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

/// The process environment as a lookup table, so tests can pass an explicit one.
pub type Environment = HashMap<String, String>;

/// Return a required setting; missing or empty values are deployment errors.
pub fn require_env(name: &str, environ: &Environment) -> Result<String, ConfigError> {
    match environ.get(name) {
        Some(value) if !value.is_empty() => Ok(value.clone()),
        _ => Err(ConfigError(format!(
            "{name} environment variable is not set"
        ))),
    }
}

fn require_port(name: &str, environ: &Environment) -> Result<u16, ConfigError> {
    let text = require_env(name, environ)?;
    text.trim()
        .parse()
        .map_err(|_| ConfigError(format!("{name} must be a port number, not {text:?}")))
}

/// What the container needs before any stream exists; everything the operator
/// tunes lives in `RuntimeSettings`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub rtsp_port: u16,
    pub hls_port: u16,
    pub rtmp_port: u16,
    pub api_port: u16,
    pub log_level: String,
    pub videos_dir: PathBuf,
    pub state_path: PathBuf,
    pub hls_dir: PathBuf,
    pub ffmpeg_log_dir: PathBuf,
    pub host_name: String,
    pub host_lan_ip: String,
    pub srt_port: u16,
}

impl Config {
    /// Fixed container paths and the default SRT port; the ports and log level come from the caller.
    pub fn new(
        rtsp_port: u16,
        hls_port: u16,
        rtmp_port: u16,
        api_port: u16,
        log_level: &str,
    ) -> Result<Self, ConfigError> {
        if !LOG_LEVEL_NAMES.contains(&log_level) {
            return Err(ConfigError(format!(
                "LOG_LEVEL must be one of {}, not {log_level:?}",
                LOG_LEVEL_NAMES.join(", ")
            )));
        }
        Ok(Self {
            rtsp_port,
            hls_port,
            rtmp_port,
            api_port,
            log_level: log_level.to_string(),
            videos_dir: PathBuf::from("/app/videos"),
            state_path: PathBuf::from("/app/mounted/streams_config.json"),
            hls_dir: PathBuf::from("/app/hls"),
            ffmpeg_log_dir: PathBuf::from("/tmp/ffmpeg-logs"),
            host_name: String::new(),
            host_lan_ip: String::new(),
            srt_port: 8890,
        })
    }

    pub fn show_ffmpeg_output(&self) -> bool {
        self.log_level == "debug"
    }

    /// Read the start-up settings passed through from .env by Compose.
    pub fn from_env(environ: &Environment) -> Result<Self, ConfigError> {
        let mut config = Self::new(
            require_port("MEDIAMTX_RTSP_PORT", environ)?,
            require_port("MEDIAMTX_HLS_PORT", environ)?,
            require_port("MEDIAMTX_RTMP_PORT", environ)?,
            require_port("STREAM_API_PORT", environ)?,
            &require_env("LOG_LEVEL", environ)?.to_lowercase(),
        )?;
        config.srt_port = require_port("MEDIAMTX_SRT_PORT", environ)?;
        config.host_name = environ
            .get("AUTOSTREAM_HOSTNAME")
            .cloned()
            .unwrap_or_default();
        config.host_lan_ip = environ
            .get("AUTOSTREAM_LAN_IP")
            .cloned()
            .unwrap_or_default();
        Ok(config)
    }

    pub fn from_process_env() -> Result<Self, ConfigError> {
        Self::from_env(&std::env::vars().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn environ() -> Environment {
        [
            ("MEDIAMTX_RTSP_PORT", "8554"),
            ("MEDIAMTX_HLS_PORT", "8888"),
            ("MEDIAMTX_RTMP_PORT", "1935"),
            ("MEDIAMTX_SRT_PORT", "18890"),
            ("STREAM_API_PORT", "8080"),
            ("LOG_LEVEL", "info"),
        ]
        .into_iter()
        .map(|(name, value)| (name.to_string(), value.to_string()))
        .collect()
    }

    #[test]
    fn deployment_settings_load() {
        let config = Config::from_env(&environ()).unwrap();
        assert_eq!(config.rtsp_port, 8554);
        assert_eq!(config.srt_port, 18890);
        assert_eq!(config.log_level, "info");
        assert!(!config.show_ffmpeg_output());
        assert_eq!(
            (config.host_name.as_str(), config.host_lan_ip.as_str()),
            ("", "")
        );
        let mut with_hosts = environ();
        with_hosts.insert("AUTOSTREAM_HOSTNAME".into(), "publisher".into());
        with_hosts.insert("AUTOSTREAM_LAN_IP".into(), "192.168.1.20".into());
        with_hosts.insert("LOG_LEVEL".into(), "DEBUG".into());
        let config = Config::from_env(&with_hosts).unwrap();
        assert_eq!(
            (config.host_name.as_str(), config.host_lan_ip.as_str()),
            ("publisher", "192.168.1.20")
        );
        assert!(config.show_ffmpeg_output());
    }

    #[test]
    fn invalid_deployment_settings_fail_when_loaded() {
        for (field, value, message) in [
            (
                "MEDIAMTX_RTSP_PORT",
                "",
                "MEDIAMTX_RTSP_PORT environment variable is not set",
            ),
            (
                "MEDIAMTX_RTSP_PORT",
                "abc",
                "MEDIAMTX_RTSP_PORT must be a port number",
            ),
            ("LOG_LEVEL", "verbose", "LOG_LEVEL must be one of"),
        ] {
            let mut broken = environ();
            broken.insert(field.into(), value.into());
            let error = Config::from_env(&broken).unwrap_err();
            assert!(error.to_string().starts_with(message), "{error}");
        }
    }
}
