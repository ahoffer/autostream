# Autostream

Automatic video streaming server with web-based stream control and hot-reload
file discovery. Each video is published three ways at once: RTSP and HLS (through
MediaMTX, for playback) and MPEG-TS over UDP (which preserves KLV/MISB timed
metadata that RTSP and HLS cannot carry).

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

2. **Build and deploy** via Docker Compose:
   ```bash
   make build       # Only needed once, or after code changes
   make compose-up
   ```

## How It Works

Autostream automatically:
- **Scans** the `videos/` directory on startup
- **Starts streaming** each video file via RTSP/HLS (through MediaMTX) and
  KLV-preserving MPEG-TS/UDP, looping infinitely by default
- **Watches** for new files added at runtime
- **Removes streams** when files are deleted

## Stream URLs

Other containers on `octo-cx-network` reach each stream through the `autostream`
service on the ports configured in `.env`:
- **RTSP**: `rtsp://autostream:${MEDIAMTX_RTSP_PORT}/<stream-name>` (default `8554`)
- **HLS**: `http://autostream:${MEDIAMTX_HLS_PORT}/<stream-name>/index.m3u8` (default `8888`)
- **UDP (KLV)**: `udp://${OUTPUT_HOST}:<port>` — MPEG-TS with KLV/data streams
  preserved. Each stream gets its own port from `${UDP_BASE_PORT}` through
  `${UDP_LAST_PORT}`; the exact port per stream is shown in the control UI and
  the `/api/streams` output. A port stays assigned for the life of the
  supervisor so a consumer's registration keeps working across restarts, so a
  run that cycles through more videos than the range holds will use it up —
  later streams then publish RTSP/HLS only and say so in the UI.

For access from the host, see "Port Mappings" below.

### KLV / MISB metadata

RTSP and HLS go through MediaMTX, whose track model only carries H.264/AAC — KLV
timed-metadata data streams are dropped (ffmpeg's RTP muxer cannot carry them
either). To preserve KLV, autostream also stream-copies the data streams into an
MPEG-TS feed pushed over **UDP**, the standard MISB/STANAG-4609 transport.

In the octo-cx stack, `OUTPUT_HOST` defaults to the cx-search **video-streaming**
service (on `octo-cx-network`), which listens on `udp://0.0.0.0:<port>`, decodes
the KLV, and publishes it to the AMQP `stream.klv` topic that cx-edge consumes
for geolocation. Add each feed to cx-search like any other stream, using the
`udp://0.0.0.0:<port>` the UI/API reports for it — there is no separate
registration step beyond adding the stream.

`video-streaming` is the cx-search service that ingests video/KLV UDP feeds on
`40000-40100/udp`; see that stack's docs for details.

UDP is push, not pull. If `OUTPUT_HOST` does not resolve (for example the
cx-search stack isn't running), autostream logs a warning and streams RTSP/HLS
only rather than failing — the KLV feed simply starts once the consumer is
reachable and the stream restarts. For standalone use, set `OUTPUT_HOST` to any
reachable consumer or a multicast group like `239.0.0.1`. Verify a feed with, for
example, `ffprobe udp://<host>:<port>` — the KLV stream appears as `Data: klv (KLVA)`.

**Example:** If you add `sailboat.mp4` to the `videos/` directory:
```
rtsp://autostream:8554/sailboat
```

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Edit `.env` file:

Docker Compose reads `.env` directly; `mediamtx.yml` is mounted into the
container as-is. Every variable must be set — `.env` is the single source of
truth and there are no in-code fallbacks. The Default column shows the shipped
`.env` values.

| Variable | Description | Default |
|----------|-------------|---------|
| `CONTAINER_NAME` | Image name and service/hostname used in stream URLs | `autostream` |
| `VERSION` | Image version tag | `2.0.0` (see `.env`) |
| `MAX_VIDEO_BITRATE` | Cap video bitrate (for example `3M`, `5M`) | `2M` |
| `OUTPUT_HOST` | Host/service the KLV UDP feeds are pushed to (cx-search `video-streaming`, or an IP/multicast group) | `video-streaming` |
| `UDP_BASE_PORT` | First UDP port; each stream gets the next one | `40000` |
| `UDP_LAST_PORT` | Last UDP port; streams arriving after the range is used up run RTSP/HLS only | `40100` |
| `MEDIAMTX_RTSP_PORT` | RTSP listener port | `8554` |
| `MEDIAMTX_HLS_PORT` | HLS HTTP port | `8888` |
| `MEDIAMTX_RTP_PORT` | RTP UDP port | `8000` |
| `MEDIAMTX_RTCP_PORT` | RTCP UDP port | `8001` |
| `STREAM_API_PORT` | Stream control UI/API port | `8080` |
| `LOG_LEVEL` | Supervisor log level; `debug` also shows ffmpeg output | `info` |

## Commands

```bash
make build             # Build the container image
make compose-up        # Start via Docker Compose
make compose-down      # Stop the Docker Compose stack
make compose-logs      # Tail Docker Compose logs
make clean             # Remove the config cache

# systemd service (optional; runs the stack on boot)
make systemd-install   # Build the image, then install the systemd unit
make systemd-uninstall # Remove the systemd unit
```

## Docker Compose

Plain `docker compose up -d` also works after the image is built; `make
compose-up` adds config-change detection so `mediamtx.yml` edits recreate the
container when needed.

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

## Supported Formats

Every file in `videos/` gets a stream except hidden files and READMEs — there
is no format allowlist. FFmpeg must be able to decode the file (MP4, MKV, AVI,
MOV, WEBM, FLV, TS, and so on); a non-video file gets a stream slot whose
ffmpeg process fails and is retried periodically.
