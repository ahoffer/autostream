"""Validated start-up configuration, loaded explicitly from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOG_LEVEL_NAMES = ("debug", "info", "warning", "error")


def require_env(name, environ=None):
    """Return a required setting; missing or empty values are deployment errors."""
    value = (os.environ if environ is None else environ).get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


@dataclass(frozen=True)
class Config:
    """What the container needs before any stream exists; everything the operator tunes lives in RuntimeSettings."""
    rtsp_port: int
    hls_port: int
    rtmp_port: int
    api_port: int
    log_level: str
    videos_dir: Path = Path("/app/videos")
    state_path: Path = Path("/app/mounted/streams_config.json")
    hls_dir: Path = Path("/app/hls")
    ffmpeg_log_dir: Path = Path("/tmp/ffmpeg-logs")
    host_name: str = ''
    host_lan_ip: str = ''

    def __post_init__(self):
        if self.log_level not in LOG_LEVEL_NAMES:
            raise RuntimeError(
                f"LOG_LEVEL must be one of {', '.join(LOG_LEVEL_NAMES)}, not {self.log_level!r}")

    @property
    def show_ffmpeg_output(self):
        return self.log_level == "debug"

    @classmethod
    def from_env(cls, environ=None):
        """Read the start-up settings passed through from .env by Compose."""
        environ = os.environ if environ is None else environ
        return cls(
            rtsp_port=int(require_env("MEDIAMTX_RTSP_PORT", environ)),
            hls_port=int(require_env("MEDIAMTX_HLS_PORT", environ)),
            rtmp_port=int(require_env("MEDIAMTX_RTMP_PORT", environ)),
            api_port=int(require_env("STREAM_API_PORT", environ)),
            log_level=require_env("LOG_LEVEL", environ).lower(),
            host_name=environ.get('AUTOSTREAM_HOSTNAME', ''),
            host_lan_ip=environ.get('AUTOSTREAM_LAN_IP', ''),
        )
