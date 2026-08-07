"""Stream lifecycle: the video slots, their ffmpeg processes, and their endpoints.

Owns the config read from .env, the Stream entity, the registry of slots, and
every operation over them. Knows nothing about how it is driven: the supervisor
feeds it file events and the HTTP API calls the same operations, so neither
transport nor poll loop appears here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def require_env(name):
    """Return the env var's value, failing at boot if it is missing or empty.

    .env is the single source of truth for config; compose passes every value
    through, so a missing one is a deployment error, not a case to default.
    """
    value = os.getenv(name)
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


VIDEOS_DIR = Path("/app/videos")
STREAM_VIDEO_SCRIPT = "/app/stream-video.sh"
RTSP_PORT = int(require_env("MEDIAMTX_RTSP_PORT"))
HLS_PORT = int(require_env("MEDIAMTX_HLS_PORT"))
API_PORT = int(require_env("STREAM_API_PORT"))
LOG_LEVEL = require_env("LOG_LEVEL").lower()
LOG_LEVEL_NAMES = ("debug", "info", "warning", "error")
if LOG_LEVEL not in LOG_LEVEL_NAMES:
    raise RuntimeError(
        f"LOG_LEVEL must be one of {', '.join(LOG_LEVEL_NAMES)}, not {LOG_LEVEL!r}")
# Documented side effect of debug: the child ffmpeg processes keep their stdio
# instead of writing to per-stream log files under FFMPEG_LOG_DIR.
SHOW_FFMPEG_OUTPUT = LOG_LEVEL == "debug"
# One log file per stream, truncated at each start so it holds only the current
# run. When a process dies unexpectedly, the tail is replayed into our log —
# otherwise the ffmpeg error explaining the death would be lost.
FFMPEG_LOG_DIR = Path("/tmp/ffmpeg-logs")
FFMPEG_LOG_DIR.mkdir(exist_ok=True)
FFMPEG_LOG_TAIL_BYTES = 4096
MAX_VIDEO_BITRATE = os.getenv("MAX_VIDEO_BITRATE", "")  # empty disables the cap
MAX_VIDEO_BPS = parse_bitrate(MAX_VIDEO_BITRATE)  # malformed values fail here, at boot

# Passthrough policy. MediaMTX only carries H.264/H.265, so nothing else may be
# stream-copied. HEVC is eligible but not the default: RTSP consumers handle it,
# but browser HLS playback of HEVC is spotty, so it takes an explicit request.
PASSTHROUGH_CODECS = frozenset({"h264", "hevc"})
PASSTHROUGH_DEFAULT_CODECS = frozenset({"h264"})


def default_mode(video_codecs):
    """Codec-derived default mode: all-H.264 files pass through, all else transcodes.

    None or empty (never probed, probe failed, or no video track) defaults to
    transcode — the mode that works for everything.
    """
    if video_codecs and all(c in PASSTHROUGH_DEFAULT_CODECS for c in video_codecs):
        return "passthrough"
    return "transcode"


def passthrough_eligible(video_codecs):
    """True when every video track can ride MediaMTX unencoded.

    All tracks matter because stream-video.sh maps every video track (-map 0:v?).
    """
    return bool(video_codecs) and all(c in PASSTHROUGH_CODECS for c in video_codecs)

# UDP MPEG-TS output carries KLV/data streams that RTSP/HLS (via MediaMTX) drop.
# Each stream is pushed to OUTPUT_HOST on its own port (UDP_BASE_PORT + slot).
# OUTPUT_HOST is a reachable consumer or a multicast group.
OUTPUT_HOST = require_env("OUTPUT_HOST")
UDP_BASE_PORT = int(require_env("UDP_BASE_PORT"))
UDP_LAST_PORT = int(require_env("UDP_LAST_PORT"))
if UDP_LAST_PORT < UDP_BASE_PORT:
    raise RuntimeError(
        f"UDP_LAST_PORT ({UDP_LAST_PORT}) is below UDP_BASE_PORT ({UDP_BASE_PORT})")

HOSTNAME = require_env("CONTAINER_NAME")


@dataclass
class Stream:
    """One video file's stream slot: its identity, its endpoints, and its process.

    A slot exists for as long as the file is present in VIDEOS_DIR, so "known
    but not currently streaming" is process is None rather than a separate
    collection. filename is the on-disk basename the slot belongs to, kept so a
    second file sanitizing to the same name can't silently take the slot over.
    loop_count is the last requested value and deliberately outlives the
    process, so a stop/start keeps the user's choice; mode works the same way,
    with None meaning "no explicit request yet" so the codec-derived default
    applies until the user picks one. video_codecs/audio_codecs are the last
    start's probe of the file — they describe the file, not the process, so
    clear_process leaves them alone. udp_port is None when the configured range
    was already used up, which means RTSP/HLS only.
    """
    name: str
    filename: str
    udp_port: int | None
    loop_count: int = -1
    process: subprocess.Popen | None = None
    udp_enabled: bool = False
    stopping: bool = False
    mode: str | None = None
    video_codecs: tuple | None = None
    audio_codecs: tuple | None = None
    passthrough_active: bool = False

    @property
    def video_path(self):
        """The file this slot streams. Derived, so it can't disagree with filename."""
        return str(VIDEOS_DIR / self.filename)

    @property
    def running(self):
        """True while the stream is up and has not been asked to stop."""
        return self.process is not None and not self.stopping

    @property
    def occupied(self):
        """True while a process owns this slot, including one still stopping."""
        return self.process is not None

    def mark_started(self, process, loop_count, mode, udp_enabled, passthrough_active):
        """Transition the slot to running under a freshly launched process."""
        self.process = process
        self.loop_count = loop_count
        self.mode = mode
        self.udp_enabled = udp_enabled
        self.passthrough_active = passthrough_active
        self.stopping = False

    def clear_process(self):
        """Transition the slot back to idle once its process is gone."""
        self.process = None
        self.udp_enabled = False
        self.passthrough_active = False
        self.stopping = False

    def urls(self):
        """Return the copyable endpoint URLs for this stream.

        The udp entry is None when the slot has no port from the range.
        """
        return {
            "rtsp": f"rtsp://{HOSTNAME}:{RTSP_PORT}/{self.name}",
            "hls": f"http://{HOSTNAME}:{HLS_PORT}/{self.name}/index.m3u8",
            "udp": f"udp://{OUTPUT_HOST}:{self.udp_port}" if self.udp_port else None,
        }


# Shared state. Mutations and reads MUST hold _state_lock — the API server runs in
# threads and the poll loop runs in the main thread, both touching this state.
_state_lock = threading.RLock()
_streams_by_name = {}          # stream_name -> Stream, one entry per video file present

# UDP port allocator. Separate from Stream on purpose: an assignment lasts for the
# whole process lifetime, outliving the slot, so a video that is removed and put
# back keeps the udp:// URL a downstream consumer already registered.
_udp_ports = {}               # stream_name -> UDP port for the KLV MPEG-TS feed
_next_udp_port = UDP_BASE_PORT  # next port to hand out; protected by _state_lock

_output_host_warned = False   # so we log the "unresolved OUTPUT_HOST" warning once
_output_reachable = False     # cached OUTPUT_HOST reachability; the poll loop refreshes it so the status API never blocks on DNS


def refresh_output_reachable():
    """Refresh the cached OUTPUT_HOST reachability.

    ffmpeg aborts the whole process (taking the RTSP output down with it) if the
    UDP destination host is unresolvable. So when the consumer (for example the
    cx-search video-streaming service) is not on the network, streams launch
    RTSP/HLS only rather than breaking playback. A numeric IP or multicast group
    always resolves, so this only trips on a missing hostname.

    gethostbyname blocks for the resolver timeout when the host is down, so it
    must stay off the request path: status reads and stream starts sample the
    cache, and the poll loop refreshes it every tick.
    """
    global _output_reachable, _output_host_warned
    try:
        socket.gethostbyname(OUTPUT_HOST)
        _output_host_warned = False
        _output_reachable = True
    except OSError:
        if not _output_host_warned:
            log.warning("OUTPUT_HOST %r does not resolve; streaming RTSP/HLS only, "
                        "no KLV/UDP feed until it becomes reachable", OUTPUT_HOST)
            _output_host_warned = True
        _output_reachable = False


def _udp_port_for(stream_name):
    """Return the stream's UDP port, assigning the next free one on first use.

    Returns None once the configured range is used up; the caller streams
    RTSP/HLS only, the same degraded mode an unreachable OUTPUT_HOST produces.

    Ports are stable for the process lifetime so a stopped/started stream keeps
    the same udp:// URL. That means an assignment is never released, so a run
    that cycles through more than the range holds will exhaust it — the range is
    a contract with the downstream consumer, and quietly reusing a port a
    consumer still has registered would be worse than running without one.
    Must be called while holding _state_lock.
    """
    global _next_udp_port
    port = _udp_ports.get(stream_name)
    if port is None:
        if _next_udp_port > UDP_LAST_PORT:
            log.warning("UDP port range %d-%d is used up; %s streams RTSP/HLS only",
                        UDP_BASE_PORT, UDP_LAST_PORT, stream_name)
            return None
        port = _next_udp_port
        _next_udp_port += 1
        _udp_ports[stream_name] = port
    return port


def _first_int(text):
    """First integer-valued line in ffprobe output, or None.

    ffprobe reports "N/A" (sometimes more than one line of it) when a bitrate is
    unknown, so we can't just int() the whole output — pick the first real number.
    """
    for line in text.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def get_video_bitrate(video_path):
    """Return the video bitrate in bits per second via ffprobe, or None.

    Tries the video stream first, then the container format. An "N/A" (unknown
    bitrate, common for MPEG-TS) is not an error — it just falls through to None.
    """
    queries = (
        ["-select_streams", "v:0", "-show_entries", "stream=bit_rate"],
        ["-show_entries", "format=bit_rate"],
    )
    for query in queries:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", *query,
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("Error probing bitrate for %s: %s", Path(video_path).name, e)
            return None
        bitrate = _first_int(result.stdout)
        if bitrate is not None:
            return bitrate
    return None


def get_stream_codecs(video_path):
    """Return (video_codecs, audio_codecs, audio_copyable) for the file, or
    (None, None, False) when the probe fails — callers treat unknown as
    transcode + encode, the combination that works for everything.

    The codec tuples are index-ordered and cover every track, because
    stream-video.sh maps every track. audio_copyable is True only when each
    audio track is AAC that already carries global headers (extradata): ADTS
    AAC — the packaging MPEG-TS sources use — has none, and ffmpeg's RTSP muxer
    needs the headers at SDP time, before the first packet flows, so the
    aac_adtstoasc bitstream filter cannot rescue a copy. Such audio is
    re-encoded instead.

    JSON output rather than csv: csv emits fields in ffprobe's internal order,
    not the requested order, and MPEG-TS inputs repeat streams under a programs
    section — json.loads()["streams"] dodges both traps.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=index,codec_type,codec_name,extradata_size",
             "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Error probing codecs for %s: %s", Path(video_path).name, e)
        return None, None, False
    if result.returncode != 0:
        return None, None, False
    try:
        entries = json.loads(result.stdout).get("streams", [])
    except ValueError:
        return None, None, False
    ordered = sorted(entries, key=lambda s: s.get("index", 0))
    video = tuple(s.get("codec_name", "") for s in ordered if s.get("codec_type") == "video")
    audio_streams = [s for s in ordered if s.get("codec_type") == "audio"]
    audio = tuple(s.get("codec_name", "") for s in audio_streams)
    audio_copyable = all(s.get("codec_name") == "aac" and s.get("extradata_size", 0) > 0
                         for s in audio_streams)
    return video, audio, audio_copyable


def get_bitrate_cap(video_path):
    """Return MAX_VIDEO_BITRATE if the video exceeds it, else empty string.

    The cap value is expanded into concrete ffmpeg flags by stream-video.sh,
    which owns all encode syntax.
    """
    if not MAX_VIDEO_BPS:
        return ""

    video_bps = get_video_bitrate(video_path)
    if not video_bps:
        log.debug("Could not detect bitrate for %s, no limit applied", Path(video_path).name)
        return ""

    video_mbps = video_bps / 1_000_000
    if video_bps > MAX_VIDEO_BPS:
        log.debug("Bitrate %.1fM exceeds max %s for %s; applying cap",
                  video_mbps, MAX_VIDEO_BITRATE, Path(video_path).name)
        return MAX_VIDEO_BITRATE
    else:
        log.debug("Bitrate %.1fM within max %s, no limit applied", video_mbps, MAX_VIDEO_BITRATE)
        return ""


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


def _ffmpeg_log_path(stream_name):
    return FFMPEG_LOG_DIR / f"{stream_name}.log"


def _log_ffmpeg_tail(stream_name):
    """Replay the end of a stream's ffmpeg log after an unexpected death.

    ffmpeg output goes to a per-stream file rather than the console (except in
    debug mode, where it was already visible), so without this the reason a
    stream died would be invisible.
    """
    if SHOW_FFMPEG_OUTPUT:
        return
    try:
        with open(_ffmpeg_log_path(stream_name), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - FFMPEG_LOG_TAIL_BYTES))
            tail = f.read().decode(errors="replace").strip()
    except OSError as e:
        log.warning("No ffmpeg log for %s: %s", stream_name, e)
        return
    if tail:
        log.warning("ffmpeg output for %s before it died:\n%s", stream_name, tail)


def wait_for_mediamtx():
    log.info("Waiting for MediaMTX to be available on port %d...", RTSP_PORT)
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("localhost", RTSP_PORT))
                break
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(1)
    log.info("MediaMTX is ready")


def start_stream(stream_name, loop_count=None, mode=None, log_start=True):
    """Start a claimed stream slot.

    loop_count None keeps the last requested value; mode None keeps the last
    requested mode (codec default when the user never picked one). Passthrough
    eligibility is re-derived from a fresh probe on every start, so the script
    only ever receives a vetted mode — a replaced file or failed probe degrades
    to transcode instead of launching a command ffmpeg cannot run.
    """
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            log.warning("Cannot start unclaimed stream: %s", stream_name)
            return False
        if stream.occupied:
            log.warning("Stream already %s: %s",
                        "stopping" if stream.stopping else "running", stream_name)
            return False
        video_path = stream.video_path
        udp_port = stream.udp_port
        if loop_count is None:
            loop_count = stream.loop_count
        if mode is None:
            mode = stream.mode

    # ffprobe can take time — run the probes outside the lock so we don't stall
    # other threads on I/O.
    video_codecs, audio_codecs, audio_copy = get_stream_codecs(video_path)
    requested_mode = mode or default_mode(video_codecs)
    passthrough = requested_mode == "passthrough" and passthrough_eligible(video_codecs)
    # Copied packets can't honor a cap, so don't even probe the bitrate.
    bitrate_cap = "" if passthrough else get_bitrate_cap(video_path)
    # No port from the range is the same outcome as an unreachable consumer:
    # publish RTSP/HLS and leave the KLV feed off.
    udp_enabled = _output_reachable and udp_port is not None
    udp_target = f"{OUTPUT_HOST}:{udp_port}" if udp_enabled else ""

    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None or stream.occupied:
            # Another thread claimed or started the slot while we were probing.
            log.warning("Stream changed while starting: %s", stream_name)
            return False

        try:
            cmd = [STREAM_VIDEO_SCRIPT, video_path, stream_name, str(loop_count),
                   bitrate_cap, udp_target,
                   "passthrough" if passthrough else "transcode",
                   "copy" if audio_copy else "encode"]
            if SHOW_FFMPEG_OUTPUT:
                process = subprocess.Popen(cmd)
            else:
                # The child keeps its own fd, so closing ours right away is safe.
                with open(_ffmpeg_log_path(stream_name), "wb") as ffmpeg_log:
                    process = subprocess.Popen(cmd, stdout=ffmpeg_log, stderr=ffmpeg_log)
        except Exception as e:
            log.error("Failed to start stream %s: %s", stream_name, e)
            return False

        stream.video_codecs = video_codecs
        stream.audio_codecs = audio_codecs
        stream.mark_started(process, loop_count, mode=mode,
                            udp_enabled=udp_enabled, passthrough_active=passthrough)

    if log_start:
        udp_status = "UDP active" if udp_target else "UDP disabled"
        log.info("Started stream: %s video=%r mode=%s audio=%s udp=%s udp_url=%s",
                 stream_name, Path(video_path).name,
                 "passthrough" if passthrough else "transcode",
                 "copy" if audio_copy else "aac",
                 udp_status, stream.urls()["udp"])
    return True


def stop_stream(stream_name):
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None or not stream.occupied:
            log.warning("Stream not running: %s", stream_name)
            return False
        process = stream.process
        should_terminate = not stream.stopping
        stream.stopping = True

    try:
        if should_terminate:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if not should_terminate:
                log.warning("Stream still stopping: %s", stream_name)
                return True
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Better to leak a wedged process than to hang the supervisor forever
                # (can happen on stuck NFS/CIFS reads inside ffmpeg). Keep the stream
                # slot blocked so a second ffmpeg process can't reuse the same output.
                log.error("Stream %s did not die after SIGKILL; leaking pid %d",
                          stream_name, process.pid)
                return True
        with _state_lock:
            current = _streams_by_name.get(stream_name)
            if current is not None and current.process is process:
                current.clear_process()
        log.info("Stopped stream: %s", stream_name)
    except Exception as e:
        log.error("Error stopping stream %s: %s", stream_name, e)

    return True


def restart_stream(stream_name, loop_count=None, mode=None):
    """Relaunch a stream: stop its current process if it has one, then start it.

    loop_count and mode None keep the last requested values.
    """
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        occupied = stream is not None and stream.occupied
    if occupied:
        stop_stream(stream_name)
    return start_stream(stream_name, loop_count, mode)


def stream_exists(stream_name):
    """True if a slot is registered under this name."""
    with _state_lock:
        return stream_name in _streams_by_name


def stop_all_streams():
    """Stop every slot that currently owns a process."""
    with _state_lock:
        names = [s.name for s in _streams_by_name.values() if s.occupied]
    for name in names:
        stop_stream(name)


def start_all_streams():
    """Start every idle slot, keeping each one's last requested loop count."""
    with _state_lock:
        names = [s.name for s in _streams_by_name.values() if not s.occupied]
    for name in names:
        start_stream(name)


def get_stream_status():
    """Get status of all streams"""
    result = []
    # Cached reachability (the poll loop refreshes it; never DNS on the API path).
    # A running feed reads active only while the consumer resolves — ffmpeg keeps
    # pushing UDP into the void otherwise, so the start-time flag alone would lie.
    reachable = _output_reachable
    with _state_lock:
        for stream in _streams_by_name.values():
            urls = stream.urls()
            udp_active = stream.running and stream.udp_enabled and reachable
            if udp_active:
                udp_reason = None
            elif stream.udp_port is None:
                # Permanent for this slot, so report it ahead of the transient reasons.
                udp_reason = f"no free UDP port in {UDP_BASE_PORT}-{UDP_LAST_PORT}"
            elif not stream.running:
                udp_reason = "stream stopped"
            elif not reachable:
                udp_reason = f"{OUTPUT_HOST} unreachable"
            else:
                udp_reason = "starting"
            requested_mode = stream.mode or default_mode(stream.video_codecs)
            passthrough_active = stream.running and stream.passthrough_active
            if requested_mode != "passthrough" or passthrough_active:
                passthrough_reason = None
            elif stream.video_codecs is not None and not passthrough_eligible(stream.video_codecs):
                # Permanent for this file, so report it ahead of the transient reasons.
                blocked = sorted({c for c in stream.video_codecs if c not in PASSTHROUGH_CODECS})
                passthrough_reason = (f"video codec {', '.join(blocked)} cannot pass through "
                                      f"(H.264/H.265 only)")
            elif not stream.running:
                passthrough_reason = "stream stopped"
            elif stream.video_codecs is None:
                passthrough_reason = "codec probe failed; transcoding"
            else:
                passthrough_reason = "starting"
            result.append({
                "name": stream.name,
                "running": stream.running,
                "stopping": stream.stopping,
                "loop_count": stream.loop_count,
                "mode": requested_mode,
                "passthrough_active": passthrough_active,
                "passthrough_reason": passthrough_reason,
                "rtsp_url": urls["rtsp"],
                "hls_url": urls["hls"],
                "udp_url": urls["udp"],
                "udp_active": udp_active,
                "udp_reason": udp_reason,
            })
    return result


def _claim_slot(stream_name, filename):
    """Register (or refresh) the slot binding stream_name to filename.

    Returns True if the slot belongs to this filename after the call, False if
    there was a collision with a different file (caller should skip).
    Must be called under _state_lock.
    """
    stream = _streams_by_name.get(stream_name)
    if stream is None:
        _streams_by_name[stream_name] = Stream(
            name=stream_name,
            filename=filename,
            udp_port=_udp_port_for(stream_name),
        )
        return True
    if stream.filename != filename:
        log.warning("Stream name collision: %s already owned by %r; skipping %r "
                    "(after removing the owner, touch this file or restart to stream it)",
                    stream_name, stream.filename, filename)
        return False
    return True


def handle_create(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name):
            return
    log.info("Video added: %s", path.name)
    start_stream(stream_name)


def handle_delete(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)

    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None or stream.filename != path.name:
            # Slot belongs to a different file (or never existed) — don't touch it.
            owner = stream.filename if stream is not None else None
            log.debug("Ignoring delete of %s; slot %s owned by %r", path.name, stream_name, owner)
            return
        was_occupied = stream.occupied

    log.info("Video removed: %s", path.name)
    if was_occupied:
        stop_stream(stream_name)
    with _state_lock:
        _streams_by_name.pop(stream_name, None)


def handle_modify(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name):
            return
        running = _streams_by_name[stream_name].running

    log.info("Video updated: %s", path.name)
    if running:
        restart_stream(stream_name)


def cleanup_dead_processes():
    with _state_lock:
        snapshot = [(s.name, s.process) for s in _streams_by_name.values() if s.occupied]

    dead = []
    for stream_name, process in snapshot:
        if process.poll() is not None:
            dead.append((stream_name, process))

    if dead:
        by_exit_code = {}
        for stream_name, process in dead:
            by_exit_code.setdefault(process.returncode, []).append(stream_name)
        for exit_code, stream_names in by_exit_code.items():
            log.info("Processes ended (exit code %s): %s",
                     exit_code, ", ".join(sorted(stream_names)))

    restarted = []
    for stream_name, process in dead:
        with _state_lock:
            current = _streams_by_name.get(stream_name)
            # If a concurrent /start replaced our entry with a fresh process,
            # don't orphan it — leave the new process alone.
            if current is None or current.process is not process:
                continue
            stopping = current.stopping
            loop_count = current.loop_count
            video_path = current.video_path
            current.clear_process()

        if stopping:
            continue

        if loop_count != -1 and process.returncode == 0:
            log.info("Stream completed: %s", stream_name)
            continue

        _log_ffmpeg_tail(stream_name)

        try:
            if not Path(video_path).exists():
                log.warning("Cannot restart stream %s: file missing", stream_name)
                continue
        except OSError as e:
            log.warning("Cannot restart stream %s: %s", stream_name, e)
            continue
        if not start_stream(stream_name, loop_count, log_start=False):
            log.error("Failed to restart stream: %s", stream_name)
            continue
        restarted.append(stream_name)

    if restarted:
        log.info("Restarted streams: %s", ", ".join(sorted(restarted)))


def recover_udp_outputs():
    """Restart running streams whose UDP feed is off, once OUTPUT_HOST resolves again.

    Reachability is only sampled when a stream starts, so one that came up
    before the consumer was on the network stays RTSP/HLS-only until restarted.
    Restarting re-samples the cache and wires the UDP output back in.

    Slots with no port from the range are skipped: restarting them would never
    turn the feed on, so they would be relaunched on every poll forever.
    """
    if not _output_reachable:
        return
    with _state_lock:
        pending = [s.name for s in _streams_by_name.values()
                   if s.running and not s.udp_enabled and s.udp_port is not None]
    for name in pending:
        restart_stream(name)

