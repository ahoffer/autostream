# Autostream

A test-rig video server with web-based stream control. A video can be served
over RTSP, HLS and RTMP through MediaMTX, or as an MPEG-TS feed over UDP,
multicast, RTP, SRT, TCP, HTTP, HLS segments or SRTP.

## Quickstart

1. **Check `.env`** and edit it for your environment if needed.

2. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

3. **Build and deploy** via Docker Compose:
   ```bash
   make build       # Only needed once, or after code changes
   make up          # Uses COMPOSE_FILE in .env; see "Networking modes"
   ```

4. **Point the feeds at their consumer**: open `http://<this host>:9080`,
   press Settings, and enter the destination host.

5. **Find your videos streaming**: each stream's detail shows the playback
   URLs to register in the consumer; Copy puts one on the clipboard.

6. **Create other streams**: press New stream, choose the file and the
   transport, name it, and press Create and start.

## How It Works

- The **file watcher** keeps the list of stable files in `videos/` fresh. A
  file is offered once its size and time stop changing (two-second poll,
  four-second stability check), so a copy in progress never starts a stream.
  Every stable file is offered except hidden files and READMEs; there is no
  format allowlist, FFmpeg must be able to decode the file (MP4, MKV, AVI,
  MOV, WEBM, FLV, TS, and so on).
- A **stream** is a video file bound to one transport, with its own encoding
  choices, Start and Stop, and a port of its own when the transport takes one.
  Streams are created in the UI. A first start, one with no state file yet,
  seeds a running MediaMTX stream for every video already present, named
  after the file; seeding ends as soon as any stream exists, seeded or made by
  hand.
- Each stream is **one ffmpeg process**. MediaMTX serves the RTSP kind as
  RTSP, HLS and RTMP; every other kind is delivered by ffmpeg itself.

## Transports

| Transport | Direction | KLV | The consumer registers |
|-----------|-----------|-----|------------------------|
| RTSP / HLS / RTMP via MediaMTX | consumer pulls | no | `rtsp://<host>:8554/<name>`, `http://<host>:8888/<name>/index.m3u8`, `rtmp://<host>:1935/<name>` |
| UDP | we push | yes | `udp://0.0.0.0:<port>` |
| Multicast | we push | yes | `udp://<group>:<port>` |
| RTP / TS | we push | yes | `rtp://0.0.0.0:<port>` |
| SRT listen | consumer pulls | yes | `srt://<host>:<port>` |
| SRT call | we push | yes | `srt://0.0.0.0:<port>?mode=listener` |
| TCP listen | consumer pulls | yes | `tcp://<host>:<port>` |
| HTTP TS | consumer pulls | yes | `http://<host>:<port>` |
| HLS (TS segments) | consumer pulls | yes | `http://<host>:9080/hls/<name>/index.m3u8` |
| SRTP | we push | yes | `srtp://0.0.0.0:<port>` plus the key |

`<host>` is the advertised host (see Settings). Multicast takes a random port
from 50000-59999 at creation on purpose: the consumer's redirect service
joins the group on its host network and forwards into the container on a port
of its own choosing, so any port works there, and a random one keeps clear of
the unicast range. Listening and served kinds need nothing on the consumer
side. A listening kind serves one client at a time: when that client
disconnects, ffmpeg ends and the stream listens again at once, which the UI
shows as a brief Starting. A push kind whose consumer must listen first, such
as SRT call, shows the crash and relaunch states with ffmpeg's error until the
stream is registered there; UDP, RTP and SRTP pushes send regardless and are
simply not heard until then.

MediaMTX's track model only carries video and audio, so KLV timed-metadata
streams are dropped there. Every transport stream kind carries the whole
MPEG-TS, KLV included, which is the standard MISB/STANAG-4609 way to deliver
it.

## Passthrough and transcode

Each stream has its own encoding choices, chosen in the UI. Passthrough, the
default, sends the source video unmodified. Transcode re-encodes to H.264
with a clean GOP (fixes loop-point artifacts) and the bitrate cap applied.
Two encoder choices only apply when transcoding (under passthrough the source
rate is shown instead): the frame rate (1 to 120, the UI/API limit), which
defaults to the file's own rate; and the GOP, the keyframe interval in
frames, which defaults to one second at the output rate (25 for 25 fps). A
shorter GOP means faster player join and loop recovery at some bitrate cost.

Save (Save and restart while the stream runs) applies every pending change
to mode, frame rate, GOP, loop, name, file and transport in one restart; a
stopped stream uses the saved choices on the next Start. A new transport
takes a new port; a new file keeps the port; a new name keeps the port and
restarts only the kinds whose command names the stream (MediaMTX and HLS).

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
decision. FFmpeg 8.1.2 still reproduced the leak. Software transcoding uses
more CPU.

Compose limits the entire container to 6 GiB of RAM with no swap. This
contains any future runaway allocations.

## Streams

A stream is in one of these states: **Running**; **Crashing** (the
process died unexpectedly and was relaunched; the last lines of its ffmpeg
log are shown, and the state clears once it has stayed up for a minute);
**Restarting** (died, relaunch pending on the next sweep); **Starting**;
**Waiting** (the source file is missing, the transport lacks a setting such
as the destination host, or the destination name is unresolved; a DNS answer
does not prove receiver health);
**Failed** (could not be started, with the reason: the file has no video
track, which is what a non-video file or a copy still in progress looks like;
it is retried when the file changes); **Stopped**;
**Completed** (a finite loop played to the end); **Stopping** (a disabled
process has not exited).

Stopping a stream leaves the others running, and the choice is remembered:
a stopped stream stays down through dead-process sweeps, settings changes,
and container restarts until it is started again. Start All / Stop All apply
to every stream; Start All preserves already-running processes and counts a
waiting stream as started. Delete asks once: the first press turns it into
Confirm delete, the second deletes the stream, stops its process and frees
its port. A stream whose process cannot be stopped is not deleted and keeps
its port; a replacement cannot reuse its destination.

The ffmpeg command tab of a stream's detail shows the exact ffmpeg command
line that stream runs (or would run), for inspection; every flag is explained
in the table at the bottom of `ffmpegcmd.py`. If the old process cannot exit,
its command remains visible alongside the requested settings and an error.
The Output tab shows the encoder in use. Each stream's ffmpeg log is at
`/tmp/ffmpeg-logs/<stream>.log` inside the container.

## State

`mounted/streams_config.json` (bind-mounted to `/app/mounted`) remembers every stream:
its video file, transport, port, encoding choices and enabled choice, plus the
values from the Settings panel. Delete the state file while the container is
stopped to reset everything. A state file with no streams left in it is not
reset: deleted streams stay deleted. A file that disappears from `videos/`
parks its streams in Waiting until it returns; nothing is removed on its
behalf.

HLS (TS segments) streams write their playlists under `/app/hls` inside the
container; the directory is not persisted.

Stream names are yours: letters, digits, `-` and `_`, unique, and they double
as the RTSP, HLS and RTMP path.

## Configuration

Two kinds of configuration exist. `.env` holds what the container needs before
it starts: the image, the networking mode, the listener ports, and the log
level. Everything about where feeds go and how streams are advertised is set
at run time in the web UI under **Settings**.

### `.env`

`.env` is checked in with working values. Docker Compose reads `.env` directly and passes the whole file through to the
container. Every variable must be set.

| Variable | Description |
|----------|-------------|
| `CONTAINER_NAME` | Image and container name |
| `VERSION` | Image version tag |
| `COMPOSE_FILE` | Networking mode: the base file plus `docker-compose.host.yml` or `docker-compose.octo-cx.yml` (see "Networking modes") |
| `MEDIAMTX_RTSP_PORT` | RTSP listener port |
| `MEDIAMTX_HLS_PORT` | HLS HTTP port |
| `MEDIAMTX_RTMP_PORT` | RTMP listener port |
| `MEDIAMTX_RTP_PORT` | RTP UDP port |
| `MEDIAMTX_RTCP_PORT` | RTCP UDP port |
| `STREAM_API_PORT` | Stream control UI/API port |
| `LOG_LEVEL` | Supervisor log level; `debug` also shows ffmpeg output |

### Settings

Settings in the UI edits these; `GET` and `POST /api/settings` carry the
same keys. A blank host or bitrate cap means unset; any other blank takes its
default. A save takes effect
at once.

| Setting | Meaning | Default |
|---------|---------|---------|
| Destination host | Where pushed transports send: the consumer host's address as reachable from the publisher. Changing it restarts them. | unset |
| Multicast group | The group the Multicast transport sends to, and what the consumer registers. | 239.255.0.1 |
| Advertised host | Host in the URLs the consumer registers. Blank uses the address the client reached the API by. | blank |
| Bitrate cap | Transcode-only cap such as `2M` or `3000K`. Changing it restarts only streams transcoding above it. Blank disables the cap. | blank |
| SRTP key | Base64 master key the SRTP transport encrypts with; the consumer needs the same key. | `autostream` repeated four times |
| First ports | One per transport kind that takes one. A new stream takes the lowest free port from its kind's first port and keeps it; changing a first port affects later streams only. | UDP 40000, RTP 40020, SRT call 40040, SRTP 40060, SRT listen 41000, TCP 42000, HTTP TS 43000 |

Advertised host has **Use hostname**, **Use LAN IP**, and **Use localhost**
shortcuts. They fill the field; Save applies the choice. Clear the field to
return to using the browser's address. Existing saved values are preserved.

`make up` supplies the Docker daemon's hostname and, for a local Linux daemon,
the IPv4 address of the host's lowest-metric default-route interface. The
values are detected at startup and exposed through `GET /api/host`. A shortcut
is disabled if its address is unavailable. For remote Docker, other host
platforms, or a different preferred interface, supply `AUTOSTREAM_HOSTNAME`
and `AUTOSTREAM_LAN_IP` in the shell environment. These optional variables
also work with `docker compose up -d` directly (or in `.env` for direct Compose).

The UI has no authentication, so anyone who can reach the API port can create
streams and change where the feeds go.

## API

Every mutation is a `POST`; GETs read the last published snapshot.

- `GET /api/streams`: every stream with its state, choices, source tracks, the
  delivery address and the `register` URLs for the requesting host.
- `GET /api/videos`, `GET /api/transports`, `GET /api/settings`.
- `POST /api/streams?name=&file=&transport=&mode=&loop=&fps=&gop=&start=`:
  create (`start=0` creates it stopped).
- `POST /api/streams/<name>/start|stop|delete`.
- `POST /api/streams/<name>/settings?name=&file=&transport=&mode=&loop=&fps=&gop=`:
  any subset; `name` renames the stream; `fps=auto` and `gop=auto` return to
  the automatic values.
- `POST /api/streams/start-all`, `POST /api/streams/stop-all`, `POST /api/settings?...`.
- `GET /hls/<name>/index.m3u8` and segments for HLS (TS segments) streams.

Queue saturation returns 503; an operation that cannot finish returns 409;
storage failures return 500; a refused creation or setting returns 400 with
the reason. Requests are not automatically retried.

## Python code structure

The application modules and the UI page live in `src/` and are copied flat into
`/app` in the image, so they import each other by bare module name. The root
holds the container and deployment files plus `socket_memory.py`, a host-side
diagnostic that captures slab counters and socket queues in every running
container once per minute into `.generated/socket-memory.jsonl`; see its
`--help`.

`stream_supervisor.py` owns all stream changes and persistence. HTTP threads
submit operations to a bounded queue. One background thread runs the
directory polling/stability checker and submits detached observations to that
same queue. The supervisor executes work in order and publishes detached
status snapshots, from which GET composes URLs without walking live streams.

| Module | Responsibility |
|--------|----------------|
| `appconfig.py` | Start-up facts from `.env`: ports and log level |
| `transports.py` | The transport kinds table: direction, KLV, muxer, port setting, delivery and register templates |
| `runtimesettings.py` | Operator settings: destination, multicast group, advertised host, bitrate cap, SRTP key, first ports; validation in one place |
| `streamregistry.py` | Stream creation and deletion, ports, bindings, and the file inventory |
| `mediastream.py` | One stream: choices, start/stop, rebinding, finite completion, and recovery |
| `ffmpegcmd.py` | Media probing, encoding decisions, and command syntax per delivery |
| `ffmpegprocess.py` | Popen, bounded shutdown, and file-backed diagnostics |
| `statefile.py` | Single-writer persistence and atomic replacement |
| `streamapi.py` | HTTP validation, queued mutations, settings, status, UI and HLS serving |
| `streamresponses.py` | Describe detached streams and compose URLs per request |
| `filewatch.py` | Background polling, stability checking, and present/stable inventories |
| `udphost.py` | DNS cache shared by pushed streams |

A Stop arriving during preparation waits for that operation to finish, then
executes. Slow probing, DNS, and bounded process shutdown can delay later
work. Scan errors do not mean files were deleted.

Each process captures stdout/stderr in a temporary file, removed after exit.
Observation forwards bounded chunks to the injected logger and retains a 4 KiB
tail. Large diagnostic bursts may be truncated with a log notice. Per-stream
logs rotate at 2 MB with one backup; the temporary capture file can grow for the
lifetime of a run.

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
creates the `mounted/` directory first. Logs: `docker compose logs -f`; stop:
`docker compose down`, both in this directory.
`mediamtx.yml` is part of the image: edit it, run `make build`, then `make up`
again, and Compose recreates the container on the new image.

### Networking modes

The `COMPOSE_FILE` line in `.env` selects the networking mode:

- **Host networking** (`COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml`): MediaMTX, the API and every
  listening transport bind the host's interfaces on their ports directly, with
  no NAT, and pushed feeds leave from the host's own address. For a publisher
  on its own host. The host firewall applies to these listeners; on a host
  running ufw, allow them from the LAN, for example:

  ```bash
  sudo ufw allow from <lan subnet> to any port 8554 proto tcp
  sudo ufw allow from <lan subnet> to any port 8888 proto tcp
  sudo ufw allow from <lan subnet> to any port 1935 proto tcp
  sudo ufw allow from <lan subnet> to any port 9080 proto tcp
  sudo ufw allow from <lan subnet> to any port 8000:8001 proto udp
  sudo ufw allow from <lan subnet> to any port 41000:41099 proto udp
  sudo ufw allow from <lan subnet> to any port 42000:43099 proto tcp
  ```

- **Consumer network** (`COMPOSE_FILE=docker-compose.yml:docker-compose.octo-cx.yml`): joins the consumer's
  external `octo-cx-network`, so containers there reach this one as
  `autostream`, and publishes the ports one to one so the RTP/RTCP ports
  MediaMTX advertises are the ones LAN clients reach. For a publisher on the
  same host as the Compose form of the consumer, which must already have
  created the network. Published ports bypass ufw. Pick free numbers in `.env`
  when the consumer stack already uses a port on that host (its relay
  publishes 8554); with one-to-one publishing that is the only change. The
  listening transports are published as fixed ranges (41000-41099/udp for SRT,
  42000-42099 for TCP, 43000-43099 for HTTP TS), so their first ports must
  stay inside those ranges in this mode.

In both modes the ports clients use are the ones shown on each card, so
`ffplay rtsp://<advertised host>:8554/<name>` works over UDP or TCP;
video-streaming itself prefers TCP.

### Deploying across hosts

The publisher host runs autostream outside the consumer's Docker network or
Kubernetes cluster; the consumer host runs cx-search `video-streaming`, which
hands any registered URL to ffmpeg: it pulls RTSP, HLS, RTMP, SRT, TCP and
HTTP, and listens on `udp://0.0.0.0:<port>`, `rtp://`, `srt://...?mode=listener`
and `srtp://`. Containers on either host resolve neither `.local` names nor
the other host's container names, so use addresses the LAN's DNS knows or IP
addresses, and reserve those addresses on the router: a changed publisher
address breaks every advertised URL and CX registration, and a changed
consumer address sends pushed feeds nowhere without any error here.

1. On the publisher: `.env` with the host networking line, `make build`,
   `make up`, and the firewall rules above if ufw is active.
2. Open the web UI, expand Settings, and set the destination host to the
   consumer host's address and, for multicast tests, a group. Set the
   advertised host only if the consumer must use a different address than
   your browser did.
3. Create a stream per transport you want to exercise, then register its
   `register` line in CX-Watch.
4. Check from the consumer host: `docker exec video-streaming ffprobe -v error
   -rtsp_transport tcp -show_streams rtsp://<advertised host>:8554/<name>` for
   a pulled kind; `docker logs video-streaming` shows ffmpeg reading a
   registered pushed feed without an input error.

The Compose form of the consumer forwards UDP 40000-40100 into its container,
so the push first ports must stay inside that range; the Kubernetes chart
exposes no UDP today, so pushed kinds there need a UDP NodePort, LoadBalancer,
or host networking on the consumer side. Its multicast-input allocator draws
from the same range and cannot see ports in use here.
