#!/bin/sh

# Note: Log level is set directly in mediamtx.yml

# Trap SIGTERM and SIGINT to kill all child processes immediately
trap 'kill -TERM 0' TERM INT

# The config is always rendered from the mounted template — there is deliberately
# no baked-in fallback. Booting MediaMTX on some other config would hide a missing
# mount behind a server that looks healthy but ignores this repo's ports and paths.
MEDIAMTX_TEMPLATE=/app/mediamtx.yml.template
MEDIAMTX_CONFIG=/tmp/mediamtx.yml

if [ ! -f "$MEDIAMTX_TEMPLATE" ]; then
  echo "entrypoint: $MEDIAMTX_TEMPLATE is missing; mount mediamtx.yml there (see docker-compose.yml)" >&2
  exit 1
fi

envsubst < "$MEDIAMTX_TEMPLATE" > "$MEDIAMTX_CONFIG"

# Start MediaMTX in background
/mediamtx "$MEDIAMTX_CONFIG" &

# Start stream supervisor in background
python3 /app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
