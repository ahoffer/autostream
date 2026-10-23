# Regression checklist

Manual checks that have caught real defects in the control UI and supervisor.
Run against a deployed stack (`make build && make up MODE=host`) with the
sample videos in `videos/` (H.264 MP4s, an MJPEG AVI, an MPEG-TS with KLV).
Ground truth is always the process table and the API:

```bash
docker exec autostream ps -o pid,etimes,args | grep ffmpeg
curl -s localhost:8080/api/streams | python3 -m json.tool
docker logs --since 5m autostream
cat state/streams.json
```

Give the UI one poll (5 s) after any change before judging it; the server
notices a dead process within one 2 s tick.

## Create, delete and persistence

- Fresh boot with no state: no ffmpeg processes, the list says there are no
  streams yet, the File dropdown in New stream lists every stable file.
- Create a stream per transport for the KLV file: each gets one ffmpeg
  process, its own port from its kind's first port (multicast: a random port
  from 50000-59999), and the Playback URL matches the Transports table in
  the README.
- Create with a taken name, an empty name, or a name with spaces: refused with
  the reason, nothing created.
- Create stopped: the ffmpeg command tab shows a preview and there is no
  process; Start brings it up.
- Stop one stream, `docker restart autostream`: it stays stopped, the others
  return, every stream keeps its port.
- Change mode, frame rate, GOP or loop under Advanced, restart the container:
  the choices survive and the relaunched command reflects them.
- Rename a running UDP stream: the row keeps its place and port, no ffmpeg
  restart (`ps` etimes unchanged), the state file key moves. Rename an RTSP
  stream: one restart with the new path. Rename to a taken name: refused with
  the reason, the draft kept.
- Delete: the first press turns it into Confirm delete, the second removes the
  stream and its process; the port is reused by the next stream of that kind.
- A state file from an earlier release stops the supervisor with a message
  naming the file; deleting it boots clean.

## Files

- Add a video while the UI is open: it appears in the dropdowns within ~10 s.
- Delete the file of a running stream: the stream shows Waiting "source file
  missing" with no orphan ffmpeg; copy the file back and it restarts.
- Replace a file in place: every stream playing it restarts; others do not.
- Non-video content (`.mp4` holding text, a zero-byte file, a `.part` copy in
  progress): a stream over it shows Failed with "no video track found" and is
  not launched; when the file becomes a real video it starts on its own.

## Settings and commands

- Passthrough on an H.264 file: command has `-c:v copy`, the Frame rate and
  GOP fields under Advanced give way to the source rate. Transcode:
  `-g N -keyint_min N`, fields editable.
- Passthrough requested on a file whose codec is not eligible: it transcodes,
  the Output tab names the codec and says transcode was used instead, Frame
  rate and GOP stay editable.
- FPS defaults to the source rate; GOP defaults to one second at the output
  rate (24 fps file → 24; FPS set to 15 → GOP 15). `ffprobe` on the RTSP
  stream reports the chosen rate.
- FPS above 120 or below 1, GOP below 1, loop below -1: rejected with a
  clear message; the draft is kept so it can be corrected.
- Changing the Transport dropdown and saving: one restart, a port of the new
  kind, the old port freed. Changing the Video file: one restart, same port.
- The ffmpeg command tab matches the real argv (`ps`) in every state:
  running, stopped, after a settings change, after a crash relaunch, after
  restart.
- Typing in Frame rate/GOP under Advanced: keystrokes land in order, the
  caret stays put, Save and Cancel enable and Unsaved appears whichever tab
  is showing; blank means auto. Cancel restores the values from before
  editing. A poll during editing keeps the draft and the focus.
- Loop set via the API (`loop=3`) shows as count with Loop count 3, not
  forever.
- Settings: a destination host change restarts pushed streams only; a first
  port change moves no existing stream; a bitrate cap change restarts only
  streams transcoding above it; an invalid multicast group is refused.

## Start, stop, restart semantics

- Stop one stream: the others keep running.
- Start All only launches streams that are down (running PIDs unchanged);
  Stop All stops everything. Both buttons re-enable after the refresh.
- A clicked button is disabled until the refresh; rapid double clicks fire
  once.
- 20 parallel mixed start/stop/settings requests on one stream: never more
  than one live ffmpeg per stream, no zombies, UI recovers.

## Failure and recovery

- `kill -9` one ffmpeg: within ~4 s the API and UI show Crashing with
  "ffmpeg exited with code -9" plus the last log lines; the list mark is a
  hollow red square; other streams are unaffected; after 60 s stable it
  reads Running.
- Kill several streams at once, kill repeatedly: same behaviour, counts
  increase, no orphaned processes.
- A finite loop (Play once) ends in Completed and is not relaunched.
- `docker pause` for 20 s: the stale band says no update for N s; after
  unpause it clears and the state matches.
- `docker restart` with the UI open: it recovers without a reload.
- Destination host unset or unresolvable: pushed streams show Waiting with
  the reason, pulled streams are unaffected; once set and resolvable the
  pushed streams start without touching the others.
- SRT call with nobody listening on the consumer: Restarting with the ffmpeg
  "Connection refused" error until the consumer registers the listener.

## Transports from the consumer

From the consumer host, inside its `video-streaming` container (`docker exec
video-streaming ffprobe -v error -show_streams <url>`): RTSP over TCP and
UDP, RTMP, HLS from MediaMTX, HLS (TS segments) from port 8080, SRT listen,
TCP listen and HTTP TS all open and list the tracks; the transport stream
kinds list `klv` for the KLV file, the MediaMTX kinds do not. UDP and RTP
pushes are heard on `udp://0.0.0.0:<port>` and `rtp://0.0.0.0:<port>`.

## UI

- List marks and state words agree with the API for every state (running,
  crashing, restarting, starting, waiting, failed, stopped, completed).
- Keyboard: Tab reaches stream rows and controls, Enter selects and moves
  focus to the detail heading, arrow keys move between notebook tabs, focus
  survives the list re-render, a visible ring appears only for keyboard focus.
- 400, 800 and 1280 px windows: no horizontal scroll; below 1280 the detail
  stacks under the list; below 720 each row folds to two lines; the
  Transport dropdown never truncates its longest option.
- Messages clear on their own: the stale band goes away on the next
  successful poll, a refused Save on the next successful save or on Cancel;
  a poll failure never blanks the page.
- Frame rate/GOP record their typed value when the field loses focus (Tab or
  click away), not only on Enter.
- Copy buttons work on the plain-HTTP origin and say "Copied".
- No console errors other than expected 400s; no favicon 404.

## Host playback

- `ffplay rtsp://localhost:8554/<stream>` plays over UDP or TCP; the HLS URL
  on port 8888 plays in VLC or a browser.
