# Regression checklist

Manual checks that have caught real defects in the control UI and supervisor.
Run against a deployed stack (`make build && make compose-up`) with the four
sample videos in `videos/` (two H.264 MP4s, one MJPEG AVI, one MPEG-TS with
KLV). Ground truth is always the process table and the API:

```bash
docker exec autostream ps -o pid,etimes,args | grep ffmpeg
curl -s localhost:9080/api/streams | python3 -m json.tool
docker logs --since 5m autostream
cat state/streams.json
```

Give the UI one poll (5 s) after any change before judging it; the server
notices a dead process within one 2 s tick.

## Boot and persistence

- Fresh boot: every video gets two ffmpeg processes (rtsp, udp); the log
  shows the encoder tier once and one "Started" line per output.
- `state/streams.json` exists, owned by UID 1000, one UDP port per stream.
- Stop one output, `docker restart autostream`: that output stays stopped,
  the other returns, the stream keeps the same UDP port.
- Change mode/FPS/GOP/loop, restart the container: the settings survive and
  the relaunched command reflects them.
- Delete a video, copy it back under the same name: it gets its previous
  settings and port back.
- A damaged or missing state file boots with defaults and a warning, never a
  crash.

## Files

- Add a video while the UI is open: it appears within ~10 s and streams.
- Delete the selected stream's file: the selection moves, no orphan ffmpeg.
- Replace a file in place: both outputs restart.
- Names: spaces, unicode, apostrophes, 200 characters; a name that sanitizes
  to an existing stream name (`Cargoship.MP4`) is refused with a warning and
  the original slot is untouched; a name that sanitizes to nothing
  (`!!!.mp4`) streams under a timestamp name and leaves no empty entry in the
  state file.
- Non-video content (`.mp4` holding text, a zero-byte file, a `.part` copy in
  progress) shows Failed with "no video track found" and is not launched;
  when the file becomes a real video it starts on its own.

## Settings and commands

- Passthrough on an H.264 file: command has `-c:v copy`, FPS and GOP fields
  disabled. Transcode: `-g N -keyint_min N`, fields enabled.
- Passthrough requested on the MJPEG file: it transcodes, the note names the
  codec, FPS and GOP stay enabled.
- FPS defaults to the source rate; GOP defaults to one second at the output
  rate (24 fps file → 24; FPS set to 15 → GOP 15). `ffprobe` on the RTSP
  output reports the chosen rate.
- FPS above 120 or below 1, GOP below 1, loop below -1: rejected with a
  clear message; the draft is kept so it can be corrected.
- The Command view matches the real argv (`ps`) in every state: running,
  stopped, after a settings change, after a crash relaunch, after restart.
- Typing in FPS/GOP: keystrokes land in order, the caret stays put, Save
  and Cancel appear; blank means automatic. Cancel restores the values from
  before editing. A poll during editing keeps the draft and the focus.
- Save on a running stream restarts it once with all changed values; on a
  stopped stream it only saves them and the command view updates.
- Playback set to a value outside the presets (`loop=3` via API) shows as
  "Play 4x", not "Loop forever".

## Start, stop, restart semantics

- Stop one output: the other keeps running.
- Stream-level start (`POST /api/streams/<name>/start`; the UI has only
  per-output buttons) on a fully stopped stream brings both outputs up; on a
  running stream it, like a settings change, relaunches only the outputs
  that are up (a deliberately stopped one stays down).
- Start All only launches outputs that are down (running PIDs unchanged);
  Stop All stops everything. Both buttons re-enable after the refresh.
- A clicked button shows "…" and is disabled until the refresh; rapid
  double clicks fire once.
- 20 parallel mixed start/stop/settings requests on one stream: no more
  than two ffmpeg processes per stream, no zombies, UI recovers.

## Failure and recovery

- `kill -9` one ffmpeg: within ~4 s the API and UI show Crashing with
  "ffmpeg exited with code -9" plus the last log lines; the sidebar dot is
  red; the other output is unaffected; after 60 s stable it reads Running.
- Kill both outputs, kill several streams at once, kill repeatedly: same
  behaviour, counts increase, no orphaned processes.
- A finite loop (Play once) ends in Completed and is not relaunched.
- `docker pause` for 20 s: the sidebar shows "updates stalled Ns"; after
  unpause it clears and the state matches.
- `docker restart` with the UI open: it recovers without a reload.
- `OUTPUT_HOST` unresolvable: udp shows Waiting with the reason, rtsp is
  unaffected; once resolvable the udp output starts without touching rtsp.
- UDP port range exhausted: a new stream takes the port of the
  longest-removed file; with no removed files it shows Failed with
  "no free UDP port".

## UI

- Sidebar dots and pills agree with the API for every state (running,
  crashing, restarting, starting, waiting, failed, stopped, completed).
- Keyboard: Tab reaches stream rows and controls, Enter selects, focus
  survives the list re-render, a visible ring appears only for keyboard focus.
- 700 px window: no horizontal scroll, list becomes a top strip, no empty
  band under it.
- Error banner clears on its own: a "Failed to fetch" goes away on the next
  successful poll, a refused Save on the next successful command or on
  Cancel; a poll failure never blanks the pane.
- FPS/GOP record their typed value when the field loses focus (Tab or click
  away), not only on Enter.
- Copy buttons work on the plain-HTTP origin and say "Copied".
- No console errors other than expected 400s; no favicon 404.

## Host playback

- `ffplay -rtsp_transport tcp rtsp://localhost:9554/<stream>` plays; the
  HLS URL on port 9322 plays in VLC or a browser.
