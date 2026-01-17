#!/bin/sh

# Note: Log level is set directly in mediamtx.yml

# Trap SIGTERM and SIGINT to kill all child processes immediately
trap 'kill -TERM 0' TERM INT

# Start MediaMTX in background
/mediamtx /app/mediamtx.yml &

# Start stream supervisor in background
python3 /app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
