#!/usr/bin/env python3
"""
Stream supervisor: watches /app/videos and manages FFmpeg streaming processes
Includes HTTP API for stream control
"""
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Note: inotify doesn't work on network filesystems (CIFS/NFS), so we use polling

VIDEOS_DIR = Path("/app/videos")
STREAM_VIDEO_SCRIPT = "/usr/local/bin/stream-video.sh"
INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"
RTSP_PORT = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
HLS_PORT = int(os.getenv("MEDIAMTX_HLS_PORT", "8888"))
API_PORT = int(os.getenv("STREAM_API_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()
MAX_VIDEO_BITRATE = os.getenv("MAX_VIDEO_BITRATE", "")

# UDP MPEG-TS output carries KLV/data streams that RTSP/HLS (via MediaMTX) drop.
# Each stream is pushed to OUTPUT_HOST on its own port (UDP_BASE_PORT + slot).
# Set OUTPUT_HOST to a reachable consumer or a multicast group; the loopback
# default just means "nothing listens" until it is configured.
OUTPUT_HOST = os.getenv("OUTPUT_HOST", "127.0.0.1")
UDP_BASE_PORT = int(os.getenv("UDP_BASE_PORT", "20000"))

# Poll loop tuning
POLL_INTERVAL_SEC = 2
DEBOUNCE_STABLE_POLLS = 2  # ~4s of (mtime, size) stability before committing a change

HOSTNAME = os.getenv("CONTAINER_NAME")
if not HOSTNAME:
    raise RuntimeError("CONTAINER_NAME environment variable is not set")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("autostream")

# Shared state. Mutations and reads MUST hold _state_lock — the API server runs in
# threads and the poll loop runs in the main thread, both touching these dicts.
_state_lock = threading.RLock()
streams = {}                  # stream_name -> {"process": Popen, "video_path": str, "loop_count": int}
available_videos = {}         # stream_name -> video_path (str)
stream_loop_counts = {}       # stream_name -> last requested loop_count (persists across stop/start)
stream_name_to_filename = {}  # stream_name -> filename (basename); reverse map so name collisions don't silently overwrite
stream_udp_ports = {}         # stream_name -> UDP port for the KLV MPEG-TS feed
_next_udp_port = UDP_BASE_PORT  # next port to hand out; protected by _state_lock
_output_host_warned = False   # so we log the "unresolved OUTPUT_HOST" warning once


def stream_urls(stream_name):
    """Return the copyable endpoint URLs for a stream.

    Must be called while holding _state_lock so the UDP port assignment is
    stable and visible in status/log output before the stream starts.
    """
    udp_port = _udp_port_for(stream_name)
    return {
        "rtsp": f"rtsp://{HOSTNAME}:{RTSP_PORT}/{stream_name}",
        "hls": f"http://{HOSTNAME}:{HLS_PORT}/{stream_name}/index.m3u8",
        "udp": f"udp://{OUTPUT_HOST}:{udp_port}",
    }


def log_stream_urls():
    """Log one copyable endpoint URL per line, keyed by stream name."""
    with _state_lock:
        names = sorted(available_videos)
        rows = []
        for name in names:
            urls = stream_urls(name)
            rows.extend(f"{name}={url}" for url in urls.values())

    for url in rows:
        print(url, flush=True)


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


def _udp_port_for(stream_name):
    """Return the stream's UDP port, assigning the next free one on first use.

    Ports are stable for the process lifetime so a stopped/started stream keeps
    the same udp:// URL. Must be called while holding _state_lock.
    """
    global _next_udp_port
    port = stream_udp_ports.get(stream_name)
    if port is None:
        port = _next_udp_port
        _next_udp_port += 1
        stream_udp_ports[stream_name] = port
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


def get_bitrate_flags(video_path):
    """Return ffmpeg bitrate flags if video exceeds MAX_VIDEO_BITRATE, else empty string."""
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
        return f"-b:v {MAX_VIDEO_BITRATE} -maxrate {MAX_VIDEO_BITRATE} -bufsize {MAX_VIDEO_BITRATE}"
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


def start_stream(video_path, stream_name, loop_count=-1, log_start=True):
    # ffprobe (inside get_bitrate_flags) and the DNS lookup can take time — run
    # both before taking the lock so we don't block API threads on I/O.
    bitrate_flags = get_bitrate_flags(video_path)
    output_reachable = output_host_reachable()

    with _state_lock:
        if stream_name in streams:
            status = "stopping" if streams[stream_name].get("stopping", False) else "running"
            log.warning("Stream already %s: %s", status, stream_name)
            return False

        urls = stream_urls(stream_name)
        udp_target = urls["udp"].removeprefix("udp://") if output_reachable else ""
        try:
            cmd = [STREAM_VIDEO_SCRIPT, str(video_path), stream_name, str(loop_count), bitrate_flags, udp_target]
            if LOG_LEVEL == "debug":
                process = subprocess.Popen(cmd)
            else:
                process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error("Failed to start stream %s: %s", stream_name, e)
            return False

        streams[stream_name] = {
            "process": process,
            "video_path": str(video_path),
            "loop_count": loop_count,
            "udp_enabled": output_reachable,
            "stopping": False,
        }
        available_videos[stream_name] = str(video_path)
        stream_loop_counts[stream_name] = loop_count

    if log_start:
        udp_status = "UDP active" if udp_target else "UDP disabled"
        log.info("Started stream: %s video=%s udp=%s udp_url=%s",
                 stream_name, json.dumps(Path(video_path).name), udp_status, urls["udp"])
    return True


def stop_stream(stream_name):
    with _state_lock:
        stream_info = streams.get(stream_name)
        if stream_info is None:
            log.warning("Stream not found: %s", stream_name)
            return False
        process = stream_info["process"]
        should_terminate = not stream_info.get("stopping", False)
        stream_info["stopping"] = True

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
            current = streams.get(stream_name)
            if current is not None and current["process"] is process:
                del streams[stream_name]
        log.info("Stopped stream: %s", stream_name)
    except Exception as e:
        log.error("Error stopping stream %s: %s", stream_name, e)

    return True


def get_stream_status():
    """Get status of all streams"""
    result = []
    with _state_lock:
        for name, video_path in available_videos.items():
            running_info = streams.get(name)
            is_stopping = running_info is not None and running_info.get("stopping", False)
            is_running = running_info is not None and not is_stopping
            # Live value when running, last-requested value otherwise.
            if running_info is not None:
                loop_count = running_info["loop_count"]
            else:
                loop_count = stream_loop_counts.get(name, -1)
            urls = stream_urls(name)
            result.append({
                "name": name,
                "video_path": video_path,
                "running": is_running,
                "stopping": is_stopping,
                "loop_count": loop_count,
                "rtsp_url": urls["rtsp"],
                "hls_url": urls["hls"],
                "udp_url": urls["udp"],
                "udp_enabled": is_running and running_info.get("udp_enabled", False),
            })
    return result


def _claim_slot(stream_name, filename, video_path):
    """Register (or refresh) a stream_name -> filename binding.

    Returns True if the binding belongs to this filename after the call,
    False if there was a collision with a different file (caller should skip).
    Must be called under _state_lock.
    """
    owner = stream_name_to_filename.get(stream_name)
    if owner is not None and owner != filename:
        log.warning("Stream name collision: %s already owned by %r; skipping %r",
                    stream_name, owner, filename)
        return False
    stream_name_to_filename[stream_name] = filename
    available_videos[stream_name] = video_path
    _udp_port_for(stream_name)  # reserve a stable UDP port so status always has one
    return True


def scan_videos():
    """Scan video directory and populate available_videos."""
    if not VIDEOS_DIR.exists():
        log.error("Directory does not exist: %s", VIDEOS_DIR)
        return

    with _state_lock:
        for video_path in VIDEOS_DIR.iterdir():
            if not video_path.is_file() or is_ignored(video_path):
                continue
            stream_name = sanitize_name(video_path)
            _claim_slot(stream_name, video_path.name, str(video_path))


def claim_existing_file_for_stream(stream_name):
    """Claim a previously skipped colliding file after the owner disappears."""
    try:
        candidates = sorted(VIDEOS_DIR.iterdir(), key=lambda path: path.name)
    except OSError as e:
        log.warning("Cannot scan for replacement stream %s: %s", stream_name, e)
        return None

    with _state_lock:
        if stream_name in stream_name_to_filename:
            return None
        for path in candidates:
            try:
                if not path.is_file() or is_ignored(path):
                    continue
            except OSError:
                continue
            if sanitize_name(path) == stream_name and _claim_slot(stream_name, path.name, str(path)):
                return path
    return None


def sync_videos():
    """Scan videos and start all streams"""
    log.info("Scanning %s for video files...", VIDEOS_DIR)
    scan_videos()

    with _state_lock:
        targets = list(available_videos.items())

    count = 0
    for stream_name, video_path in targets:
        if start_stream(video_path, stream_name, log_start=False):
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
    start_stream(path, stream_name)


def handle_delete(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)

    with _state_lock:
        owner = stream_name_to_filename.get(stream_name)
        if owner != path.name:
            # Slot belongs to a different file (or never existed) — don't touch it.
            log.debug("Ignoring delete of %s; slot %s owned by %r", path.name, stream_name, owner)
            return

    log.info("Video removed: %s", path.name)
    stop_stream(stream_name)
    with _state_lock:
        stream_name_to_filename.pop(stream_name, None)
        available_videos.pop(stream_name, None)
        stream_loop_counts.pop(stream_name, None)

    replacement = claim_existing_file_for_stream(stream_name)
    if replacement is not None:
        log.info("Video added: %s", replacement.name)
        start_stream(replacement, stream_name)


def handle_modify(filepath):
    path = Path(filepath)
    if is_ignored(path):
        return

    stream_name = sanitize_name(path)
    with _state_lock:
        if not _claim_slot(stream_name, path.name, str(path)):
            return
        loop_count = stream_loop_counts.get(stream_name, -1)
        running_info = streams.get(stream_name)
        running = running_info is not None and not running_info.get("stopping", False)

    log.info("Video updated: %s", path.name)
    if running:
        stop_stream(stream_name)
        start_stream(path, stream_name, loop_count)


def cleanup_dead_processes():
    # Snapshot under the lock, then poll() (which is non-blocking) outside.
    with _state_lock:
        snapshot = [(name, info["process"]) for name, info in streams.items()]

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
            current = streams.get(stream_name)
            # If a concurrent /start replaced our entry with a fresh process,
            # don't orphan it — leave the new process alone.
            if current is None or current["process"] is not process:
                continue
            stopping = current.get("stopping", False)
            loop_count = current["loop_count"]
            del streams[stream_name]
            video_path = available_videos.get(stream_name)

        if stopping:
            continue

        if loop_count != -1 and process.returncode == 0:
            log.info("Stream completed: %s", stream_name)
            continue

        if not video_path:
            continue
        try:
            if not Path(video_path).exists():
                log.warning("Cannot restart stream %s: file missing", stream_name)
                continue
        except OSError as e:
            log.warning("Cannot restart stream %s: %s", stream_name, e)
            continue
        if not start_stream(video_path, stream_name, loop_count, log_start=False):
            log.error("Failed to restart stream: %s", stream_name)
            continue
        restarted.append(stream_name)

    if restarted:
        log.info("Restarted streams: %s", ", ".join(sorted(restarted)))


class StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default access logging; we have our own.

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
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
                    names = list(streams.keys())
                for name in names:
                    stop_stream(name)
                self.send_json({"success": True})
                return

            if stream_name == 'start-all':
                with _state_lock:
                    candidates = [(name, vp) for name, vp in available_videos.items()
                                  if name not in streams]
                    loops = {name: stream_loop_counts.get(name, -1) for name, _ in candidates}
                for name, video_path in candidates:
                    start_stream(video_path, name, loops[name])
                self.send_json({"success": True})
                return

            if action == 'start':
                with _state_lock:
                    video_path = available_videos.get(stream_name)
                    running = stream_name in streams
                if video_path is None:
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
                success = start_stream(video_path, stream_name, loop_count)
                self.send_json({"success": success})

            elif action == 'stop':
                success = stop_stream(stream_name)
                self.send_json({"success": success})

            else:
                self.send_json({"error": "Unknown action"}, 400)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


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
    """Return {filename: (mtime, size)} for all non-ignored files in VIDEOS_DIR."""
    files = {}
    for video_path in VIDEOS_DIR.iterdir():
        if not video_path.is_file() or is_ignored(video_path):
            continue
        stat = video_path.stat()
        files[video_path.name] = (stat.st_mtime, stat.st_size)
    return files


def watch_directory():
    """Watch directory for changes using polling (works on network filesystems).

    Changed/new files are debounced: we wait for (mtime, size) to be stable for
    DEBOUNCE_STABLE_POLLS consecutive polls before acting, so a long file copy
    doesn't trigger a storm of stop+start cycles.

    Deletions are processed before creates/modifies in each tick so that a
    rename (delete-then-create with the same stream_name) frees the slot before
    the new file tries to claim it.
    """
    log.info("Watching %s for changes (polling mode)...", VIDEOS_DIR)

    last_cleanup = time.time()
    try:
        known_files = get_video_files()
    except Exception as e:
        log.error("Error scanning directory: %s", e)
        known_files = {}

    pending = {}  # filename -> {"mtime": float, "size": int, "stable_polls": int}

    while True:
        time.sleep(POLL_INTERVAL_SEC)

        try:
            current_files = get_video_files()
        except Exception as e:
            log.error("Error scanning directory: %s", e)
            current_files = None

        if current_files is not None:
            # Drop pending entries for files that vanished mid-debounce.
            for filename in list(pending.keys()):
                if filename not in current_files:
                    del pending[filename]

            # Detect deletions (no debounce — gone is gone).
            deletions = [f for f in known_files if f not in current_files]

            # Detect adds/modifies through the debounce buffer.
            ready_creates = []
            ready_modifies = []
            for filename, current in current_files.items():
                prev = known_files.get(filename)
                if prev == current:
                    pending.pop(filename, None)
                    continue
                state = pending.get(filename)
                if state is None or (state["mtime"], state["size"]) != current:
                    pending[filename] = {"mtime": current[0], "size": current[1], "stable_polls": 0}
                else:
                    state["stable_polls"] += 1
                    if state["stable_polls"] >= DEBOUNCE_STABLE_POLLS:
                        if prev is None:
                            ready_creates.append(filename)
                        else:
                            ready_modifies.append(filename)
                        known_files[filename] = current
                        del pending[filename]

            # Process deletions first so renames into a now-free slot work.
            for filename in deletions:
                handle_delete(VIDEOS_DIR / filename)
                known_files.pop(filename, None)

            for filename in ready_creates:
                handle_create(VIDEOS_DIR / filename)
            for filename in ready_modifies:
                handle_modify(VIDEOS_DIR / filename)

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
