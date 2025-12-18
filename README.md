# Autostream

Automatic RTSP video streaming server with web-based stream control and hot-reload file discovery.

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

2. **Build and start**:
   ```bash
   make build    # Only needed once, or after code changes
   make up
   ```

3. **Control your streams** via the web UI:
   ```
   http://localhost:9080
   ```

4. **Access streams** via RTSP:
   ```
   rtsp://localhost:9554/<video-name>
   ```

## Web Control UI

A built-in web interface at **http://localhost:9080** lets you:

- **Start/Stop** individual streams
- **Stop All / Start All** streams at once
- **Set playback count**: Infinite loop, or play 1x, 2x, 3x, 5x, 10x times
- **View status** of all streams (auto-refreshes every 5 seconds)

The playback count setting persists when you stop a stream.

## How It Works

Autostream automatically:
- **Scans** the `videos/` directory on startup
- **Starts streaming** each video file via RTSP (infinite loop by default)
- **Watches** for new files added at runtime
- **Removes streams** when files are deleted

## Stream URLs

Videos are accessible at:
- **RTSP (from host)**: `rtsp://localhost:9554/<stream-name>`
- **RTSP (Docker network)**: `rtsp://autostream:8554/<stream-name>`
- **HLS**: `http://localhost:9322/<stream-name>/index.m3u8`

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Edit `.env` file:

```bash
VERSION=0.6                    # Docker image version
CONTAINER_NAME=autostream      # Container name
MEDIAMTX_RTSP_PORT=8554       # Internal RTSP port
```

## Port Mapping

| Service | Host Port | Description |
|---------|-----------|-------------|
| RTSP | 9554 | Main streaming protocol |
| HLS | 9322 | HTTP Live Streaming |
| Web UI | 9080 | Stream Control Interface |

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/streams` | GET | List all streams with status |
| `/api/streams/{name}/start?loop=N` | POST | Start stream (-1=infinite, 0=1x, 1=2x, etc.) |
| `/api/streams/{name}/stop` | POST | Stop stream |
| `/api/streams/start-all` | POST | Start all stopped streams |
| `/api/streams/stop-all` | POST | Stop all running streams |

## Commands

```bash
make build      # Build container image
make up         # Start container
make down       # Stop container
```

## Supported Formats

Any video format supported by FFmpeg: MP4, MKV, AVI, MOV, WEBM, FLV, TS, etc.
