#!/bin/sh

# Start MediaMTX in background
/mediamtx /mediamtx.yml &

# Start stream supervisor in background
/app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
