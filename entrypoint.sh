#!/bin/sh

# Note: Log level is set directly in mediamtx.yml

# Trap SIGTERM and SIGINT to kill all child processes immediately
trap 'kill -TERM 0' TERM INT

MEDIAMTX_CONFIG=/app/mediamtx.yml

if [ -f /app/mediamtx.yml.template ]; then
  envsubst < /app/mediamtx.yml.template > /tmp/mediamtx.yml
  MEDIAMTX_CONFIG=/tmp/mediamtx.yml
fi

# Start MediaMTX in background
/mediamtx "$MEDIAMTX_CONFIG" &

# Start stream supervisor in background
python3 /app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
