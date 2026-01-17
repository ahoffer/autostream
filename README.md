# Autostream

Automatic RTSP video streaming server with web-based stream control and hot-reload file discovery.

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```sh
   cp your-video.mp4 videos/
   ```

2. **Build and deploy**:
   ```sh
   make build    # Only needed once, or after code changes
   make up
   ```

## How It Works

Autostream automatically:
- **Scans** the `videos/` directory on startup
- **Starts streaming** each video file via RTSP (infinite loop by default)
- **Watches** for new files added at runtime
- **Removes streams** when files are deleted

## Stream URLs

Videos are accessible via the `autostream` service:
- **RTSP**: `rtsp://autostream:8554/<stream-name>`
- **HLS**: `http://autostream:8888/<stream-name>/index.m3u8`

**Example:** If you add `sailboat.mp4` to the `videos/` directory:
```
rtsp://autostream:8554/sailboat
```

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Edit `.env` file:

```sh
VERSION=0.6                    # Docker image version
CONTAINER_NAME=autostream      # Container name
MEDIAMTX_RTSP_PORT=8554       # Internal RTSP port
```

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

```sh
make build      # Build container image with nerdctl
make export     # Save image to autostream.tar
make import     # Import tar into k3s (requires sudo)
make up         # Deploy to Kubernetes (namespace: octocx)
make down       # Remove from Kubernetes
```

## Kubernetes (k3s) Setup

nerdctl and k3s use separate containerd namespaces. Images built with nerdctl are not visible to k3s until exported and imported.

**Code changes** (stream-supervisor.py, Dockerfile, etc.):
```sh
make build          # Build the image
make export         # Save to autostream.tar
make import         # Import into k3s (prompts for sudo)
make down && make up  # Redeploy
```

**Config changes** (mediamtx.yml only):
```sh
make down && make up  # ConfigMap is recreated from local file
```

## Supported Formats

Any video format supported by FFmpeg: MP4, MKV, AVI, MOV, WEBM, FLV, TS, etc.
