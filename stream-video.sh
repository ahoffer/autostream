#!/bin/sh
# Wrapper script to stream a video file to MediaMTX via RTSP
# Usage: stream-video.sh <video-file> <stream-path>

VIDEO_FILE="$1"
STREAM_PATH="$2"

# Default bitrate if not set (controls network bandwidth usage)
BITRATE="${STREAM_BITRATE:-3M}"

exec ffmpeg -re -stream_loop -1 \
  -i "$VIDEO_FILE" \
  -c:v libx264 -profile:v baseline -preset ultrafast \
  -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "6M" \
  -map 0 \
  -f rtsp "rtsp://localhost:8554/$STREAM_PATH"
