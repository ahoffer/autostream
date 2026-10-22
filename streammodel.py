"""Stream and output state, endpoint identity, and filename rules.

No registry, environment reads, or process launches live here. A Stream receives
its immutable configuration explicitly; its owner synchronizes mutations.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import ffmpegcmd
from appconfig import Config


@dataclass
class Output:
    """One of a stream's ffmpeg processes: rtsp or udp.

    enabled is the desired state — "supervision keeps this up" — and outlives
    the process, so a per-output Stop is remembered across a dead-process
    sweep and a container restart. completed marks a finite loop that ran to
    the end, which is not a failure to relaunch from. launched_argv is what
    the running process was actually started with, so the status can show it
    even if the generated command has since changed. crashes counts
    unexpected deaths since the output last ran stably and last_error keeps
    the tail of the log from the latest one (or the reason a launch was
    refused), so the UI can say "crashing" and show why instead of "running".
    """
    name: str
    enabled: bool = True
    process: subprocess.Popen | None = None
    stopping: bool = False
    completed: bool = False
    launched_argv: list | None = None
    started_at: float | None = None
    crashes: int = 0
    last_error: str | None = None

    @property
    def running(self):
        """True while the process is up and has not been asked to stop."""
        return self.process is not None and not self.stopping

    @property
    def occupied(self):
        """True while a process owns this output, including one still stopping."""
        return self.process is not None

    def mark_started(self, process, argv):
        self.process = process
        self.launched_argv = argv
        self.started_at = time.time()
        self.stopping = False
        self.completed = False

    def clear_process(self):
        self.process = None
        self.launched_argv = None
        self.started_at = None
        self.stopping = False

    def reset_health(self):
        """Forget past crashes: the user changed something, judge afresh."""
        self.crashes = 0
        self.last_error = None


@dataclass
class Stream:
    """One video file's stream slot: its identity, its endpoints, and its outputs.

    A slot exists for as long as the file is present in VIDEOS_DIR. filename
    is the on-disk basename the slot belongs to, kept so a second file
    sanitizing to the same name can't silently take the slot over. loop_count,
    mode, and gop_frames are the last requested values and deliberately
    outlive the processes; mode None means "no explicit request yet" so the
    codec-derived default applies until the user picks one; frame_rate None
    likewise means the file's own rate, and gop_frames None one second at the
    output rate. profile is the
    last probe of the file — it describes the file, not a process. udp_port is
    None when the configured range was already used up, which means RTSP/HLS
    only.
    """
    name: str
    filename: str
    udp_port: int | None
    config: Config = field(kw_only=True, repr=False)
    loop_count: int = -1
    mode: str | None = None
    gop_frames: int | None = None
    frame_rate: float | None = None
    profile: ffmpegcmd.FileProfile | None = None
    rtsp: Output = field(default_factory=lambda: Output("rtsp"))
    udp: Output = field(default_factory=lambda: Output("udp"))

    @property
    def video_path(self):
        """The file this slot streams. Derived, so it can't disagree with filename."""
        return str(self.config.videos_dir / self.filename)

    @property
    def outputs(self):
        return (self.rtsp, self.udp)

    def output(self, output_name):
        return self.rtsp if output_name == "rtsp" else self.udp

    @property
    def running(self):
        return any(output.running for output in self.outputs)

    @property
    def stopping(self):
        return any(output.stopping for output in self.outputs)

    def urls(self):
        """Return the copyable endpoint URLs for this stream.

        The udp entry is None when the slot has no port from the range.
        """
        return {
            "rtsp": f"rtsp://{self.config.hostname}:{self.config.rtsp_port}/{self.name}",
            "hls": f"http://{self.config.hostname}:{self.config.hls_port}/{self.name}/index.m3u8",
            "udp": f"udp://{self.config.output_host}:{self.udp_port}" if self.udp_port else None,
        }


def is_ignored(path):
    """Skip hidden files and README* so they don't get streamed as videos."""
    name = path.name
    return name.startswith('.') or name.lower().startswith('readme')


def sanitize_name(filepath):
    """Convert filename to valid stream name"""
    name = Path(filepath).stem
    name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    name = name.lower()
    name = re.sub(r'_+', '_', name)
    name = re.sub(r'-+', '-', name)
    name = name.strip('_-')
    return name


def stream_name_for(path):
    """The sanitized file stem, or an empty string for an invalid name."""
    return sanitize_name(path)

