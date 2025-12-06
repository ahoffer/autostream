# Autostream

Automatic RTSP video streaming server with hot-reload file discovery.

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

2. **Start the container**:
   ```bash
   make build up
   ```

3. **Access your streams** at:
   ```
   rtsp://localhost:9554/<video-name>
   ```

That's it! Videos are automatically discovered and streamed. Check logs for exact URLs:
```bash
docker logs autostream
```

## How It Works

Autostream automatically:
- **Scans** the `videos/` directory on startup
- **Starts streaming** each video file via RTSP
- **Watches** for new files added at runtime
- **Removes streams** when files are deleted

## Stream URLs

Videos are accessible at:
- **From host**: `rtsp://localhost:9554/<stream-name>`
- **From Docker network**: `rtsp://autostream:8554/<stream-name>`

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `rtsp://localhost:9554/my_video_1080p`
- `test-stream.mkv` → `rtsp://localhost:9554/test-stream`
- `__demo__.mov` → `rtsp://localhost:9554/demo`

## Configuration

Edit `.env` file:

```bash
VERSION=0.4
CONTAINER_NAME=autostream
MEDIAMTX_RTSP_PORT=8554
# LOG_LEVEL=debug  # Uncomment for verbose FFmpeg/MediaMTX output
```

### Log Levels

- **Default (info)**: Only supervisor messages showing stream URLs
- **Debug**: Full FFmpeg encoding output + MediaMTX connection logs

Enable debug mode:
```bash
# Uncomment in .env
LOG_LEVEL=debug
```

## Port Mapping

| Service | Container Port | Host Port |
|---------|---------------|-----------|
| RTSP | 8554 | 9554 |
| HLS | 8888 | 9322 |
| RTP | 8000/udp | 9000/udp |
| RTCP | 8001/udp | 9001/udp |

## Supported Formats

Any video format supported by FFmpeg: MP4, MKV, AVI, MOV, WEBM, FLV, TS, etc.

## Commands

```bash
make build      # Build container
make up         # Start container
make build up   # Build and start

docker compose down         # Stop container
docker logs -f autostream   # Follow logs
```

## Testing Streams

**Using ffplay:**
```bash
ffplay rtsp://localhost:9554/my_video
```

**Using VLC:**
Open VLC → Media → Open Network Stream → Enter RTSP URL

**Using ffprobe:**
```bash
ffprobe rtsp://localhost:9554/my_video
```

## File Exclusions

- Hidden files (starting with `.`) are automatically skipped
- All other files are assumed to be playable videos

## Docker Network

The container runs on the `octo-cx-network` Docker network. Other containers on this network can access streams using the container hostname:

```
rtsp://autostream:8554/<stream-name>
```
