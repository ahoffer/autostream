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

# .env owns the listener ports (compose passes them through). Hand them to
# MediaMTX via its MTX_* config overrides so mediamtx.yml never carries port
# numbers that can drift out of sync.
export MTX_RTSPADDRESS=":${MEDIAMTX_RTSP_PORT:?MEDIAMTX_RTSP_PORT is not set}"
export MTX_RTPADDRESS=":${MEDIAMTX_RTP_PORT:?MEDIAMTX_RTP_PORT is not set}"
export MTX_RTCPADDRESS=":${MEDIAMTX_RTCP_PORT:?MEDIAMTX_RTCP_PORT is not set}"
export MTX_HLSADDRESS=":${MEDIAMTX_HLS_PORT:?MEDIAMTX_HLS_PORT is not set}"

# Start MediaMTX in background
/mediamtx "$MEDIAMTX_CONFIG" &

# Start stream supervisor in background
python3 /app/stream-supervisor.py &

# Wait for all background processes to terminate
wait
