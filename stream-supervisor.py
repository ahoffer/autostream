#!/usr/bin/env python3
"""
Stream supervisor: watches /app/videos and manages FFmpeg streaming processes
"""
import os
import re
import subprocess
import time
import socket
from pathlib import Path
from datetime import datetime
from inotify_simple import INotify, flags

VIDEOS_DIR = Path("/app/videos")
STREAM_VIDEO_SCRIPT = "stream-video.sh"
RTSP_PORT = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()

# Get hostname from environment (required)
HOSTNAME = os.getenv("CONTAINER_NAME")
if not HOSTNAME:
    raise RuntimeError("CONTAINER_NAME environment variable is not set")

# Track running streams: {stream_name: process}
streams = {}


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


def start_stream(video_path, stream_name):
    if stream_name in streams:
        log(f"Stream already running: {stream_name}")
        return False

    try:
        # Show FFmpeg output only in debug mode
        if LOG_LEVEL == "debug":
            process = subprocess.Popen(
                [STREAM_VIDEO_SCRIPT, str(video_path), stream_name]
            )
        else:
            process = subprocess.Popen(
                [STREAM_VIDEO_SCRIPT, str(video_path), stream_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        streams[stream_name] = process
        rtsp_url = f"rtsp://{HOSTNAME}:{RTSP_PORT}/{stream_name}"
        #log(f"Starting {stream_name} -> {video_path}")
        log(f"Now playing {rtsp_url}")
        return True
    except Exception as e:
        log(f"Failed to start stream {stream_name}: {e}")
        return False


def stop_stream(stream_name):
    if stream_name not in streams:
        log(f"Stream not found: {stream_name}")
        return False

    process = streams[stream_name]
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


def sync_videos():
    log(f"Scanning {VIDEOS_DIR} for video files...")

    if not VIDEOS_DIR.exists():
        log(f"Directory does not exist: {VIDEOS_DIR}")
        return

    count = 0
    for video_path in VIDEOS_DIR.iterdir():
        # Skip directories and hidden files
        if not video_path.is_file() or video_path.name.startswith('.'):
            continue

        stream_name = sanitize_name(video_path)
        if start_stream(video_path, stream_name):
            count += 1

    log(f"Initial sync complete: {count} streams started")


def handle_create(filepath):
    path = Path(filepath)

    # Skip hidden files
    if path.name.startswith('.'):
        return

    stream_name = sanitize_name(path)
    log(f"New video detected: {path.name}")
    start_stream(path, stream_name)


def handle_delete(filepath):
    path = Path(filepath)
    stream_name = sanitize_name(path)
    log(f"Video deleted: {path.name}")
    stop_stream(stream_name)


def cleanup_dead_processes():
    dead_streams = []
    for stream_name, process in streams.items():
        if process.poll() is not None:
            log(f"Process died: {stream_name}")
            dead_streams.append(stream_name)

    for stream_name in dead_streams:
        del streams[stream_name]


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

    # Initial sync
    sync_videos()

    # Watch for changes
    watch_directory()


if __name__ == "__main__":
    main()
