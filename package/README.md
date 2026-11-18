# Autostream

Autostream plays video files as live streams for cxedge to ingest. It runs
on the same host as cxedge and joins cxedge's Docker network, so cxedge
reaches every stream by the name `autostream`.

## Before you start

- Docker with Compose (`docker compose version` prints a version).
- cxedge installed with Docker Compose and running. Its stack creates the
  `octo-cx-network` network that autostream joins.
- `GITLAB_OLABS_HOST`, `GITLAB_OLABS_USER`, and `GITLAB_OLABS_TOKEN` set in
  your environment, from the person who gave you this folder.

## Start

Run these commands in this folder:

```bash
printf '%s' "$GITLAB_OLABS_TOKEN" | docker login "$GITLAB_OLABS_HOST" -u "$GITLAB_OLABS_USER" --password-stdin   # Only needed once
docker compose up -d
```

The first start downloads the image. Open the play page at
`http://<this host>:9080`.

## Play a video

The play page lists every file in `videos/`. On a first start every video is
already playing.

1. Press **Play once** or **Play forever** beside a video. Play once ends at
   the end of the file. Play forever starts over at the end. Pressing either
   on a playing video restarts it.
2. Press **Copy** beside its address, such as
   `http://autostream:43000`.
3. Register that address in cxedge.

**Stop** ends a stream. To add a video, copy it into `videos/`. It appears on
the page within a few seconds. Most formats work, such as MP4, MKV, AVI, MOV
and MPEG-TS.

## Stop

```bash
docker compose down
```

Stream choices are kept for the next start. To start over, run
`docker compose down -v`, which also deletes them.

## If something goes wrong

- If the start says `network octo-cx-network declared as external, but could
  not be found`, start cxedge first.
- If it says `port is already allocated`, another program uses port 9080 or
  9180. Change `PLAY_PORT` or `STREAM_API_PORT` in `.env` and start again.
- If the download says `unauthorized` or `denied`, run the `docker login`
  command again with your credentials.
- `docker compose logs -f` shows the logs.
