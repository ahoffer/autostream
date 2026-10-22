# Autostream

Automatic video streaming server with web-based stream control and hot-reload
file discovery. Each video is published three ways at once: RTSP and HLS (through
MediaMTX, for playback) and MPEG-TS over UDP (which preserves KLV/MISB timed
metadata that RTSP and HLS cannot carry).

## Quickstart

1. **Create your local configuration** (once per checkout), then edit `.env`
   for your environment:
   ```bash
   cp .env.example .env
   ```

2. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

3. **Build and deploy** via Docker Compose:
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

## Passthrough and transcode

Each RTSP or UDP output has its own mode and settings, chosen in the UI.
Changing an enabled output applies its new choices independently. A stopped
output stays stopped and uses its saved choices on the next Start. The modes:

- **Passthrough** (`Video: Passthrough`): the source video is sent unmodified —
  no decode, no encode, near-zero CPU. H.264, H.265, MPEG-2, MPEG-4 part 2
  and MJPEG can pass through (the codecs MediaMTX carries over RTSP). Only
  H.264 files default to passthrough: the others reach RTSP consumers but not
  browser HLS, so they take an explicit request — useful for imitating a
  camera with that encoder. Files with any other video codec always
  transcode; a passthrough request on them is kept but runs as transcode,
  with the reason shown in the UI. Note that
  passthrough streams follow the source file's keyframe cadence (players join
  and recover at source keyframes) instead of the enforced 1-second GOP, and
  `MAX_VIDEO_BITRATE` does not apply.
- **Transcode** (`Video: Transcode`): re-encode to H.264 with a clean GOP
  (fixes loop-point artifacts) and the bitrate cap applied, using the
  `libx264` software encoder. Two encoder settings sit
  beside the mode in the UI and only apply when transcoding (they are
  disabled under passthrough): `FPS`, the output frame rate (1 to 120, the
  UI/API limit), which defaults to the file's own rate; and `GOP`, the keyframe interval in frames, which
  defaults to one second at the output rate (25 for 25 fps). A shorter GOP
  means faster player join and loop recovery at some bitrate cost.

Settings edits are held until Apply (Cancel puts the previous values back);
Apply saves and applies changes only to that output. The command display shows
the actual running command, or a prepared preview when stopped. If the old
process cannot exit, its command remains visible alongside the requested
settings and an error; a replacement cannot reuse its destination.

Audio is handled automatically in both modes: copied when the source is
already AAC, re-encoded to AAC otherwise.

## Video encoding

Transcoding uses software decoding and `libx264` encoding on every machine.
The container does not map GPU devices or install GPU drivers, and there is
no hardware encoder detection or selection.

**Project note (2026-09-29):** Hardware acceleration was removed after a
memory leak in looping QSV decoding exhausted host RAM and swap, causing
host-wide OOM kills and application crashes. The measured leak was in
decoding; removing hardware encoding as well is the project's reliability
decision. FFmpeg 8.1.2 still reproduced the leak. See
[the incident measurements](docs/memory-leak.md). Software transcoding uses
more CPU.

Compose limits the entire container to 6 GiB of RAM with no swap. This
contains any future runaway allocations.
For host socket-memory diagnostics, run `python3 socket_memory.py` to capture
host slab counters and socket queues/owners in every running container once
per minute. It requires Docker access and host `ss`; logs rotate under
`.generated/socket-memory.jsonl` (approximately 80 MiB maximum). Use `--once`
for one snapshot, and Ctrl-C to stop continuous capture.

The UI shows the encoder separately for each output. Each output's ffmpeg log is at
`/tmp/ffmpeg-logs/<stream>.<output>.log` inside the container.

## Outputs

Each source file has two independently configured outputs, shown as rows in the
UI with their own settings, Start, and Stop:

- **rtsp** publishes to MediaMTX, which serves it as RTSP and HLS. HLS has no
  separate process: it is up whenever rtsp is.
- **udp** pushes the KLV-preserving MPEG-TS feed to `OUTPUT_HOST`.

Each output shows one of these states: **Running**; **Crashing** (the
process died unexpectedly and was relaunched; the last lines of its ffmpeg
log are shown, and the state clears once it has stayed up for a minute);
**Restarting** (died, relaunch pending on the next sweep); **Starting**;
**Waiting** (UDP destination DNS is unresolved; a DNS answer does not prove receiver health);
**Failed** (could not be started, with the reason: no UDP port left, or
the file has no video track, which is what a non-video file or a copy still
in progress looks like; it is retried when the file changes); **Stopped**;
**Completed** (a finite loop played to the end); **Stopping** (a disabled
process has not exited); **Retiring** (a removed source still occupies its destination).

Stopping one output leaves the other running, and the choice is remembered:
a stopped output stays down through dead-process sweeps, settings changes,
and container restarts until it is started again. Start All / Stop All apply to
every present output; Start All preserves already-running processes. Both processes decode and encode
independently, so a transcoded stream costs about twice the CPU it
did as a single process.

The Command button on an output row shows the exact ffmpeg command line that
output runs (or would run), for inspection; every flag is explained in the
table at the bottom of `ffmpegcmd.py`.

## State

`state/streams.json` (bind-mounted to `/app/state`) remembers each output's
mode, loop, FPS, GOP, enabled choice, and encoding defaults, plus the UDP
port assigned to each stream name. Settings and the port of a video that was
removed are kept so the same file gets them back when copied in again; once
the port range is full, the longest-removed file's port (and settings) are
given to the newcomer. A file whose stem sanitizes to nothing (for example
`!!!.mp4`) has an invalid stream name and is skipped.
Delete the state file while the container is stopped to reset everything,
including port assignments.

Version 2 stores independent output records. On first startup with a version 1
file, the store copies the former shared settings to both outputs, preserves
independent enabled flags and UDP assignments, validates the result, and backs
up the original as `streams.json.v1.bak` before replacing it. Later edits do not
synchronize the two records. Migration runs at startup, not during a build.

## Stream URLs

Other containers on `octo-cx-network` reach each stream through the `autostream`
service on the ports configured in `.env`:
- **RTSP**: `rtsp://autostream:${MEDIAMTX_RTSP_PORT}/<stream-name>` (default `8554`)
- **HLS**: `http://autostream:${MEDIAMTX_HLS_PORT}/<stream-name>/index.m3u8` (default `8888`)
- **UDP (KLV)**: `udp://${OUTPUT_HOST}:<port>` — MPEG-TS with KLV/data streams
  preserved. Each stream gets its own port from `${UDP_BASE_PORT}` through
  `${UDP_LAST_PORT}`; the exact port per stream is shown in the control UI and
  the `/api/streams` output. A port stays assigned to its stream name (see
  "State") so a consumer's registration keeps working across restarts, so a
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
registration step beyond adding the stream. Keep `UDP_BASE_PORT`/`UDP_LAST_PORT`
inside video-streaming's own ingest range (`UDP_START_PORT`..`UDP_END_PORT`,
`40000-40100` by default) or it will not be listening where autostream pushes.

UDP is push, not pull. If `OUTPUT_HOST` does not resolve (for example the
cx-search stack isn't running), autostream logs a warning and holds the udp
output back rather than failing — it starts on its own once the consumer is
reachable, without touching the rtsp output. For standalone use, set `OUTPUT_HOST` to any
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

Copy `.env.example` to `.env` if you have not created it yet, then edit `.env`
for your environment. `.env` is ignored by Git; `.env.example` contains the
shared defaults. When adding configuration variables, update `.env.example`
and add the new settings to existing installations' local `.env` files.

Docker Compose reads `.env` directly and passes the whole file through to the
container; `mediamtx.yml` is mounted in as-is. `.env` is the single source of
truth: every variable must be set, and the only one that may be empty is
`MAX_VIDEO_BITRATE` (empty disables the cap). The Default column shows the
shipped `.env.example` values.

| Variable | Description | Default |
|----------|-------------|---------|
| `CONTAINER_NAME` | Image name and service/hostname used in stream URLs | `autostream` |
| `VERSION` | Image version tag | `2.2.2` (see `.env.example`) |
| `MAX_VIDEO_BITRATE` | Cap video bitrate (for example `3M`, `5M`); transcode mode only — passthrough sends the source bitrate unchanged | `2M` |
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

## Python code structure

`stream_supervisor.py` owns all output changes and persistence. HTTP threads
submit operations to a bounded queue. One background thread runs the existing
directory polling/stability checker and submits detached observations to that
same queue. The supervisor executes work in order and publishes JSON snapshots;
HTTP GET never walks live outputs. There is no lifecycle state machine or
per-output synchronization protocol.

| Module | Responsibility |
|--------|----------------|
| `appconfig.py` | Immutable startup configuration |
| `outputregistry.py` | Output lookup, collisions, stable UDP assignments, and removed-source cleanup |
| `mediaoutput.py` | Independent settings, start/stop, finite completion, and recovery |
| `ffmpegcmd.py` | Media probing, encoding decisions, and command syntax |
| `ffmpegprocess.py` | Popen, bounded shutdown, and file-backed diagnostics |
| `statefile.py` | Single-writer persistence, migration, and atomic replacement |
| `streamapi.py` | HTTP validation, queued mutations, and cached status/UI serving |
| `streamresponses.py` | Format detached output descriptions |
| `filewatch.py` | Background polling, stability checking, and present/stable inventories |
| `udphost.py` | DNS cache shared by independently configured outputs |

A Stop arriving during preparation waits for that operation to finish, then
executes. Slow probing, DNS, and bounded process shutdown can delay later work;
directory scanning runs independently. The watcher uses the existing two-second
poll interval and four-second stability check. Scan errors do not mean files
were deleted. A process that cannot stop keeps its identity and port occupied.

Mutations use `/api/streams/{name}/outputs/{rtsp|udp}/{start|stop|settings}`.
The former source-level mutation routes return HTTP 410 and direct callers to
an output. `/api/streams/start-all` and `/api/streams/stop-all` remain. Queue
saturation returns 503; operations that cannot finish return 409; storage
failures return 500. Requests are not automatically retried.

Each process captures stdout/stderr in a temporary file, removed after exit.
Observation forwards bounded chunks to the injected logger and retains a 4 KiB
tail. Large diagnostic bursts may be truncated with a log notice. Per-output
logs rotate at 2 MB with one backup; the temporary capture file can grow for the
lifetime of a run. There is no pipe-reader thread.

See [the RDD design](docs/responsibility-driven-design.md) and
[the refactor measurements](docs/refactor-metrics.md).

Run the tests with the Python standard library:

```bash
python3 -B -m unittest discover -s tests -v
```

## Docker Compose

The Dockerfile pins the MediaMTX release and rebuilds its binary with patched
Go and networking dependencies. The release archive is verified by SHA-256;
update its checksum together with `MEDIAMTX_VERSION`. The build also applies
available Alpine package fixes before installing Python. The compiler and
source files stay in the build stage, outside the runtime image.

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

To watch from the host, force RTSP over TCP:

```bash
ffplay -rtsp_transport tcp rtsp://localhost:9554/cargoship2
```

ffplay defaults to RTP over UDP, and MediaMTX advertises its internal RTP
ports (8000/8001) while the host sees them as 9000/9001, so the UDP
negotiation lands on the wrong ports and playback stalls. Over TCP the
video rides the RTSP connection and the mapping does not matter.

## Supported Formats

Every file in `videos/` gets a stream except hidden files and READMEs — there
is no format allowlist. FFmpeg must be able to decode the file (MP4, MKV, AVI,
MOV, WEBM, FLV, TS, and so on); a non-video file gets a stream slot whose
ffmpeg process fails and is retried periodically.
