#!/bin/sh

# Set MediaMTX log level based on LOG_LEVEL environment variable
# Default: error (quiet), Debug: info (verbose)
if [ "$LOG_LEVEL" = "debug" ]; then
    sed -i 's/^logLevel:.*/logLevel: info/' /mediamtx.yml
else
    sed -i 's/^logLevel:.*/logLevel: error/' /mediamtx.yml
fi

# Trap SIGTERM and SIGINT to kill all child processes immediately
trap 'kill -TERM 0' TERM INT

# Start MediaMTX in background
/mediamtx /mediamtx.yml &

# Start stream supervisor in background
/app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
