#!/bin/sh
# Stream a video file to MediaMTX (RTSP + HLS) and, when a UDP target is given,
# also as MPEG-TS over UDP with KLV/data streams preserved.
#
# Usage: stream-video.sh <video-file> <stream-path> [loop-count] [bitrate-cap] [udp-target]
#
# Two outputs are produced from one ffmpeg process:
#   1. RTSP -> MediaMTX (which also republishes it as HLS). This is the human /
#      RTSP-client view. RTP cannot carry KLV, so only video+audio are mapped.
#   2. MPEG-TS over UDP to <udp-target> (host:port), with EVERY stream mapped and
#      data/KLV copied through untouched. This is the metadata-preserving feed.
#
# Video is transcoded to a clean GOP structure to fix looping artifacts and to
# honor the bitrate cap; stream-supervisor.py decides whether a cap applies and
# passes just the value (for example "2M"), empty for none.
#
# Every flag is explained once, in the FLAG EXPLANATIONS table at the bottom.

VIDEO_FILE="$1"
STREAM_PATH="$2"
LOOP_COUNT="${3:--1}"
BITRATE_CAP="$4"
UDP_TARGET="$5"          # host:port for the KLV MPEG-TS/UDP feed; empty = RTSP only
RTSP_PORT="${MEDIAMTX_RTSP_PORT:?MEDIAMTX_RTSP_PORT is not set}"

# Shared encode settings. Deliberately space-split.
VIDEO_OPTS="-c:v libx264 -preset ultrafast -tune zerolatency -g 30 -keyint_min 30 -sc_threshold 0 -bf 0 -x264-params ref=1"
AUDIO_OPTS="-c:a aac -b:a 128k"
TS_FIX="-fflags +genpts+igndts -avoid_negative_ts make_zero -max_muxing_queue_size 1024"

BITRATE_OPTS=""
if [ -n "$BITRATE_CAP" ]; then
  BITRATE_OPTS="-b:v $BITRATE_CAP -maxrate $BITRATE_CAP -bufsize $BITRATE_CAP"
fi

# Output 1: RTSP -> MediaMTX, video+audio only.
set -- -map 0:v? -map 0:a? $VIDEO_OPTS $BITRATE_OPTS $AUDIO_OPTS $TS_FIX -vsync cfr \
       -f rtsp "rtsp://localhost:${RTSP_PORT}/$STREAM_PATH"

# Output 2 (optional): MPEG-TS/UDP with all streams, data/KLV copied verbatim.
if [ -n "$UDP_TARGET" ]; then
  set -- "$@" -map 0 -copy_unknown $VIDEO_OPTS $BITRATE_OPTS $AUDIO_OPTS -c:d copy \
         -max_interleave_delta 1000 -bsf:d "setts=dts=max(DTS\,PREV_OUTDTS)" \
         $TS_FIX -f mpegts "udp://${UDP_TARGET}?pkt_size=1316"
fi

exec ffmpeg -re -stream_loop "$LOOP_COUNT" -i "$VIDEO_FILE" "$@"

# FLAG EXPLANATIONS
#
# -re                            Read input at native frame rate (real-time streaming)
# -stream_loop N                 Loop the input N times; -1 = forever
# -i "$VIDEO_FILE"               Input video file
#
# STREAM SELECTION:
# -map 0:v? -map 0:a?            (RTSP output) Keep every video and audio track,
#                                not just the single "best" of each that ffmpeg
#                                picks by default. The trailing ? makes each
#                                optional so files with no audio (or no video)
#                                still stream. Data/KLV and subtitles are NOT
#                                mapped here: ffmpeg's RTP muxer cannot carry them
#                                and MediaMTX drops them, so mapping them would
#                                make the RTSP header fail and kill the stream.
# -map 0 -copy_unknown           (UDP output) Keep ALL streams, including data
#                                and tracks ffmpeg cannot identify. MPEG-TS over
#                                UDP carries KLV/MISB timed metadata natively.
# -c:d copy                      (UDP output) Copy data streams (KLV) through untouched.
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
# -b:v/-maxrate/-bufsize <cap>   Cap the video bitrate (only when a cap is passed)
#
# AUDIO ENCODING:
# -c:a aac                       Encode to AAC
# -b:a 128k                      Audio bitrate: 128 kbps
#
# TIMESTAMP FIXES (eliminates negative DTS and discontinuities):
# -fflags +genpts                Regenerate presentation timestamps (fixes loop discontinuities)
# -fflags +igndts                Ignore input DTS (eliminates negative -0.067s DTS)
# -avoid_negative_ts make_zero   Shift all timestamps to start at 0 (prevents negative values)
# -vsync cfr                     (RTSP output) Constant frame rate (even frame spacing at loop point)
# -max_interleave_delta 1000     (UDP output) Bound how long the muxer waits to interleave the
#                                sparse KLV data stream against video/audio.
# -bsf:d setts=dts=max(DTS,PREV_OUTDTS)
#                                (UDP output) Force copied data-stream DTS to stay monotonic
#                                after the video transcode retimes the program. In the script
#                                the comma is escaped as \, for ffmpeg's filter parser.
#
# STREAM RELIABILITY:
# -max_muxing_queue_size 1024    Prevent buffer overflows during encoding
#
# OUTPUTS:
# -f rtsp rtsp://localhost:${RTSP_PORT}/<stream-path>   Publish to MediaMTX (serves RTSP + HLS)
# -f mpegts udp://<udp-target>?pkt_size=1316            KLV-preserving MPEG-TS feed
