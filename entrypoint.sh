#!/bin/sh

# Trap SIGTERM and SIGINT to kill all child processes immediately
trap 'kill -TERM 0' TERM INT

# The config is bind-mounted by compose. Fail loudly if the mount is missing:
# otherwise mediamtx dies, the supervisor waits on its port forever, and the
# container sits half-dead while looking healthy.
MEDIAMTX_CONFIG=/app/mediamtx.yml

if [ ! -f "$MEDIAMTX_CONFIG" ]; then
  echo "entrypoint: $MEDIAMTX_CONFIG is missing; mount mediamtx.yml there (see docker-compose.yml)" >&2
  exit 1
fi

# Start MediaMTX in background
/mediamtx "$MEDIAMTX_CONFIG" &

# Start stream supervisor in background
python3 /app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
