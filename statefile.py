"""The on-disk memory of the supervisor: what survives a container restart.

Answers one question — which per-stream choices and port assignments must
outlive the process — and knows nothing about running streams. streams.py
translates its registry to and from these records; this module only reads
and writes them.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass(frozen=True)
class PersistedOutput:
    """One output's desired state."""
    enabled: bool = True


@dataclass(frozen=True)
class PersistedStream:
    """A stream's choices. filename guards against a different file with the
    same sanitized name inheriting them."""
    filename: str
    mode: str | None = None
    loop_count: int = -1
    gop_frames: int = 30
    rtsp: PersistedOutput = PersistedOutput()
    udp: PersistedOutput = PersistedOutput()


@dataclass(frozen=True)
class PersistedState:
    # Both keyed by stream name: lookup by name is the only access pattern,
    # and the JSON file reads naturally as a name -> record table.
    udp_ports: dict = field(default_factory=dict)      # stream name -> UDP port
    streams: dict = field(default_factory=dict)        # stream name -> PersistedStream


def _output_from_json(data):
    return PersistedOutput(enabled=bool(data.get("enabled", True)))


def _stream_from_json(data):
    outputs = data.get("outputs", {})
    return PersistedStream(
        filename=data["filename"],
        mode=data.get("mode"),
        loop_count=int(data.get("loop_count", -1)),
        gop_frames=int(data.get("gop_frames", 30)),
        rtsp=_output_from_json(outputs.get("rtsp", {})),
        udp=_output_from_json(outputs.get("udp", {})),
    )


def load_state(path):
    """Read the state file; an absent or unreadable file is an empty state.

    Refusing to boot over a damaged state file would take every stream down
    for the sake of remembered settings, so damage is logged and forgotten.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return PersistedState()
    except OSError as error:
        log.warning("Cannot read state file %s: %s", path, error)
        return PersistedState()
    try:
        data = json.loads(raw)
        if data.get("version") != STATE_VERSION:
            log.warning("State file %s has version %r, expected %d; starting fresh",
                        path, data.get("version"), STATE_VERSION)
            return PersistedState()
        return PersistedState(
            udp_ports={name: int(port) for name, port in data.get("udp_ports", {}).items()},
            streams={name: _stream_from_json(record)
                     for name, record in data.get("streams", {}).items()},
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        log.warning("State file %s is unreadable (%s); starting fresh", path, error)
        return PersistedState()


def save_state(path, state):
    """Write the state atomically: a crash mid-write never truncates the file."""
    path = Path(path)
    document = {
        "version": STATE_VERSION,
        "udp_ports": dict(state.udp_ports),
        "streams": {
            name: {
                "filename": record.filename,
                "mode": record.mode,
                "loop_count": record.loop_count,
                "gop_frames": record.gop_frames,
                "outputs": {"rtsp": asdict(record.rtsp), "udp": asdict(record.udp)},
            }
            for name, record in state.streams.items()
        },
    }
    temp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as error:
        log.error("Cannot write state file %s: %s", path, error)
