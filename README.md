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
   make up MODE=host   # or MODE=octo-cx; see "Networking modes"
   ```

4. **Point the feeds at their consumer**: open `http://<this host>:8080`,
   expand Settings in the sidebar, and enter the destination host. RTSP and
   HLS play without it; the KLV UDP feeds wait until it is set.

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
  the bitrate cap does not apply.
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
- **udp** pushes the KLV-preserving MPEG-TS feed to the destination host set
  under Settings.

Each output shows one of these states: **Running**; **Crashing** (the
process died unexpectedly and was relaunched; the last lines of its ffmpeg
log are shown, and the state clears once it has stayed up for a minute);
**Restarting** (died, relaunch pending on the next sweep); **Starting**;
**Waiting** (the udp output lacks a destination host or a port, or the
destination name is unresolved; a DNS answer does not prove receiver health);
**Failed** (could not be started, with the reason: the file has no video
track, which is what a non-video file or a copy still in progress looks like;
it is retried when the file changes); **Stopped**;
**Completed** (a finite loop played to the end); **Stopping** (a disabled
process has not exited); **Retiring** (a removed source still occupies its destination).

Stopping one output leaves the other running, and the choice is remembered:
a stopped output stays down through dead-process sweeps, settings changes,
and container restarts until it is started again. Start All / Stop All apply to
every present output; Start All preserves already-running processes and counts
a waiting udp output as started. Both processes decode and encode
independently, so a transcoded stream costs about twice the CPU it
did as a single process.

The Command button on an output row shows the exact ffmpeg command line that
output runs (or would run), for inspection; every flag is explained in the
table at the bottom of `ffmpegcmd.py`.

## State

`state/streams.json` (bind-mounted to `/app/state`) remembers each output's
mode, loop, FPS, GOP, and enabled choice, the UDP port assigned to each
stream name, and the values from the Settings panel. Choices and the port of a video that was
removed are kept so the same file gets them back when copied in again; once
the port range is full, the longest-removed file's port (and settings) are
given to the newcomer. A file whose stem sanitizes to nothing (for example
`!!!.mp4`) has an invalid stream name and is skipped.
Delete the state file while the container is stopped to reset everything,
including port assignments.

Each output has its own record: the RTSP and UDP outputs of one video do not
share settings. The destination host and the bitrate cap are never stored per
output; they follow the Settings panel, so a change there reaches every output
when saved. A state file written by an earlier release is rejected at
startup; delete it to start fresh.

## Stream URLs

Any client that can reach the publisher host plays the streams at the
advertised host on the ports configured in `.env`:
- **RTSP**: `rtsp://<advertised host>:${MEDIAMTX_RTSP_PORT}/<stream-name>` (default `8554`)
- **HLS**: `http://<advertised host>:${MEDIAMTX_HLS_PORT}/<stream-name>/index.m3u8` (default `8888`)
- **UDP (KLV)**: `udp://<destination host>:<port>` — MPEG-TS with KLV/data streams
  preserved, pushed to the destination host set under Settings. Each stream
  gets its own port from the UDP range set there; the exact port per stream is
  shown in the control UI and the `/api/streams` output. A port stays assigned
  to its stream name (see "State") so a consumer's registration keeps working
  across restarts, so a run that cycles through more videos than the range
  holds will use it up — later streams then publish RTSP/HLS only and say so
  in the UI.

The advertised host is whatever address you reached the UI by, unless the
Settings panel names one. Set it when the consumer must use a different
address than you did, for example a container that cannot resolve the name
your browser used. Containers resolve neither `.local` names nor another
host's container names; an IP address or a name the LAN's DNS knows works
everywhere.

### KLV / MISB metadata

RTSP and HLS go through MediaMTX, whose track model only carries H.264/AAC — KLV
timed-metadata data streams are dropped (ffmpeg's RTP muxer cannot carry them
either). To preserve KLV, autostream also stream-copies the data streams into an
MPEG-TS feed pushed over **UDP**, the standard MISB/STANAG-4609 transport.

The consumer is cx-search's **video-streaming** service, which listens on
`udp://0.0.0.0:<port>`, decodes the KLV, and publishes it to the AMQP
`stream.klv` topic that cx-edge consumes for geolocation. Set the destination
host under Settings to the address of the host running that service, as
reachable from the publisher host. Add each feed to cx-search like any other
stream, using `udp://0.0.0.0:<port>` with the port the UI/API reports for it —
there is no separate registration step beyond adding the stream. Keep the UDP
range inside video-streaming's own ingest range (`UDP_START_PORT`..`UDP_END_PORT`,
`40000-40100` by default, which Settings also defaults to) or it will not be
listening where autostream pushes. The Compose form of the consumer publishes
that range into its container; the Kubernetes chart exposes no UDP today, so
the KLV path there needs a UDP NodePort, LoadBalancer, or host networking on
the consumer side. Its multicast-input allocator draws from the same range and
cannot see ports autostream already uses.

UDP is push, not pull. An unset destination host, or one whose name does not
resolve, holds the udp output back in Waiting rather than failing — it starts
on its own once the destination is set and resolvable, without touching the
rtsp output. An IP address always resolves, so a consumer that has moved or
stopped shows as Running here while the packets go nowhere; check the
consumer's logs. For standalone use, set the destination host to any reachable
consumer or a multicast group like `239.0.0.1`. Verify a feed on the receiving
host with, for example, `ffprobe udp://0.0.0.0:<port>` — the KLV stream appears
as `Data: klv (KLVA)`.

**Example:** If you add `sailboat.mp4` to the `videos/` directory:
```
rtsp://<advertised host>:8554/sailboat
```

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Two kinds of configuration exist. `.env` holds what the container needs before
it starts: the image, the networking mode, the listener ports, and the log
level. Everything about where feeds go and how streams are advertised is set
at run time in the web UI under **Settings** and saved with the state.

### `.env`

Copy `.env.example` to `.env` if you have not created it yet, then edit `.env`
for your environment. `.env` is ignored by Git; `.env.example` contains the
shared defaults. When adding configuration variables, update `.env.example`
and add the new settings to existing installations' local `.env` files.

Docker Compose reads `.env` directly and passes the whole file through to the
container; `mediamtx.yml` is copied into the image, so editing it means
`make build`. Every variable must be set. The
Default column shows the shipped `.env.example` values.

| Variable | Description | Default |
|----------|-------------|---------|
| `CONTAINER_NAME` | Image and container name | `autostream` |
| `VERSION` | Image version tag | `2.3.0` (see `.env.example`) |
| `COMPOSE_FILE` | Networking mode: the base file plus `docker-compose.host.yml` or `docker-compose.octo-cx.yml` (see "Networking modes") | host |
| `MEDIAMTX_RTSP_PORT` | RTSP listener port | `8554` |
| `MEDIAMTX_HLS_PORT` | HLS HTTP port | `8888` |
| `MEDIAMTX_RTP_PORT` | RTP UDP port | `8000` |
| `MEDIAMTX_RTCP_PORT` | RTCP UDP port | `8001` |
| `STREAM_API_PORT` | Stream control UI/API port | `8080` |
| `LOG_LEVEL` | Supervisor log level; `debug` also shows ffmpeg output | `info` |

### Settings

The Settings panel in the UI sidebar edits these; `GET` and `POST
/api/settings` carry the same five keys. Blank means unset. A save takes effect
at once and is remembered in `state/streams.json`.

| Setting | Meaning | Default |
|---------|---------|---------|
| Destination host | Where the KLV UDP feeds are pushed: the consumer host's address as reachable from the publisher, or a multicast group. Unset holds every udp output in Waiting. Changing it restarts the enabled udp outputs. | unset |
| UDP ports (first, last) | The range handed out to streams, matching the consumer's ingest range. Changing it affects new streams only: existing ones keep their ports, and the panel warns while any of them lie outside the range. | `40000`-`40100` |
| Advertised host | Host in the RTSP/HLS URLs the UI and API show. Blank uses the address the client reached the API by. | blank |
| Bitrate cap | Transcode-only cap such as `2M` or `3000K`; passthrough sends the source bitrate unchanged. Changing it restarts only outputs transcoding above it. Blank disables the cap. | blank |

Values from an older `.env` (`OUTPUT_HOST`, `UDP_BASE_PORT`, `UDP_LAST_PORT`,
`MAX_VIDEO_BITRATE`) are ignored; enter them in the panel once after upgrading.
The UI has no authentication, so anyone who can reach the API port can change
where the feeds go.

## Commands

```bash
make build             # Build the container image
make test              # Build the image, then run the tests inside it
make up                # Start the stack in the mode .env names
make up MODE=host      # Select host networking in .env, then start
make up MODE=octo-cx   # Select the consumer's docker network, then start
# Logs: `docker compose logs -f`. Stop: `docker compose down`. Both in this directory.
```

## Python code structure

The application modules and the UI page live in `src/` and are copied flat into
`/app` in the image, so they import each other by bare module name. The root
holds the container and deployment files plus the host-side diagnostic
`socket_memory.py`.

`stream_supervisor.py` owns all output changes and persistence. HTTP threads
submit operations to a bounded queue. One background thread runs the existing
directory polling/stability checker and submits detached observations to that
same queue. The supervisor executes work in order and publishes detached
status snapshots; HTTP GET composes URLs from the snapshot and never walks
live outputs. There is no lifecycle state machine or
per-output synchronization protocol.

| Module | Responsibility |
|--------|----------------|
| `appconfig.py` | Start-up facts from `.env`: ports and log level |
| `runtimesettings.py` | Operator settings: destination, UDP range, advertised host, bitrate cap; validation in one place |
| `outputregistry.py` | Output lookup, collisions, stable UDP assignments, and removed-source cleanup |
| `mediaoutput.py` | Independent settings, start/stop, finite completion, and recovery |
| `ffmpegcmd.py` | Media probing, encoding decisions, and command syntax |
| `ffmpegprocess.py` | Popen, bounded shutdown, and file-backed diagnostics |
| `statefile.py` | Single-writer persistence and atomic replacement |
| `streamapi.py` | HTTP validation, queued mutations, settings, and status/UI serving |
| `streamresponses.py` | Format detached output descriptions and compose URLs per request |
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

### Tests

The tests use only the Python standard library and run inside the container
image, against the same Python and ffmpeg that ship:

```bash
make test
```

It rebuilds the image, then mounts `tests/` read-only into a throwaway
container; the image itself carries no tests. For a quick run on the host
instead:

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

## Docker Compose

The Dockerfile pins the MediaMTX release and rebuilds its binary with patched
Go and networking dependencies. The release archive is verified by SHA-256;
update its checksum together with `MEDIAMTX_VERSION`. The build also applies
available Alpine package fixes before installing Python. The compiler and
source files stay in the build stage, outside the runtime image.

Plain `docker compose up -d` also works after the image is built; `make up`
only adds the optional mode selection and the `state/` directory.
`mediamtx.yml` is part of the image: edit it, run `make build`, then the mode
target again, and Compose recreates the container on the new image.

### Networking modes

`.env` names the compose files to use in `COMPOSE_FILE`; keep exactly one of
the two lines active. `make up MODE=host` or `MODE=octo-cx` toggles the line
and starts the stack. Nothing is detected: the mode is whatever that line says.

- **Host networking** (`docker-compose.host.yml`): MediaMTX and the API bind
  the host's interfaces on the `.env` ports directly, with no NAT, and the KLV
  feeds leave from the host's own address. For a publisher on its own host.
  The host firewall applies to these listeners; on a host running ufw, allow
  them from the LAN, for example:

  ```bash
  sudo ufw allow from <lan subnet> to any port 8554 proto tcp
  sudo ufw allow from <lan subnet> to any port 8888 proto tcp
  sudo ufw allow from <lan subnet> to any port 8080 proto tcp
  sudo ufw allow from <lan subnet> to any port 8000:8001 proto udp
  ```

- **Consumer network** (`docker-compose.octo-cx.yml`): joins the consumer's
  external `octo-cx-network`, so containers there reach this one as
  `autostream`, and publishes the ports one to one so the RTP/RTCP ports
  MediaMTX advertises are the ones LAN clients reach. For a publisher on the
  same host as the Compose form of the consumer, which must already have
  created the network. Published ports bypass ufw. Pick free numbers in `.env`
  when the consumer stack already uses a port on that host (its relay
  publishes 8554); with one-to-one publishing that is the only change.

In both modes the ports clients use are the `.env` values, so `ffplay
rtsp://<advertised host>:8554/<stream-name>` works over UDP or TCP;
video-streaming itself prefers TCP. The web UI is `http://<publisher
address>:8080`.

### Deploying across hosts

The publisher host runs autostream outside the consumer's Docker network or
Kubernetes cluster; the consumer host runs cx-search `video-streaming`, which
pulls RTSP/HLS or listens for the UDP feeds. Containers on either host resolve
neither `.local` names nor the other host's container names, so use addresses
the LAN's DNS knows or IP addresses, and reserve those addresses on the
router: a changed publisher address breaks every advertised URL and CX
registration, and a changed consumer address sends the KLV feeds nowhere
without any error here.

1. On the publisher: `.env` with the host networking line, `make build`,
   `make up MODE=host`, and the firewall rules above if ufw is active.
2. Open the web UI, expand Settings, and set the destination host to the
   consumer host's address. Leave the UDP range at the consumer's ingest range
   (`40000-40100` in the Compose form). Set the advertised host only if the
   consumer must use a different address than your browser did.
3. KLV path: for each stream, add `udp://0.0.0.0:<port shown in the UI>` as a
   stream in CX-Watch. Video only: add `rtsp://<advertised host>:8554/<name>`
   instead (KLV is lost; MediaMTX drops it).
4. Check from the consumer host: `docker exec video-streaming ffprobe -v error
   -rtsp_transport tcp -show_streams rtsp://<advertised host>:8554/<name>`;
   once a UDP stream is registered, `docker logs video-streaming` shows ffmpeg
   reading that port without an input error.

## Supported Formats

Every file in `videos/` gets a stream except hidden files and READMEs — there
is no format allowlist. FFmpeg must be able to decode the file (MP4, MKV, AVI,
MOV, WEBM, FLV, TS, and so on); a non-video file gets a stream slot whose
ffmpeg process fails and is retried periodically.
