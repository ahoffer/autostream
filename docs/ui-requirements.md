# Stream Control UI requirements

The web page served at `/` is the only way an operator creates, tunes, starts
and stops streams. This document says what that page must let the operator
see and do. It says nothing about how the page looks; that is the designer's
work.

## Who uses it and why

One operator, a developer, is debugging a video consumer (CX-Edge) on the
same LAN. Autostream is the test rig that feeds it. The operator's loop is:
create a stream of a known file over a chosen transport, copy the playback
URL into the consumer, watch whether the stream runs, change an encoding
choice, watch again. Many transports get tried in one sitting. Nothing here
is public, multi-user, or high scale; up to about twenty streams exist at
once.

The page stays open for hours beside other tools, so it is often in a
window 600 to 900 px wide, sometimes full width, sometimes on a phone to
glance at state.

## Vocabulary

Use these words on the page. They match the API, the README and the state
file.

- **Stream**: one video file bound to one transport, with its own encoding
  choices and, when the transport takes one, its own port. Named by the
  operator, who can rename it after it exists.
- **Transport**: how the stream is delivered. Ten kinds exist, each with a
  direction and a rule about ports; see the table below.
- **Direction**: *push* (we send to the consumer), *listen* (the consumer
  connects to us), or *serve* (MediaMTX or HLS files serve it).
- **Playback URL**: the URL the operator pastes into the consumer to play
  the stream. Every stream has at least one; the RTSP kind has three. The
  API calls this the register field.
- **Delivery**: where ffmpeg sends, listens, publishes or writes. Shown for
  troubleshooting; the playback URL is what the operator needs.
- **Mode**: *passthrough* copies the source codecs; *transcode* re-encodes
  video with the chosen frame rate and GOP. Passthrough is only eligible
  for some source codecs.
- **Frame rate** and **GOP**: output choices for transcode; each is *auto*
  (source rate; one second of frames) or an explicit value.
- **Loop**: forever, once, or a count.
- **Settings**: topology and policy shared by all streams: destination
  host, multicast group, advertised host, bitrate cap, SRTP key, and a
  first port per transport kind.
- **KLV**: timed metadata carried in MPEG-TS; every kind except the MediaMTX
  one carries it.

## Transport kinds

| id | Label | Direction | KLV | Port | Needs a setting |
|---|---|---|---|---|---|
| rtsp | RTSP / HLS / RTMP via MediaMTX | serve | no | none | |
| udp | UDP | push | yes | from UDP first port | destination host |
| multicast | Multicast | push | yes | random, 50000 to 59999 | multicast group |
| rtp | RTP / TS | push | yes | from RTP first port | destination host |
| srt_listen | SRT listen | listen | yes | from SRT listen first port | |
| srt_call | SRT call | push | yes | from SRT call first port | destination host |
| tcp_listen | TCP listen | listen | yes | from TCP first port | |
| http | HTTP TS | listen | yes | from HTTP first port | |
| hls | HLS (TS segments) | serve | yes | none | |
| srtp | SRTP | push | yes | from SRTP first port | destination host, SRTP key |

A new stream takes the lowest free port at or above its kind's first port.
The port is fixed for the stream's life. Changing a first port affects new
streams only.

## Stream states

The server reports one of these states with a short reason. The page shows
both.

| State | Meaning | Typical reason text |
|---|---|---|
| running | ffmpeg is up and delivering | none, or the last log lines if it has been noisy |
| starting | enabled, process launching | starting |
| stopping | Stop pressed, waiting for exit | waiting for process exit |
| stopped | not enabled | stopped |
| completed | played once to the end | played to the end |
| waiting | enabled but blocked by a missing setting or port | no destination host configured; no free port |
| restarting | process died, relaunch pending | died 3x; relaunching |
| crashing | process died and was relaunched, under watch | died 3x; relaunched, watching |
| failed | gave up; error text available | the ffmpeg error |

A push stream whose consumer is not listening yet shows as crashing or
restarting with an I/O error. That is normal while testing and should read
as a state to act on, not a catastrophe.

## What the operator must be able to see

1. **Every stream** without paging: name, transport, file, port, and state
   with its reason. Which streams are running must be obvious at a glance.
   The count of streams and of running streams.
2. **The full detail of any one stream**: everything above plus the mode
   in force and the encoder, and, only when the chosen mode could not be
   honoured (passthrough asked for, codec not eligible), the fact that
   transcode was used instead; the source facts (video codecs, audio
   codecs, KLV present or not, source frame rate); the output frame rate
   and GOP and whether each is auto or explicit; whether the output carries
   the source's KLV (passed through, dropped by the transport, or none in
   the source); the loop choice; the
   playback URL or URLs; the delivery URL; the ffmpeg command; and the
   error text when there is any.
3. **The playback URL**, copyable in one action, with feedback that the
   copy happened. Where a kind has several (RTSP, HLS, RTMP) each is
   labelled.
4. **The ffmpeg command** on demand rather than always.
5. **The error text** (last ffmpeg log lines) when present, together with
   the state that caused it.
6. **Settings** with their current values and what each is for.
7. **Freshness**: the server is polled every 5 seconds. When updates have
   stalled for more than about 12 seconds the page says so and for how
   long. When the server is unreachable the page keeps the last known
   state and says it is stale; when the server is reachable again the
   notice clears on its own.
8. **The result of an action**: a failed start, stop, delete, create or save
   shows the server's message near the thing that was acted on. The server
   returns plain text such as "Choose a video file", "GOP length must be a
   whole number of frames, 1 or more, or auto", "Stream not found", or
   "Operation could not finish; check the stream status".

## What the operator must be able to do

1. **Reach the full detail** of any stream.
2. **Start**, **Stop** and **Delete** a stream. Start is unavailable while
   running; Stop is unavailable while stopped. Delete is irreversible and
   requires a second confirming action.
3. **Start all** and **Stop all**.
4. **Create** a stream: choose a video file from the list of stable files,
   choose a transport, give it a name (a default is derived from the file
   stem and the transport), set mode, frame rate, GOP and loop, then
   either create it started or create it stopped. Before creating, the
   operator can see which port the stream would take (a predicted number,
   random, or none) and whether the transport needs a setting that is
   still unset.
5. **Edit** a stream: change the name, file, transport, mode, frame rate,
   GOP or loop. Edits are a draft until saved or cancelled; saving a running
   stream restarts it and the page says so before the operator commits.
   The draft survives the 5 second refresh. Changing the transport shows
   the port the stream would take.
6. **Advanced choices**: mode, frame rate, GOP and loop are advanced. They
   are hidden until the operator asks to see them, both when creating and
   when editing. The usual case is file, transport and name only.
7. **Mode rules**: passthrough is offered only when the source is eligible;
   when it is not, the page says which codecs prevent it. While
   passthrough is chosen, frame rate and GOP are not editable and the
   source rate is shown instead. While transcode is chosen, frame rate
   (1 to 120, or auto) and GOP (whole frames, 1 or more, or auto) are
   editable.
8. **Edit settings**: all twelve fields, with save and cancel. Blank host
   fields mean unset; a blank port means its default. Validation messages
   come from the server as plain text and are shown.
9. **Copy** any playback URL or the delivery URL.

## Constraints

- One static HTML file with inline CSS and JavaScript, served by the app as
  it is today. No build step, no framework, no bundler.
- Usable when the LAN has no internet access.
- Usable in windows from 400 px to 1600 px wide.
- Meets WCAG AA and is fully operable by keyboard.
- The page contains only what this document names. No additional copy,
  hints, badges or notes.
- Re-rendering on each poll must not cause flicker or lose focus from a
  field the operator is typing in.

## Data for mockups

Real responses from the running instance are saved as fixtures beside the
designs. A mockup must use them so that names, URLs and states are the real
ones.

- `sample_streams.json`: nine streams, three running, across seven kinds,
  exactly as the API returned them.
- `sample_streams_mixed.json`: those nine plus three built from them so
  every state is represented: `cargoship-srt_call` crashing with its I/O
  error, `truck-srtp` waiting on the SRTP key, `boats-tcp` failed on a port
  in use, `cargoship-hls` completed. Mockups render from this file.
- `sample_transports.json`: the kinds table as the API serves it.
- `sample_settings.json`: the settings in force on tang.
- `sample_videos.json`: the three stable files.

A mockup must show, with that data, at least: every stream; a running push
stream in full detail with its playback URL; the same stream with an
unsaved edit; the crashing stream with its error text; the waiting stream;
the create form with a predicted port; the settings; the stale notice; and
the page at 400 px and at 1440 px wide.
