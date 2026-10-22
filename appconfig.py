"""Validated application configuration, loaded explicitly at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

LOG_LEVEL_NAMES = ("debug", "info", "warning", "error")


def require_env(name, environ=None):
    """Return a required setting; missing or empty values are deployment errors."""
    value = (os.environ if environ is None else environ).get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


def parse_bitrate(bitrate_str):
    """Parse bitrate string (e.g., '3M', '3000K', '3000000') to bits per second."""
    if not bitrate_str:
        return None
    bitrate_str = bitrate_str.strip().upper()
    if bitrate_str.endswith('M'):
        return int(float(bitrate_str[:-1]) * 1_000_000)
    elif bitrate_str.endswith('K'):
        return int(float(bitrate_str[:-1]) * 1_000)
    else:
        return int(bitrate_str)


@dataclass(frozen=True)
class Config:
    hostname: str
    rtsp_port: int
    hls_port: int
    api_port: int
    log_level: str
    output_host: str
    udp_base_port: int
    udp_last_port: int
    max_video_bitrate: str = ""
    videos_dir: Path = Path("/app/videos")
    state_path: Path = Path("/app/state/streams.json")
    ffmpeg_log_dir: Path = Path("/tmp/ffmpeg-logs")
    max_video_bps: int | None = field(init=False)

    def __post_init__(self):
        if self.log_level not in LOG_LEVEL_NAMES:
            raise RuntimeError(
                f"LOG_LEVEL must be one of {', '.join(LOG_LEVEL_NAMES)}, not {self.log_level!r}")
        if self.udp_last_port < self.udp_base_port:
            raise RuntimeError(
                f"UDP_LAST_PORT ({self.udp_last_port}) is below UDP_BASE_PORT ({self.udp_base_port})")
        object.__setattr__(self, "max_video_bps", parse_bitrate(self.max_video_bitrate))

    @property
    def show_ffmpeg_output(self):
        return self.log_level == "debug"

    @classmethod
    def from_env(cls, environ=None):
        """Read the settings passed through from .env by Compose."""
        environ = os.environ if environ is None else environ
        return cls(
            hostname=require_env("CONTAINER_NAME", environ),
            rtsp_port=int(require_env("MEDIAMTX_RTSP_PORT", environ)),
            hls_port=int(require_env("MEDIAMTX_HLS_PORT", environ)),
            api_port=int(require_env("STREAM_API_PORT", environ)),
            log_level=require_env("LOG_LEVEL", environ).lower(),
            output_host=require_env("OUTPUT_HOST", environ),
            udp_base_port=int(require_env("UDP_BASE_PORT", environ)),
            udp_last_port=int(require_env("UDP_LAST_PORT", environ)),
            max_video_bitrate=environ.get("MAX_VIDEO_BITRATE", ""),
        )
