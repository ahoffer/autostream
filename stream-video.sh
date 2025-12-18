#!/bin/sh
# Wrapper script to stream a video file to MediaMTX via RTSP
# Usage: stream-video.sh <video-file> <stream-path> [loop-count]
#   loop-count: -1 for infinite (default), 0 for no loop, N for N additional plays
#
# LOOPING DISCONTINUITY FIX:
# The source videos have negative DTS (-0.067s) and B-frames. When using
# -stream_loop -1, each loop restart causes:
#   - Timestamp jump backward: 12.0s -> 0.0s (violates monotonic time)
#   - Negative DTS reappears every loop cycle
#   - B-frame reference dependencies break at loop boundary
#   - Large GOP (8.3s) causes artifacts to persist
#
# This creates visible artifacts (pixelation, tearing).
# Solution: Transcode with clean GOP structure and regenerated timestamps.
# Artifacts should clear up after the first loop.

VIDEO_FILE="$1"
STREAM_PATH="$2"
LOOP_COUNT="${3:--1}"

exec ffmpeg -re -stream_loop "$LOOP_COUNT" -i "$VIDEO_FILE" \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -g 30 -keyint_min 30 -sc_threshold 0 \
  -bf 0 \
  -x264-params ref=1 \
  -c:a aac -b:a 128k \
  -fflags +genpts+igndts \
  -avoid_negative_ts make_zero \
  -vsync cfr \
  -max_muxing_queue_size 1024 \
  -f rtsp "rtsp://localhost:8554/$STREAM_PATH"

# FLAG EXPLANATIONS
# FFMPEG is complex. Some flags might be redundant.
#
# -re                            Read input at native frame rate (real-time streaming)
# -stream_loop -1                Loop video infinitely
# -i "$VIDEO_FILE"               Input video file
#
# VIDEO ENCODING (fixes GOP and B-frame issues):
# -c:v libx264                   Encode to H.264 (re-encode to fix structure)
# -preset ultrafast              Fastest encoding preset (low CPU usage)
# -tune zerolatency              Optimize for low-latency streaming
# -g 30                          GOP size: 30 frames (1s @ 30fps) for quick recovery
# -keyint_min 30                 Minimum keyframe interval: 30 frames
# -sc_threshold 0                Disable scene detection (prevents unexpected keyframes)
# -bf 0                          Disable B-frames (eliminates reference frame issues at loop)
# -x264-params ref=1             Use only 1 reference frame (reduces loop boundary complexity)
#
# AUDIO ENCODING:
# -c:a aac                       Encode to AAC
# -b:a 128k                      Audio bitrate: 128 kbps
#
# TIMESTAMP FIXES (eliminates negative DTS and discontinuities):
# -fflags +genpts                Regenerate presentation timestamps (fixes loop discontinuities)
# -fflags +igndts                Ignore input DTS (eliminates negative -0.067s DTS)
# -avoid_negative_ts make_zero   Shift all timestamps to start at 0 (prevents negative values)
# -vsync cfr                     Constant frame rate (ensures even frame spacing at loop point)
#
# STREAM RELIABILITY:
# -max_muxing_queue_size 1024    Prevent buffer overflows during encoding
#
# OUTPUT:
# -f rtsp                        Output format: RTSP
# rtsp://localhost:8554/...      Stream to MediaMTX server
