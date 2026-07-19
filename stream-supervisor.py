#!/usr/bin/env python3
"""
Stream supervisor: watches /app/videos and manages FFmpeg streaming processes
Includes HTTP API for stream control
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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Note: inotify doesn't work on network filesystems (CIFS/NFS), so we use polling


def require_env(name):
    """Return the env var's value, failing at boot if it is missing or empty.

    .env is the single source of truth for config; compose passes every value
    through, so a missing one is a deployment error, not a case to default.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


VIDEOS_DIR = Path("/app/videos")
STREAM_VIDEO_SCRIPT = "/usr/local/bin/stream-video.sh"
INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"
RTSP_PORT = int(require_env("MEDIAMTX_RTSP_PORT"))
HLS_PORT = int(require_env("MEDIAMTX_HLS_PORT"))
API_PORT = int(require_env("STREAM_API_PORT"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()
MAX_VIDEO_BITRATE = os.getenv("MAX_VIDEO_BITRATE", "")  # empty disables the cap

# UDP MPEG-TS output carries KLV/data streams that RTSP/HLS (via MediaMTX) drop.
# Each stream is pushed to OUTPUT_HOST on its own port (UDP_BASE_PORT + slot).
# OUTPUT_HOST is a reachable consumer or a multicast group.
OUTPUT_HOST = require_env("OUTPUT_HOST")
UDP_BASE_PORT = int(require_env("UDP_BASE_PORT"))

# Poll loop tuning
POLL_INTERVAL_SEC = 2
DEBOUNCE_STABLE_POLLS = 2  # ~4s of (mtime, size) stability before committing a change

HOSTNAME = require_env("CONTAINER_NAME")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("autostream")

@dataclass
class Stream:
    """One video file's stream slot: its identity, its endpoints, and its process.

    A slot exists for as long as the file is present in VIDEOS_DIR, so "known
    but not currently streaming" is process is None rather than a separate
    collection. filename is the on-disk basename the slot belongs to, kept so a
    second file sanitizing to the same name can't silently take the slot over.
    loop_count is the last requested value and deliberately outlives the
    process, so a stop/start keeps the user's choice.
    """
    name: str
    filename: str
    video_path: str
    udp_port: int
    loop_count: int = -1
    process: subprocess.Popen | None = None
    udp_enabled: bool = False
    stopping: bool = False

    @property
    def running(self):
        """True while the stream is up and has not been asked to stop."""
        return self.process is not None and not self.stopping

    def urls(self):
        """Return the copyable endpoint URLs for this stream."""
        return {
            "rtsp": f"rtsp://{HOSTNAME}:{RTSP_PORT}/{self.name}",
            "hls": f"http://{HOSTNAME}:{HLS_PORT}/{self.name}/index.m3u8",
            "udp": f"udp://{OUTPUT_HOST}:{self.udp_port}",
        }


# Shared state. Mutations and reads MUST hold _state_lock — the API server runs in
# threads and the poll loop runs in the main thread, both touching this state.
_state_lock = threading.RLock()
streams_by_name = {}          # stream_name -> Stream, one entry per video file present

# UDP port allocator. Separate from Stream on purpose: an assignment lasts for the
# whole process lifetime, outliving the slot, so a video that is removed and put
# back keeps the udp:// URL a downstream consumer already registered.
_udp_ports = {}               # stream_name -> UDP port for the KLV MPEG-TS feed
_next_udp_port = UDP_BASE_PORT  # next port to hand out; protected by _state_lock

_output_host_warned = False   # so we log the "unresolved OUTPUT_HOST" warning once
_output_reachable = False     # cached OUTPUT_HOST reachability; the poll loop refreshes it so the status API never blocks on DNS


def log_stream_urls():
    """Log one copyable endpoint URL per line, keyed by stream name."""
    with _state_lock:
        rows = [f"{stream.name}={url}"
                for stream in sorted(streams_by_name.values(), key=lambda s: s.name)
                for url in stream.urls().values()]

    for row in rows:
        print(row, flush=True)


def output_host_reachable():
    """Return True if OUTPUT_HOST resolves, so the UDP/KLV output can be added.

    ffmpeg aborts the whole process (taking the RTSP output down with it) if the
    UDP destination host is unresolvable. So when the consumer (for example the
    cx-search video-streaming service) is not on the network, we drop the UDP
    output and stream RTSP/HLS only rather than breaking playback. A numeric IP
    or multicast group always resolves, so this only trips on a missing hostname.
    """
    global _output_host_warned
    try:
        socket.gethostbyname(OUTPUT_HOST)
        _output_host_warned = False
        return True
    except OSError:
        if not _output_host_warned:
            log.warning("OUTPUT_HOST %r does not resolve; streaming RTSP/HLS only, "
                        "no KLV/UDP feed until it becomes reachable", OUTPUT_HOST)
            _output_host_warned = True
        return False


def refresh_output_reachable():
    """Cache OUTPUT_HOST reachability so status reads never block on a DNS lookup.

    gethostbyname blocks for the resolver timeout when the host is down, so it
    must stay off the request path. The poll loop calls this every tick.
    """
    global _output_reachable
    _output_reachable = output_host_reachable()


def _udp_port_for(stream_name):
    """Return the stream's UDP port, assigning the next free one on first use.

    Ports are stable for the process lifetime so a stopped/started stream keeps
    the same udp:// URL. Must be called while holding _state_lock.
    """
    global _next_udp_port
    port = _udp_ports.get(stream_name)
    if port is None:
        port = _next_udp_port
        _next_udp_port += 1
        _udp_ports[stream_name] = port
    return port


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


def get_bitrate_cap(video_path):
    """Return MAX_VIDEO_BITRATE if the video exceeds it, else empty string.

    The cap value is expanded into concrete ffmpeg flags by stream-video.sh,
    which owns all encode syntax.
    """
    if not MAX_VIDEO_BITRATE:
        return ""

    max_bps = parse_bitrate(MAX_VIDEO_BITRATE)
    if not max_bps:
        return ""

    video_bps = get_video_bitrate(video_path)
    if not video_bps:
        log.debug("Could not detect bitrate for %s, no limit applied", Path(video_path).name)
        return ""

    video_mbps = video_bps / 1_000_000
    if video_bps > max_bps:
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


def start_stream(stream_name, loop_count=None, log_start=True):
    """Start a claimed stream slot. loop_count None keeps the last requested value."""
    with _state_lock:
        stream = streams_by_name.get(stream_name)
        if stream is None:
            log.warning("Cannot start unclaimed stream: %s", stream_name)
            return False
        if stream.process is not None:
            log.warning("Stream already %s: %s",
                        "stopping" if stream.stopping else "running", stream_name)
            return False
        video_path = stream.video_path
        udp_port = stream.udp_port
        if loop_count is None:
            loop_count = stream.loop_count

    # ffprobe (inside get_bitrate_cap) and the DNS lookup can take time — run
    # both outside the lock so we don't stall other threads on I/O.
    bitrate_cap = get_bitrate_cap(video_path)
    output_reachable = output_host_reachable()
    udp_target = f"{OUTPUT_HOST}:{udp_port}" if output_reachable else ""

    with _state_lock:
        stream = streams_by_name.get(stream_name)
        if stream is None or stream.process is not None:
            # Another thread claimed or started the slot while we were probing.
            log.warning("Stream changed while starting: %s", stream_name)
            return False

        try:
            cmd = [STREAM_VIDEO_SCRIPT, video_path, stream_name, str(loop_count), bitrate_cap, udp_target]
            if LOG_LEVEL == "debug":
                process = subprocess.Popen(cmd)
            else:
                process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error("Failed to start stream %s: %s", stream_name, e)
            return False

        stream.process = process
        stream.loop_count = loop_count
        stream.udp_enabled = output_reachable
        stream.stopping = False

    if log_start:
        udp_status = "UDP active" if udp_target else "UDP disabled"
        log.info("Started stream: %s video=%s udp=%s udp_url=udp://%s:%d",
                 stream_name, json.dumps(Path(video_path).name), udp_status, OUTPUT_HOST, udp_port)
    return True


def stop_stream(stream_name):
    with _state_lock:
        stream = streams_by_name.get(stream_name)
        if stream is None or stream.process is None:
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
            current = streams_by_name.get(stream_name)
            if current is not None and current.process is process:
                current.process = None
                current.udp_enabled = False
                current.stopping = False
        log.info("Stopped stream: %s", stream_name)
    except Exception as e:
        log.error("Error stopping stream %s: %s", stream_name, e)

    return True


def get_stream_status():
    """Get status of all streams"""
    result = []
    # Cached reachability (the poll loop refreshes it; never DNS on the API path).
    # A running feed reads active only while the consumer resolves — ffmpeg keeps
    # pushing UDP into the void otherwise, so the start-time flag alone would lie.
    reachable = _output_reachable
    with _state_lock:
        for stream in streams_by_name.values():
            urls = stream.urls()
            # udp_enabled means the feed is really live: the stream is running, it
            # launched with a UDP output, and the consumer resolves right now.
            # udp_reason explains an inactive feed so the UI can say why.
            udp_active = stream.running and stream.udp_enabled and reachable
            if udp_active:
                udp_reason = None
            elif not stream.running:
                udp_reason = "stream stopped"
            elif not reachable:
                udp_reason = f"{OUTPUT_HOST} unreachable"
            else:
                udp_reason = "starting"
            result.append({
                "name": stream.name,
                "video_path": stream.video_path,
                "running": stream.running,
                "stopping": stream.stopping,
                "loop_count": stream.loop_count,
                "rtsp_url": urls["rtsp"],
                "hls_url": urls["hls"],
                "udp_url": urls["udp"],
                "udp_enabled": udp_active,
                "udp_reason": udp_reason,
            })
    return result


def iter_video_files():
    """Yield the streamable files in VIDEOS_DIR, ordered by name.

    Hidden files and READMEs are skipped, as are entries that vanish mid-scan —
    the next poll picks those up as deletions.
    """
    for path in sorted(VIDEOS_DIR.iterdir(), key=lambda path: path.name):
        try:
            if not path.is_file() or is_ignored(path):
                continue
        except OSError:
            continue
        yield path


def _claim_slot(stream_name, filename, video_path):
    """Register (or refresh) the slot binding stream_name to filename.

    Returns True if the slot belongs to this filename after the call, False if
    there was a collision with a different file (caller should skip).
    Must be called under _state_lock.
    """
    stream = streams_by_name.get(stream_name)
    if stream is None:
        streams_by_name[stream_name] = Stream(
            name=stream_name,
            filename=filename,
            video_path=video_path,
            udp_port=_udp_port_for(stream_name),
        )
        return True
    if stream.filename != filename:
        log.warning("Stream name collision: %s already owned by %r; skipping %r "
                    "(after removing the owner, touch this file or restart to stream it)",
                    stream_name, stream.filename, filename)
        return False
    stream.video_path = video_path
    return True


def scan_videos():
    """Scan the video directory and claim a stream slot for every file found."""
    if not VIDEOS_DIR.exists():
        log.error("Directory does not exist: %s", VIDEOS_DIR)
        return

    with _state_lock:
        for video_path in iter_video_files():
            _claim_slot(sanitize_name(video_path), video_path.name, str(video_path))


def sync_videos():
    """Scan videos and start all streams"""
    log.info("Scanning %s for video files...", VIDEOS_DIR)
    scan_videos()

    with _state_lock:
        targets = list(streams_by_name)

    count = 0
    for stream_name in targets:
        if start_stream(stream_name, log_start=False):
            count += 1

    log.info("Initial sync complete: %d streams started", count)
    log_stream_urls()


def handle_create(filepath):
    path = Path(filepath)
    if is_ignored(path):
        return

    stream_name = sanitize_name(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name, str(path)):
            return
    log.info("Video added: %s", path.name)
    start_stream(stream_name)


def handle_delete(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)

    with _state_lock:
        stream = streams_by_name.get(stream_name)
        if stream is None or stream.filename != path.name:
            # Slot belongs to a different file (or never existed) — don't touch it.
            owner = stream.filename if stream is not None else None
            log.debug("Ignoring delete of %s; slot %s owned by %r", path.name, stream_name, owner)
            return
        was_running = stream.process is not None

    log.info("Video removed: %s", path.name)
    if was_running:
        stop_stream(stream_name)
    with _state_lock:
        streams_by_name.pop(stream_name, None)


def handle_modify(filepath):
    path = Path(filepath)
    if is_ignored(path):
        return

    stream_name = sanitize_name(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name, str(path)):
            return
        running = streams_by_name[stream_name].running

    log.info("Video updated: %s", path.name)
    if running:
        stop_stream(stream_name)
        start_stream(stream_name)


def cleanup_dead_processes():
    # Snapshot under the lock, then poll() (which is non-blocking) outside.
    with _state_lock:
        snapshot = [(s.name, s.process) for s in streams_by_name.values() if s.process is not None]

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
            current = streams_by_name.get(stream_name)
            # If a concurrent /start replaced our entry with a fresh process,
            # don't orphan it — leave the new process alone.
            if current is None or current.process is not process:
                continue
            stopping = current.stopping
            loop_count = current.loop_count
            video_path = current.video_path
            current.process = None
            current.udp_enabled = False
            current.stopping = False

        if stopping:
            continue

        if loop_count != -1 and process.returncode == 0:
            log.info("Stream completed: %s", stream_name)
            continue

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

    output_host_reachable() is only consulted at stream start, so a stream that
    came up before the consumer was on the network stays RTSP/HLS-only until
    restarted. Restarting re-runs that check and wires the UDP output back in.
    """
    if not _output_reachable:
        return
    with _state_lock:
        pending = [s.name for s in streams_by_name.values() if s.running and not s.udp_enabled]
    for name in pending:
        stop_stream(name)
        start_stream(name)


class StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default access logging; we have our own.

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/' or parsed.path == '/index.html':
            try:
                html = INDEX_HTML_PATH.read_text(encoding='utf-8')
            except OSError as e:
                self.send_json({"error": f"index.html unavailable: {e}"}, 500)
                return
            self.send_html(html)
        elif parsed.path == '/api/streams':
            self.send_json(get_stream_status())
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip('/').split('/')

        if len(path_parts) >= 3 and path_parts[0] == 'api' and path_parts[1] == 'streams':
            stream_name = path_parts[2]
            action = path_parts[3] if len(path_parts) > 3 else None

            if stream_name == 'stop-all':
                with _state_lock:
                    names = [s.name for s in streams_by_name.values() if s.process is not None]
                for name in names:
                    stop_stream(name)
                self.send_json({"success": True})
                return

            if stream_name == 'start-all':
                with _state_lock:
                    candidates = [s.name for s in streams_by_name.values() if s.process is None]
                for name in candidates:
                    start_stream(name)
                self.send_json({"success": True})
                return

            if action == 'start':
                with _state_lock:
                    stream = streams_by_name.get(stream_name)
                    running = stream is not None and stream.process is not None
                if stream is None:
                    self.send_json({"error": "Stream not found"}, 404)
                    return
                query = parse_qs(parsed.query)
                try:
                    loop_count = int(query.get('loop', ['-1'])[0])
                except (TypeError, ValueError):
                    self.send_json({"error": "Invalid loop count"}, 400)
                    return
                if loop_count < -1:
                    self.send_json({"error": "Invalid loop count"}, 400)
                    return
                if running:
                    stop_stream(stream_name)
                success = start_stream(stream_name, loop_count)
                self.send_json({"success": success})

            elif action == 'stop':
                success = stop_stream(stream_name)
                self.send_json({"success": success})

            else:
                self.send_json({"error": "Unknown action"}, 400)
        else:
            self.send_json({"error": "Not found"}, 404)

def start_api_server():
    try:
        server = ThreadingHTTPServer(('0.0.0.0', API_PORT), StreamHandler)  # type: ignore[arg-type]
        log.info("Stream Control UI: http://localhost:%d", API_PORT)
        server.serve_forever()
    except Exception as e:
        # The control UI is a primary feature; silent half-death (supervisor alive,
        # UI gone) is worse than a hard exit that systemd/compose can restart.
        log.critical("API server failed: %s", e)
        os._exit(1)


def get_video_files():
    """Return {filename: (mtime, size)} for all streamable files in VIDEOS_DIR."""
    files = {}
    for video_path in iter_video_files():
        try:
            stat = video_path.stat()
        except OSError:
            continue  # vanished between the scan and the stat; next poll sees the delete
        files[video_path.name] = (stat.st_mtime, stat.st_size)
    return files


class FileChangeDebouncer:
    """Turns successive directory snapshots into create/modify/delete events.

    A file being copied in changes on every poll, so a create or modify is only
    reported once its (mtime, size) has held steady for DEBOUNCE_STABLE_POLLS
    consecutive polls — otherwise a long copy triggers a storm of stop+start
    cycles. Deletions are reported immediately; gone is gone.
    """

    def __init__(self, known_files):
        self._known = dict(known_files)
        self._pending = {}  # filename -> (snapshot, consecutive stable polls)

    def poll(self, current_files):
        """Return (creates, modifies, deletions) for this snapshot."""
        deletions = [name for name in self._known if name not in current_files]
        for name in deletions:
            del self._known[name]

        # Forget files that vanished mid-debounce.
        for name in list(self._pending):
            if name not in current_files:
                del self._pending[name]

        creates, modifies = [], []
        for name, current in current_files.items():
            previous = self._known.get(name)
            if previous == current:
                self._pending.pop(name, None)
                continue
            snapshot, stable_polls = self._pending.get(name, (None, 0))
            if snapshot != current:
                self._pending[name] = (current, 0)
                continue
            stable_polls += 1
            if stable_polls < DEBOUNCE_STABLE_POLLS:
                self._pending[name] = (current, stable_polls)
                continue
            if previous is None:
                creates.append(name)
            else:
                modifies.append(name)
            self._known[name] = current
            del self._pending[name]

        return creates, modifies, deletions


def watch_directory():
    """Watch directory for changes using polling (works on network filesystems).

    Deletions are processed before creates/modifies in each tick so that a
    rename (delete-then-create with the same stream_name) frees the slot before
    the new file tries to claim it.
    """
    log.info("Watching %s for changes (polling mode)...", VIDEOS_DIR)

    last_cleanup = time.time()
    refresh_output_reachable()
    try:
        known_files = get_video_files()
    except Exception as e:
        log.error("Error scanning directory: %s", e)
        known_files = {}

    debouncer = FileChangeDebouncer(known_files)

    while True:
        time.sleep(POLL_INTERVAL_SEC)

        try:
            current_files = get_video_files()
        except Exception as e:
            log.error("Error scanning directory: %s", e)
            current_files = None

        if current_files is not None:
            creates, modifies, deletions = debouncer.poll(current_files)
            # Process deletions first so renames into a now-free slot work.
            for filename in deletions:
                handle_delete(VIDEOS_DIR / filename)
            for filename in creates:
                handle_create(VIDEOS_DIR / filename)
            for filename in modifies:
                handle_modify(VIDEOS_DIR / filename)

        refresh_output_reachable()
        recover_udp_outputs()

        if time.time() - last_cleanup > 30:
            cleanup_dead_processes()
            last_cleanup = time.time()


def main():
    threading.current_thread().name = "MainThread"
    log.info("Stream supervisor starting...")

    wait_for_mediamtx()

    api_thread = threading.Thread(target=start_api_server, name="APIServer", daemon=True)
    api_thread.start()

    sync_videos()
    watch_directory()


if __name__ == "__main__":
    main()
