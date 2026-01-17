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

## Commands

```bash
make build      # Build container image with docker
make push       # Push image to k3s node
make up         # Deploy to Kubernetes (namespace: octocx)
make down       # Remove from Kubernetes
```

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
