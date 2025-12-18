#!/usr/bin/env python3
"""
Stream supervisor: watches /app/videos and manages FFmpeg streaming processes
Includes HTTP API for stream control
"""
import os
import re
import subprocess
import time
import socket
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from datetime import datetime
from inotify_simple import INotify, flags

VIDEOS_DIR = Path("/app/videos")
STREAM_VIDEO_SCRIPT = "stream-video.sh"
RTSP_PORT = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
API_PORT = int(os.getenv("STREAM_API_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()

# Get hostname from environment (required)
HOSTNAME = os.getenv("CONTAINER_NAME")
if not HOSTNAME:
    raise RuntimeError("CONTAINER_NAME environment variable is not set")

# Track running streams: {stream_name: {"process": process, "video_path": path, "loop_count": int}}
streams = {}
# Track available videos: {stream_name: video_path}
available_videos = {}
# Track last used loop count per stream (persists across stop/start)
stream_loop_counts = {}


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def sanitize_name(filepath):
    """Convert filename to valid stream name"""
    # Remove extension
    name = Path(filepath).stem

    # Replace invalid characters with underscore
    name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)

    # Convert to lowercase
    name = name.lower()

    # Collapse multiple underscores/dashes
    name = re.sub(r'_+', '_', name)
    name = re.sub(r'-+', '-', name)

    # Strip leading/trailing underscores/dashes
    name = name.strip('_-')

    return name


def wait_for_mediamtx():
    log(f"Waiting for MediaMTX to be available on port {RTSP_PORT}...")
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("localhost", RTSP_PORT))
                break
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(1)
    log("MediaMTX is ready")


def start_stream(video_path, stream_name, loop_count=-1):
    if stream_name in streams:
        log(f"Stream already running: {stream_name}")
        return False

    try:
        cmd = [STREAM_VIDEO_SCRIPT, str(video_path), stream_name, str(loop_count)]
        # Show FFmpeg output only in debug mode
        if LOG_LEVEL == "debug":
            process = subprocess.Popen(cmd)
        else:
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        streams[stream_name] = {
            "process": process,
            "video_path": str(video_path),
            "loop_count": loop_count
        }
        available_videos[stream_name] = str(video_path)
        stream_loop_counts[stream_name] = loop_count

        rtsp_url = f"rtsp://{HOSTNAME}:{RTSP_PORT}/{stream_name}"
        loop_str = "infinite" if loop_count == -1 else f"{loop_count + 1}x"
        log(f"Now playing {rtsp_url} ({loop_str})")
        return True
    except Exception as e:
        log(f"Failed to start stream {stream_name}: {e}")
        return False


def stop_stream(stream_name):
    if stream_name not in streams:
        log(f"Stream not found: {stream_name}")
        return False

    stream_info = streams[stream_name]
    process = stream_info["process"]
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log(f"Stopped stream: {stream_name}")
    except Exception as e:
        log(f"Error stopping stream {stream_name}: {e}")

    del streams[stream_name]
    return True


def get_stream_status():
    """Get status of all streams"""
    result = []
    for name, video_path in available_videos.items():
        is_running = name in streams
        loop_count = stream_loop_counts.get(name, -1)
        result.append({
            "name": name,
            "video_path": video_path,
            "running": is_running,
            "loop_count": loop_count,
            "rtsp_url": f"rtsp://{HOSTNAME}:{RTSP_PORT}/{name}"
        })
    return result


def scan_videos():
    """Scan video directory and populate available_videos"""
    if not VIDEOS_DIR.exists():
        log(f"Directory does not exist: {VIDEOS_DIR}")
        return

    for video_path in VIDEOS_DIR.iterdir():
        if not video_path.is_file() or video_path.name.startswith('.'):
            continue
        stream_name = sanitize_name(video_path)
        available_videos[stream_name] = str(video_path)


def sync_videos():
    """Scan videos and start all streams"""
    log(f"Scanning {VIDEOS_DIR} for video files...")
    scan_videos()

    count = 0
    for stream_name, video_path in available_videos.items():
        if start_stream(video_path, stream_name):
            count += 1

    log(f"Initial sync complete: {count} streams started")


def handle_create(filepath):
    path = Path(filepath)

    # Skip hidden files
    if path.name.startswith('.'):
        return

    stream_name = sanitize_name(path)
    available_videos[stream_name] = str(path)
    log(f"New video detected: {path.name}")
    start_stream(path, stream_name)


def handle_delete(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)
    log(f"Video deleted: {path.name}")
    stop_stream(stream_name)
    if stream_name in available_videos:
        del available_videos[stream_name]


def cleanup_dead_processes():
    dead_streams = []
    for stream_name, info in streams.items():
        if info["process"].poll() is not None:
            log(f"Process ended: {stream_name}")
            dead_streams.append(stream_name)

    for stream_name in dead_streams:
        del streams[stream_name]


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>Stream Control</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               max-width: 800px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #eee; }
        h1 { color: #00d4ff; }
        .stream { background: #16213e; border-radius: 8px; padding: 15px; margin: 10px 0;
                  display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
        .stream-name { font-weight: bold; font-size: 1.1em; flex: 1; min-width: 150px; }
        .stream-status { padding: 4px 12px; border-radius: 12px; font-size: 0.85em; }
        .running { background: #00c853; color: #000; }
        .stopped { background: #ff5252; color: #fff; }
        .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        button { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer;
                 font-size: 0.9em; transition: opacity 0.2s; }
        button:hover { opacity: 0.8; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-start { background: #00c853; color: #000; }
        .btn-stop { background: #ff5252; color: #fff; }
        select { padding: 8px; border-radius: 4px; background: #0f3460; color: #fff; border: 1px solid #00d4ff; }
        .rtsp-url { font-size: 0.8em; color: #888; width: 100%; margin-top: 5px; }
        .refresh { background: #00d4ff; color: #000; margin-bottom: 15px; }
    </style>
</head>
<body>
    <h1>Stream Control</h1>
    <div style="margin-bottom: 15px;">
        <button class="btn-stop" onclick="stopAll()">Stop All</button>
        <button class="btn-start" onclick="startAll()">Start All</button>
    </div>
    <div id="streams"></div>
    <script>
        async function loadStreams() {
            const container = document.getElementById('streams');
            try {
                const res = await fetch('/api/streams');
                const streams = await res.json();
                if (!streams.length) {
                    container.innerHTML = '<p>No streams found</p>';
                    return;
                }
                container.innerHTML = streams.map(s => `
                <div class="stream">
                    <span class="stream-name">${s.name}</span>
                    <span class="stream-status ${s.running ? 'running' : 'stopped'}">
                        ${s.running ? 'Running' : 'Stopped'}
                    </span>
                    <div class="controls">
                        <select id="loop-${s.name}">
                            <option value="-1" ${s.loop_count === -1 ? 'selected' : ''}>Loop: Infinite</option>
                            <option value="0" ${s.loop_count === 0 ? 'selected' : ''}>Play: 1x</option>
                            <option value="1" ${s.loop_count === 1 ? 'selected' : ''}>Play: 2x</option>
                            <option value="2" ${s.loop_count === 2 ? 'selected' : ''}>Play: 3x</option>
                            <option value="4" ${s.loop_count === 4 ? 'selected' : ''}>Play: 5x</option>
                            <option value="9" ${s.loop_count === 9 ? 'selected' : ''}>Play: 10x</option>
                        </select>
                        <button class="btn-start" onclick="startStream('${s.name}')" ${s.running ? 'disabled' : ''}>Start</button>
                        <button class="btn-stop" onclick="stopStream('${s.name}')" ${!s.running ? 'disabled' : ''}>Stop</button>
                    </div>
                    <div class="rtsp-url">${s.rtsp_url}</div>
                </div>
            `).join('');
            } catch (err) {
                container.innerHTML = '<p style="color:#ff5252">Error loading streams: ' + err.message + '</p>';
            }
        }

        async function startStream(name) {
            const loopCount = document.getElementById('loop-' + name).value;
            await fetch('/api/streams/' + name + '/start?loop=' + loopCount, {method: 'POST'});
            loadStreams();
        }

        async function stopStream(name) {
            await fetch('/api/streams/' + name + '/stop', {method: 'POST'});
            loadStreams();
        }

        async function stopAll() {
            await fetch('/api/streams/stop-all', {method: 'POST'});
            loadStreams();
        }

        async function startAll() {
            await fetch('/api/streams/start-all', {method: 'POST'});
            loadStreams();
        }

        loadStreams();
        setInterval(loadStreams, 5000);
    </script>
</body>
</html>
"""


class StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

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
            self.send_html(HTML_PAGE)
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

            # Handle stop-all and start-all
            if stream_name == 'stop-all':
                for name in list(streams.keys()):
                    stop_stream(name)
                self.send_json({"success": True})
                return

            if stream_name == 'start-all':
                for name, video_path in available_videos.items():
                    if name not in streams:
                        loop_count = stream_loop_counts.get(name, -1)
                        start_stream(video_path, name, loop_count)
                self.send_json({"success": True})
                return

            if action == 'start':
                if stream_name not in available_videos:
                    self.send_json({"error": "Stream not found"}, 404)
                    return
                query = parse_qs(parsed.query)
                loop_count = int(query.get('loop', ['-1'])[0])
                # Stop first if already running
                if stream_name in streams:
                    stop_stream(stream_name)
                success = start_stream(available_videos[stream_name], stream_name, loop_count)
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
    server = HTTPServer(('0.0.0.0', API_PORT), StreamHandler)
    log(f"Stream Control UI: http://localhost:9080")
    server.serve_forever()


def watch_directory():
    inotify = INotify()
    watch_flags = flags.CREATE | flags.DELETE | flags.MOVED_TO | flags.MOVED_FROM
    inotify.add_watch(str(VIDEOS_DIR), watch_flags)

    log(f"Watching {VIDEOS_DIR} for changes...")

    last_cleanup = time.time()

    while True:
        # Non-blocking read with timeout
        events = inotify.read(timeout=1000)  # 1 second timeout

        for event in events:
            filepath = VIDEOS_DIR / event.name

            if event.mask & (flags.CREATE | flags.MOVED_TO):
                # Brief delay to ensure file is fully written
                time.sleep(0.5)
                handle_create(filepath)
            elif event.mask & (flags.DELETE | flags.MOVED_FROM):
                handle_delete(filepath)

        # Periodic cleanup every 30 seconds
        if time.time() - last_cleanup > 30:
            cleanup_dead_processes()
            last_cleanup = time.time()


def main():
    log("Stream supervisor starting...")

    # Wait for MediaMTX
    wait_for_mediamtx()

    # Start API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    # Initial sync
    sync_videos()

    # Watch for changes
    watch_directory()


if __name__ == "__main__":
    main()
