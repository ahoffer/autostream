# Autostream

Automatic RTSP video streaming server with web-based stream control and hot-reload file discovery.

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

2. **Build and deploy**:
   ```bash
   make build    # Only needed once, or after code changes
   make push
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
- **RTSP**: `rtsp://autostream:18554/<stream-name>`
- **HLS**: `http://autostream:18888/<stream-name>/index.m3u8`

**Example:** If you add `sailboat.mp4` to the `videos/` directory:
```
rtsp://autostream:18554/sailboat
```

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Edit `.env` file:

| Variable | Description | Default |
|----------|-------------|---------|
| `K8S_NAMESPACE` | Kubernetes namespace for deployment | `octocx` |
| `VERSION` | Image version tag | `1.0.1` |
| `MAX_VIDEO_BITRATE` | Cap video bitrate (e.g., `3M`, `5M`) | `2M` |
| `MEDIAMTX_RTSP_PORT` | RTSP listener port | `8554` |
| `MEDIAMTX_HLS_PORT` | HLS HTTP port | `8888` |

## Commands

```bash
make build      # Build container image with docker
make push       # Push image to k3s node
make up         # Deploy to Kubernetes (uses K8S_NAMESPACE from .env)
make down       # Remove from Kubernetes
```

## Docker Compose

For local development or standalone deployment:

```bash
# Build the image
make build

# Start the service (processes config from .env)
make compose-up

# View logs
make compose-logs

# Stop the service
make compose-down
```

**Note:** Docker Compose uses an external network `octo-cx-network`. Create it first if it doesn't exist:
```bash
docker network create octo-cx-network
```

### Port Mappings (Docker Compose)

| Service | External Port | Internal Port |
|---------|---------------|---------------|
| RTSP    | 9554          | 8554          |
| HLS     | 9322          | 8888          |
| RTP     | 9000/udp      | 8000/udp      |
| RTCP    | 9001/udp      | 8001/udp      |
| Web UI  | 9080          | 8080          |

Access streams at:
- **RTSP**: `rtsp://localhost:9554/<stream-name>`
- **HLS**: `http://localhost:9322/<stream-name>/index.m3u8`
- **Web UI**: `http://localhost:9080`

## Kubernetes (k3s) Setup

k3s runs its own containerd namespace, so images are pushed into the node to be available.

**Code changes** (stream-supervisor.py, Dockerfile, etc.):
```bash
make build          # Build the image
make push           # Push image to k3s node
make down && make up  # Redeploy
```
## Supported Formats

Any video format supported by FFmpeg: MP4, MKV, AVI, MOV, WEBM, FLV, TS, etc.
