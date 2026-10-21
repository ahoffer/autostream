"""Stream lifecycle: the video slots, their per-output ffmpeg processes, and
their endpoints.

Owns the config read from .env, the Stream and Output entities, the registry
of slots, and every operation over them. Knows nothing about how it is driven:
the supervisor feeds it file events and the HTTP API calls the same
operations, so neither transport nor poll loop appears here. ffmpegcmd owns
the ffmpeg syntax; statefile owns the on-disk memory.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import ffmpegcmd
import statefile

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
STATE_PATH = Path("/app/state/streams.json")
RTSP_PORT = int(require_env("MEDIAMTX_RTSP_PORT"))
HLS_PORT = int(require_env("MEDIAMTX_HLS_PORT"))
API_PORT = int(require_env("STREAM_API_PORT"))
LOG_LEVEL = require_env("LOG_LEVEL").lower()
LOG_LEVEL_NAMES = ("debug", "info", "warning", "error")
if LOG_LEVEL not in LOG_LEVEL_NAMES:
    raise RuntimeError(
        f"LOG_LEVEL must be one of {', '.join(LOG_LEVEL_NAMES)}, not {LOG_LEVEL!r}")
# Documented side effect of debug: the child ffmpeg processes keep their stdio
# instead of writing to per-output log files under FFMPEG_LOG_DIR.
SHOW_FFMPEG_OUTPUT = LOG_LEVEL == "debug"
# One log file per output, truncated at each start so it holds only the current
# run. When a process dies unexpectedly, the tail is replayed into our log —
# otherwise the ffmpeg error explaining the death would be lost.
FFMPEG_LOG_DIR = Path("/tmp/ffmpeg-logs")
FFMPEG_LOG_DIR.mkdir(exist_ok=True)
FFMPEG_LOG_TAIL_BYTES = 4096
MAX_VIDEO_BITRATE = os.getenv("MAX_VIDEO_BITRATE", "")  # empty disables the cap
MAX_VIDEO_BPS = parse_bitrate(MAX_VIDEO_BITRATE)  # malformed values fail here, at boot

# Passthrough policy: the codecs MediaMTX carries over RTSP unencoded. Only
# H.264 passes through by default: HLS through MediaMTX and browsers need it,
# while HEVC, MPEG-2, MPEG-4 part 2 and MJPEG reach RTSP consumers only (the
# way a real camera with that encoder would), so they take an explicit request.
PASSTHROUGH_CODECS = frozenset({"h264", "hevc", "mpeg2video", "mpeg4", "mjpeg"})
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

    All tracks matter because the generated commands map every video track.
    """
    return bool(video_codecs) and all(c in PASSTHROUGH_CODECS for c in video_codecs)


def bitrate_cap_for(bitrate_bps):
    """MAX_VIDEO_BITRATE when the video exceeds it, else empty string.

    An unknown bitrate (None) gets no cap: there is nothing to compare against.
    """
    if MAX_VIDEO_BPS and bitrate_bps and bitrate_bps > MAX_VIDEO_BPS:
        return MAX_VIDEO_BITRATE
    return ""


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
        return str(VIDEOS_DIR / self.filename)

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
            "rtsp": f"rtsp://{HOSTNAME}:{RTSP_PORT}/{self.name}",
            "hls": f"http://{HOSTNAME}:{HLS_PORT}/{self.name}/index.m3u8",
            "udp": f"udp://{OUTPUT_HOST}:{self.udp_port}" if self.udp_port else None,
        }


# Shared state. Mutations and reads MUST hold _state_lock — the API server runs in
# threads and the poll loop runs in the main thread, both touching this state.
_state_lock = threading.RLock()
_streams_by_name = {}          # stream_name -> Stream, one entry per video file present
# Remembered choices of streams whose file is currently absent, so a re-copied
# file gets its mode, loop, and commands back. Merged into the state file.
_retained_streams = {}         # stream_name -> statefile.PersistedStream

# UDP port allocator. Separate from Stream on purpose: an assignment lasts for
# the whole process lifetime and is persisted, outliving the slot, so a video
# that is removed and put back keeps the udp:// URL a downstream consumer
# already registered.
_udp_ports = {}               # stream_name -> UDP port for the KLV MPEG-TS feed
_next_udp_port = UDP_BASE_PORT  # next port to hand out; protected by _state_lock

_output_host_warned = False   # so we log the "unresolved OUTPUT_HOST" warning once
_output_reachable = False     # cached OUTPUT_HOST reachability; the poll loop refreshes it so the status API never blocks on DNS


def load_persisted_state():
    """Seed the registry's memory from the state file. Call once, before the
    first file events: they claim slots, which assigns ports."""
    global _next_udp_port
    state = statefile.load_state(STATE_PATH)
    with _state_lock:
        _udp_ports.update({name: port for name, port in state.udp_ports.items() if name})
        _retained_streams.update({name: record for name, record in state.streams.items() if name})
        if _udp_ports:
            _next_udp_port = max(UDP_BASE_PORT, max(_udp_ports.values()) + 1)
    if state.streams:
        log.debug("Loaded settings for %d stream(s) from %s", len(state.streams), STATE_PATH)


def _persisted_stream(stream, removed_at=None):
    return statefile.PersistedStream(
        filename=stream.filename, mode=stream.mode, loop_count=stream.loop_count,
        gop_frames=stream.gop_frames, frame_rate=stream.frame_rate, removed_at=removed_at,
        rtsp=statefile.PersistedOutput(stream.rtsp.enabled),
        udp=statefile.PersistedOutput(stream.udp.enabled),
    )


def _persist():
    """Write the state file. Must be called while holding _state_lock."""
    records = dict(_retained_streams)
    for stream in _streams_by_name.values():
        records[stream.name] = _persisted_stream(stream)
    statefile.save_state(STATE_PATH, statefile.PersistedState(dict(_udp_ports), records))


def refresh_output_reachable():
    """Refresh the cached OUTPUT_HOST reachability.

    ffmpeg aborts if the UDP destination host is unresolvable. So when the
    consumer (for example the cx-search video-streaming service) is not on
    the network, the udp output is held back rather than launched to die: it
    stays enabled and recover_udp_outputs launches it once the host resolves.
    A numeric IP or multicast group always resolves, so this only trips on a
    missing hostname.

    gethostbyname blocks for the resolver timeout when the host is down, so it
    must stay off the request path: status reads and launches sample the
    cache, and the poll loop refreshes it every tick.
    """
    global _output_reachable, _output_host_warned
    try:
        socket.gethostbyname(OUTPUT_HOST)
        _output_host_warned = False
        _output_reachable = True
    except OSError:
        if not _output_host_warned:
            log.warning("OUTPUT_HOST %r does not resolve, rtsp/hls only until reachable",
                        OUTPUT_HOST)
            _output_host_warned = True
        _output_reachable = False


def _udp_port_for(stream_name):
    """Return the stream's UDP port, assigning the next free one on first use.

    The range is a contract with the downstream consumer, so a port stays
    with its stream name — including after the file is removed — for as long
    as the range has room. Only when it is used up is the port of the
    longest-removed file reclaimed (with its remembered settings), so churn
    of temporary files cannot exhaust the range while a removed-and-restored
    file normally keeps its udp:// URL. Returns None when every port belongs
    to a present file; the caller streams RTSP/HLS only.
    Must be called while holding _state_lock.
    """
    global _next_udp_port
    port = _udp_ports.get(stream_name)
    if port is not None:
        return port
    if _next_udp_port <= UDP_LAST_PORT:
        port = _next_udp_port
        _next_udp_port += 1
    else:
        # Saved files may still be waiting for discovery at startup. Their
        # ports remain reserved even before they have an active slot.
        try:
            present_filenames = {path.name for path in VIDEOS_DIR.iterdir()}
        except OSError as error:
            log.warning("Cannot check which UDP ports can be reclaimed: %s", error)
            return None
        removed = [(record.removed_at or 0, name) for name, record in _retained_streams.items()
                   if name in _udp_ports and name not in _streams_by_name
                   and record.filename not in present_filenames]
        if not removed:
            log.warning("UDP port range %d-%d is used up; %s streams RTSP/HLS only",
                        UDP_BASE_PORT, UDP_LAST_PORT, stream_name)
            return None
        _, oldest = min(removed)
        port = _udp_ports.pop(oldest)
        del _retained_streams[oldest]
        log.info("UDP port %d moves from removed %s to %s", port, oldest, stream_name)
    _udp_ports[stream_name] = port
    return port


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


def _ffmpeg_log_path(stream_name, output_name):
    return FFMPEG_LOG_DIR / f"{stream_name}.{output_name}.log"


def _ffmpeg_tail(stream_name, output_name):
    """The end of an output's ffmpeg log, or "" when there is none."""
    try:
        with open(_ffmpeg_log_path(stream_name, output_name), "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, log_file.tell() - FFMPEG_LOG_TAIL_BYTES))
            return log_file.read().decode(errors="replace").strip()
    except OSError as error:
        log.warning("No ffmpeg log for %s/%s: %s", stream_name, output_name, error)
        return ""


def _last_lines(text, count):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


FFMPEG_REASON_CHARS = 80   # how much of ffmpeg's last line a crash log line quotes


def _ffmpeg_death_reason(stream_name, output_name):
    """Why an output's ffmpeg died: (line for the log, lines for the UI).

    ffmpeg output goes to a per-output file rather than the console (except in
    debug mode, where it was already visible), so the reason is read back from
    that file. The log gets the last line, cut short to stay on one line; the
    UI gets the last two lines whole.
    """
    if SHOW_FFMPEG_OUTPUT:
        return "see the container log", "see the container log"
    tail = _ffmpeg_tail(stream_name, output_name)
    if not tail:
        return "ffmpeg exited without output", "ffmpeg exited without output"
    last_line = _last_lines(tail, 1)
    if len(last_line) > FFMPEG_REASON_CHARS:
        last_line = last_line[:FFMPEG_REASON_CHARS - 1] + "…"
    return last_line, _last_lines(tail, 2)


def wait_for_mediamtx():
    log.debug("Waiting for MediaMTX to be available on port %d...", RTSP_PORT)
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("localhost", RTSP_PORT))
                break
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(1)
    log.debug("MediaMTX is ready")


# ---- Pure helpers: from a stream's state to a command line ----

def encode_spec(stream):
    """The supervisor's decisions for this stream, from its profile and choices.

    Passthrough eligibility is re-derived from the profile, so a replaced file
    or failed probe degrades to transcode instead of producing a command
    ffmpeg cannot run. Must be called while holding _state_lock.
    """
    profile = stream.profile or ffmpegcmd.UNKNOWN_PROFILE
    requested_mode = stream.mode or default_mode(profile.video_codecs)
    output_rate = stream.frame_rate or profile.frame_rate
    gop_frames = stream.gop_frames or ffmpegcmd.natural_gop_frames(output_rate)
    passthrough = requested_mode == "passthrough" and passthrough_eligible(profile.video_codecs)
    return ffmpegcmd.EncodeSpec(
        video_path=stream.video_path,
        stream_name=stream.name,
        loop_count=stream.loop_count,
        gop_frames=gop_frames,
        frame_rate=stream.frame_rate,
        passthrough=passthrough,
        audio_copy=profile.audio_copyable,
        # Copied packets can't honor a cap.
        bitrate_cap="" if passthrough else bitrate_cap_for(profile.bitrate_bps),
        rtsp_port=RTSP_PORT,
        udp_target=f"{OUTPUT_HOST}:{stream.udp_port}" if stream.udp_port else None,
    )


def default_argv(stream, output_name):
    """The generated command for one output, or None when it cannot be built."""
    return ffmpegcmd.build_argv(encode_spec(stream), output_name)


def udp_launchable(stream):
    """Whether the udp output can be launched now, with the reason when not.

    No port from the range is permanent for this slot; an unreachable consumer
    is transient, and launching anyway would only make ffmpeg die at start.
    """
    if stream.udp_port is None:
        return False, f"no free UDP port in {UDP_BASE_PORT}-{UDP_LAST_PORT}"
    if not _output_reachable:
        return False, f"{OUTPUT_HOST} unreachable"
    return True, None


# ---- Process primitives: one output at a time ----

def launch_output(stream_name, output_name, log_start=True):
    """Start one output's ffmpeg process. Does not change enabled."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            log.warning("Cannot start unclaimed stream: %s", stream_name)
            return False
        output = stream.output(output_name)
        if not output.enabled:
            return False
        if output.occupied:
            log.warning("Output already %s: %s/%s",
                        "stopping" if output.stopping else "running", stream_name, output_name)
            return False
        if output_name == "udp":
            launchable, reason = udp_launchable(stream)
            if not launchable:
                log.debug("Holding %s/udp back: %s", stream_name, reason)
                return False
        if stream.profile is not None and not stream.profile.video_codecs:
            # Probed and found no video track (or unreadable: not a video, or
            # still being copied) — ffmpeg would only die at once. Say so and
            # wait for the file to change.
            output.last_error = "no video track found: not a video file, or still being copied"
            log.warning("%s/%s cannot start: %s", stream_name, output_name, output.last_error)
            return False
        spec = encode_spec(stream)
        argv = ffmpegcmd.build_argv(spec, output_name)
        try:
            if SHOW_FFMPEG_OUTPUT:
                process = subprocess.Popen(argv)
            else:
                # The child keeps its own fd, so closing ours right away is safe.
                with open(_ffmpeg_log_path(stream_name, output_name), "wb") as ffmpeg_log:
                    process = subprocess.Popen(argv, stdout=ffmpeg_log, stderr=ffmpeg_log)
        except Exception as error:
            output.last_error = f"could not start ffmpeg: {error}"
            log.error("%s/%s cannot start: %s", stream_name, output_name, error)
            return False
        output.mark_started(process, argv)
        rtsp_url = stream.urls()["rtsp"]

    # The rtsp output coming up is the moment the stream is watchable, so
    # that is the one line a stream gets: the URL to open.
    if log_start and output_name == "rtsp":
        log.info("%s", rtsp_url)
    else:
        log.debug("Started %s/%s", stream_name, output_name)
    return True


def stop_output(stream_name, output_name):
    """Stop one output's process. Does not change enabled."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        output = stream.output(output_name) if stream is not None else None
        if output is None or not output.occupied:
            log.warning("Output not running: %s/%s", stream_name, output_name)
            return False
        process = output.process
        should_terminate = not output.stopping
        output.stopping = True

    try:
        if should_terminate:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if not should_terminate:
                log.warning("Output still stopping: %s/%s", stream_name, output_name)
                return True
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Better to leak a wedged process than to hang the supervisor forever
                # (can happen on stuck NFS/CIFS reads inside ffmpeg). Keep the output
                # blocked so a second ffmpeg process can't reuse the same destination.
                log.error("%s/%s did not die after SIGKILL, leaking pid %d",
                          stream_name, output_name, process.pid)
                return True
        with _state_lock:
            current = _streams_by_name.get(stream_name)
            if current is not None and current.output(output_name).process is process:
                current.output(output_name).clear_process()
        log.debug("Stopped %s/%s", stream_name, output_name)
    except Exception as error:
        log.error("Error stopping %s/%s: %s", stream_name, output_name, error)

    return True


def _relaunch_output(stream_name, output_name, log_start=True):
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        occupied = stream is not None and stream.output(output_name).occupied
    if occupied:
        stop_output(stream_name, output_name)
    return launch_output(stream_name, output_name, log_start)


def refresh_stream_probe(stream_name):
    """Re-probe the file. Runs ffprobe outside the lock
    so other threads don't stall on I/O."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            return
        video_path = stream.video_path
    profile = ffmpegcmd.probe_file(video_path)
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is not None:
            stream.profile = profile


# ---- Operations: what the UI, API, and file events ask for ----

KEEP = object()   # update_settings argument: leave this setting as it is


def update_settings(stream_name, loop_count=KEEP, mode=KEEP, gop_frames=KEEP, frame_rate=KEEP):
    """Store new settings for a stream and apply them to whatever is running.

    mode None means the codec-derived default, frame_rate None the file's
    own rate, and gop_frames None one second at the output rate. A running
    output is relaunched so the process always matches the stored settings;
    a stopped stream just keeps them for its next start.
    """
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            return False
        if loop_count is not KEEP:
            stream.loop_count = loop_count
        if mode is not KEEP:
            stream.mode = mode
        if gop_frames is not KEEP:
            stream.gop_frames = gop_frames
        if frame_rate is not KEEP:
            stream.frame_rate = frame_rate
        for output in stream.outputs:
            output.reset_health()
        _persist()
        running = [output.name for output in stream.outputs if output.running]
        log.info("%s settings changed (mode=%s loop=%s fps=%s gop=%s)%s", stream_name,
                 stream.mode, stream.loop_count, stream.frame_rate, stream.gop_frames,
                 ", restarted" if running else "")
    if running:
        refresh_stream_probe(stream_name)
    for output_name in running:
        _relaunch_output(stream_name, output_name, log_start=False)
    return True


def start_stream(stream_name):
    """(Re)start a stream with its stored settings.

    A fully stopped stream comes up with both outputs. One that is up keeps
    its per-output choices: Restart relaunches the enabled outputs and leaves
    a deliberately stopped one down.
    """
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            log.warning("Cannot start unclaimed stream: %s", stream_name)
            return False
        if not any(output.enabled for output in stream.outputs):
            for output in stream.outputs:
                output.enabled = True
        for output in stream.outputs:
            output.completed = False
            output.reset_health()
        _persist()
        enabled = [output.name for output in stream.outputs if output.enabled]
    refresh_stream_probe(stream_name)
    launched = [_relaunch_output(stream_name, output_name) for output_name in enabled]
    return any(launched)


def stop_stream(stream_name):
    """Stop every output of a stream and disable both."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            log.warning("Stream not found: %s", stream_name)
            return False
        for output in stream.outputs:
            output.enabled = False
        _persist()
        occupied = [output.name for output in stream.outputs if output.occupied]
    for output_name in occupied:
        stop_output(stream_name, output_name)
    if occupied:
        log.info("%s stopped", stream_name)
    return True


def start_output(stream_name, output_name):
    """(Re)start one output and mark it enabled."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            return False
        output = stream.output(output_name)
        output.enabled = True
        output.completed = False
        output.reset_health()
        _persist()
    refresh_stream_probe(stream_name)
    if _relaunch_output(stream_name, output_name):
        return True
    # A udp output held back (consumer unreachable) is not a failure: the
    # request was recorded and recover_udp_outputs launches it later.
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        return (output_name == "udp" and stream is not None
                and stream.udp.enabled
                and not udp_launchable(stream)[0])


def disable_output(stream_name, output_name):
    """Stop one output and mark it disabled, so supervision leaves it down."""
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            return False
        output = stream.output(output_name)
        output.enabled = False
        _persist()
        occupied = output.occupied
    if occupied:
        stop_output(stream_name, output_name)
        log.info("%s/%s stopped", stream_name, output_name)
    return True


def launch_enabled_outputs(stream_name, log_start=True):
    """Bring up whatever the stream's outputs say should be up."""
    refresh_stream_probe(stream_name)
    with _state_lock:
        stream = _streams_by_name.get(stream_name)
        if stream is None:
            return
        pending = [output.name for output in stream.outputs
                   if output.enabled and not output.occupied and not output.completed]
    for output_name in pending:
        launch_output(stream_name, output_name, log_start)


def stream_exists(stream_name):
    """True if a slot is registered under this name."""
    with _state_lock:
        return stream_name in _streams_by_name


def stop_all_streams():
    with _state_lock:
        names = list(_streams_by_name)
    for name in names:
        stop_stream(name)


def start_all_streams():
    """Enable every output and bring up the ones that are down. Running
    outputs are left alone, so viewers are not dropped."""
    with _state_lock:
        for stream in _streams_by_name.values():
            for output in stream.outputs:
                output.enabled = True
                output.completed = False
        _persist()
        names = list(_streams_by_name)
    for name in names:
        launch_enabled_outputs(name)


def _output_state(stream, output):
    """(state, reason): the output's state word for the UI and, when it is not
    simply running, the human explanation."""
    if not output.enabled:
        return "stopped", "stopped"
    if output.name == "udp" and stream.udp_port is None:
        # Permanent for this slot, so report it ahead of the transient reasons.
        return "failed", f"no free UDP port in {UDP_BASE_PORT}-{UDP_LAST_PORT}"
    if output.running:
        if output.crashes:
            return "crashing", f"died {output.crashes}x; relaunched, watching"
        return "running", None
    if output.completed:
        return "completed", "played to the end"
    if output.name == "udp" and not _output_reachable:
        return "waiting", f"{OUTPUT_HOST} unreachable"
    if output.crashes:
        return "restarting", f"died {output.crashes}x; relaunching"
    if output.last_error:
        return "failed", output.last_error
    return "starting", "starting"


def _output_status(stream, output):
    argv = output.launched_argv if output.running else default_argv(stream, output.name)
    state, reason = _output_state(stream, output)
    return {
        "state": state,
        "running": output.running,
        "enabled": output.enabled,
        "stopping": output.stopping,
        "command": shlex.join(argv) if argv else None,
        "reason": reason,
        "error": output.last_error if output.crashes else None,
    }


def get_stream_status():
    """Get status of all streams"""
    result = []
    with _state_lock:
        for stream in _streams_by_name.values():
            urls = stream.urls()
            spec = encode_spec(stream)
            profile = stream.profile or ffmpegcmd.UNKNOWN_PROFILE
            video_codecs = profile.video_codecs
            source_rate = profile.frame_rate
            requested_mode = stream.mode or default_mode(video_codecs)
            passthrough_active = stream.rtsp.running and spec.passthrough
            if requested_mode != "passthrough" or passthrough_active:
                passthrough_reason = None
            elif video_codecs is not None and not passthrough_eligible(video_codecs):
                # Permanent for this file, so report it ahead of the transient reasons.
                blocked = sorted({c for c in video_codecs if c not in PASSTHROUGH_CODECS})
                passthrough_reason = (f"video codec {', '.join(blocked)} cannot pass through "
                                      f"(H.264, H.265, MPEG-2, MPEG-4, MJPEG only)")
            elif not stream.rtsp.running:
                passthrough_reason = "stream stopped"
            elif video_codecs is None:
                passthrough_reason = "codec probe failed; transcoding"
            else:
                passthrough_reason = "starting"
            result.append({
                "name": stream.name,
                "filename": stream.filename,
                "running": stream.running,
                "stopping": stream.stopping,
                "loop_count": stream.loop_count,
                "gop_frames": spec.gop_frames,
                "gop_explicit": stream.gop_frames is not None,
                "frame_rate": stream.frame_rate or source_rate,
                "frame_rate_explicit": stream.frame_rate is not None,
                "source_frame_rate": source_rate,
                "mode": requested_mode,
                "effective_mode": "passthrough" if spec.passthrough else "transcode",
                "passthrough_eligible": (passthrough_eligible(video_codecs)
                                         if video_codecs is not None else None),
                "ineligible_codecs": sorted({c for c in video_codecs or () if c not in PASSTHROUGH_CODECS}),
                "passthrough_active": passthrough_active,
                "passthrough_reason": passthrough_reason,
                "encoder": spec.encoder_label,
                "rtsp_url": urls["rtsp"],
                "hls_url": urls["hls"],
                "udp_url": urls["udp"],
                "outputs": {output.name: _output_status(stream, output) for output in stream.outputs},
            })
    return result


# ---- File events ----

def _claim_slot(stream_name, filename):
    """Register (or refresh) the slot binding stream_name to filename.

    A new slot adopts the remembered settings when they belong to the same
    filename. Returns True if the slot belongs to this filename after the
    call, False if the name is invalid or belongs to a different file (caller
    should skip). Must be called under _state_lock.
    """
    if not stream_name:
        log.warning("Skipping invalid video filename: %r", filename)
        return False
    stream = _streams_by_name.get(stream_name)
    if stream is None:
        stream = Stream(name=stream_name, filename=filename, udp_port=_udp_port_for(stream_name))
        remembered = _retained_streams.get(stream_name)
        if remembered is not None and remembered.filename == filename:
            del _retained_streams[stream_name]
            stream.mode = remembered.mode
            stream.loop_count = remembered.loop_count
            stream.gop_frames = remembered.gop_frames
            stream.frame_rate = remembered.frame_rate
            stream.rtsp.enabled = remembered.rtsp.enabled
            stream.udp.enabled = remembered.udp.enabled
        _streams_by_name[stream_name] = stream
        _persist()
        return True
    if stream.filename != filename:
        log.warning("Stream name collision: %s already owned by %r; skipping %r "
                    "(after removing the owner, touch this file or restart to stream it)",
                    stream_name, stream.filename, filename)
        return False
    return True


def handle_create(filepath, added=True):
    """Claim a slot for a file and bring it up. added is False for the files
    found at boot, which are listed by URL rather than announced."""
    path = Path(filepath)
    stream_name = stream_name_for(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name):
            return
        rtsp_url = _streams_by_name[stream_name].urls()["rtsp"]
    if added:
        log.info("Added %s  %s", path.name, rtsp_url)
    launch_enabled_outputs(stream_name, log_start=not added)


def handle_delete(filepath):
    path = Path(filepath)
    with _state_lock:
        # Match the owning filename so deleting a file refused for a name
        # collision cannot remove another file's slot.
        stream = next((s for s in _streams_by_name.values() if s.filename == path.name), None)
        if stream is None:
            log.debug("Ignoring delete of %s; it owned no slot", path.name)
            return
        stream_name = stream.name
        occupied = [output.name for output in stream.outputs if output.occupied]

    log.info("Removed %s", path.name)
    for output_name in occupied:
        stop_output(stream_name, output_name)
    with _state_lock:
        stream = _streams_by_name.pop(stream_name, None)
        if stream is not None:
            _retained_streams[stream_name] = _persisted_stream(stream, removed_at=time.time())
            _persist()


def handle_modify(filepath):
    path = Path(filepath)
    stream_name = stream_name_for(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name):
            return
        stream = _streams_by_name[stream_name]
        running = [output.name for output in stream.outputs if output.running]
        for output in stream.outputs:
            # Completion belongs to the old file; preserve explicit Stop choices.
            output.completed = False
            output.reset_health()

    log.info("Updated %s%s", path.name, ", restarted" if running else "")
    # Re-probe, relaunch what is running, and bring up enabled outputs that
    # were held back (for example a file that was still being copied).
    launch_enabled_outputs(stream_name, log_start=False)
    for output_name in running:
        _relaunch_output(stream_name, output_name, log_start=False)


# ---- Supervision ----

STABLE_AFTER_SEC = 60   # a relaunched output that lasts this long is no longer "crashing"


def cleanup_dead_processes():
    now = time.time()
    with _state_lock:
        snapshot = [(stream.name, output.name, output.process)
                    for stream in _streams_by_name.values()
                    for output in stream.outputs if output.occupied]
        for stream in _streams_by_name.values():
            for output in stream.outputs:
                if (output.running and output.crashes
                        and now - (output.started_at or now) > STABLE_AFTER_SEC):
                    log.info("%s/%s stable", stream.name, output.name)
                    output.reset_health()

    dead = [(stream_name, output_name, process)
            for stream_name, output_name, process in snapshot if process.poll() is not None]

    for stream_name, output_name, process in dead:
        with _state_lock:
            current = _streams_by_name.get(stream_name)
            output = current.output(output_name) if current is not None else None
            # If a concurrent start replaced our entry with a fresh process,
            # don't orphan it — leave the new process alone.
            if output is None or output.process is not process:
                continue
            stopping = output.stopping
            enabled = output.enabled
            loop_count = current.loop_count
            video_path = current.video_path
            output.clear_process()
            if loop_count != -1 and process.returncode == 0:
                output.completed = True

        if stopping or not enabled:
            continue

        if loop_count != -1 and process.returncode == 0:
            log.info("%s/%s finished (loop off)", stream_name, output_name)
            continue

        reason, error = _ffmpeg_death_reason(stream_name, output_name)
        crashes = 0
        with _state_lock:
            current = _streams_by_name.get(stream_name)
            if current is not None:
                output = current.output(output_name)
                output.crashes += 1
                crashes = output.crashes
                output.last_error = f"ffmpeg exited with code {process.returncode}\n{error}"
        crashed = f"{stream_name}/{output_name} crashed (rc={process.returncode})"

        try:
            if not Path(video_path).exists():
                log.warning("%s, not restarted: file missing", crashed)
                continue
        except OSError as error:
            log.warning("%s, not restarted: %s", crashed, error)
            continue
        if not launch_output(stream_name, output_name, log_start=False):
            log.error("%s, not restarted", crashed)
            continue
        log.warning("%s, restarted; crash %d: %s", crashed, crashes, reason)


def recover_udp_outputs():
    """Launch udp outputs that were held back, once OUTPUT_HOST resolves.

    Reachability is sampled at launch, so an enabled udp output that was
    skipped while the consumer was off the network stays down until this
    brings it up. Slots with no port from the range are skipped: launching
    them would never succeed.
    """
    if not _output_reachable:
        return
    with _state_lock:
        pending = [stream.name for stream in _streams_by_name.values()
                   if stream.udp_port is not None and stream.udp.enabled
                   and not stream.udp.occupied and not stream.udp.completed]
    for name in pending:
        launch_output(name, "udp")
