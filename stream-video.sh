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
# honor the bitrate cap; stream_supervisor.py decides whether a cap applies and
# passes just the value (for example "2M"), empty for none.
#
# The video encoder is auto-selected at every start: hardware first (Intel
# Quick Sync, then VA-API), libx264 when no hardware works.
#
# Every flag is explained once, in the FLAG EXPLANATIONS table at the bottom.

VIDEO_FILE="$1"
STREAM_PATH="$2"
LOOP_COUNT="${3:--1}"
BITRATE_CAP="$4"
UDP_TARGET="$5"          # host:port for the KLV MPEG-TS/UDP feed; empty = RTSP only
RTSP_PORT="${MEDIAMTX_RTSP_PORT:?MEDIAMTX_RTSP_PORT is not set}"

# Shared encode settings. Deliberately space-split.
QSV_OPTS="-c:v h264_qsv -preset veryfast -async_depth 1 -pix_fmt nv12 -g 30 -keyint_min 30 -bf 0"
VAAPI_OPTS="-vf format=nv12,hwupload -c:v h264_vaapi -g 30 -keyint_min 30 -bf 0"
X264_OPTS="-c:v libx264 -preset ultrafast -tune zerolatency -g 30 -keyint_min 30 -sc_threshold 0 -bf 0 -x264-params ref=1"
AUDIO_OPTS="-c:a aac -b:a 128k"
TS_FIX="-fflags +genpts+igndts -avoid_negative_ts make_zero -max_muxing_queue_size 1024"

# gpuconfig passes the integrated GPU's render node through compose; the
# default covers a hand-run container with the device mapped.
VAAPI_DEV="-init_hw_device vaapi=va:${VAAPI_RENDER_NODE:-/dev/dri/renderD128} -filter_hw_device va"

# One throwaway frame through the candidate encoder; exit status is the gate.
trial() {
  timeout 5 ffmpeg -hide_banner -loglevel error -f lavfi \
    -i "nullsrc=size=320x240:rate=30" -frames:v 1 "$@" -f null - >/dev/null
}

# Encoder selection: try hardware encoders first via a one-frame trial encode —
# Intel Quick Sync, then generic VA-API (the AMD path). Missing /dev/dri, wrong
# render group, or a missing GPU runtime fails a trial in under a second and
# the next tier stands. Trials run the full option string so an option this
# driver rejects can never pass the trial and then kill the real encode.
# Stateless per start: the supervisor's restart path re-picks every time.
HWDEV_OPTS=""
if trial $QSV_OPTS; then
  VIDEO_OPTS="$QSV_OPTS"
  ENCODER="h264_qsv (Intel Quick Sync)"
elif trial $VAAPI_DEV $VAAPI_OPTS; then
  VIDEO_OPTS="$VAAPI_OPTS"
  HWDEV_OPTS="$VAAPI_DEV"
  ENCODER="h264_vaapi (VA-API hardware)"
else
  VIDEO_OPTS="$X264_OPTS"
  ENCODER="libx264 (software; hardware trials failed, see above)"
fi
echo "encoder: $ENCODER" >&2

BITRATE_OPTS=""
if [ -n "$BITRATE_CAP" ]; then
  BITRATE_OPTS="-b:v $BITRATE_CAP -maxrate $BITRATE_CAP -bufsize $BITRATE_CAP"
elif [ "$VIDEO_OPTS" = "$QSV_OPTS" ]; then
  # Uncapped libx264 defaults to CRF 23; uncapped h264_qsv would default to a
  # fixed 1 Mbps. ICQ mode at the same quality number restores parity.
  BITRATE_OPTS="-global_quality 23"
fi

# Output 1: RTSP -> MediaMTX, video+audio only.
set -- -map 0:v? -map 0:a? $VIDEO_OPTS $BITRATE_OPTS $AUDIO_OPTS $TS_FIX -vsync cfr \
       -f rtsp -rtsp_transport tcp "rtsp://localhost:${RTSP_PORT}/$STREAM_PATH"

# Output 2 (optional): MPEG-TS/UDP with all streams, data/KLV copied verbatim.
if [ -n "$UDP_TARGET" ]; then
  set -- "$@" -map 0 -copy_unknown $VIDEO_OPTS $BITRATE_OPTS $AUDIO_OPTS -c:d copy \
         -max_interleave_delta 1000 -bsf:d "setts=dts=max(DTS\,PREV_OUTDTS)" \
         $TS_FIX -f mpegts "udp://${UDP_TARGET}?pkt_size=1316"
fi

exec ffmpeg -hide_banner -nostats $HWDEV_OPTS -re -stream_loop "$LOOP_COUNT" -i "$VIDEO_FILE" "$@"

# FLAG EXPLANATIONS
#
# -hide_banner                   No version/build banner, so the captured log is signal
# -nostats                       No periodic progress line; it would grow the per-stream
#                                log file the supervisor captures stderr into forever
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
# ENCODER SELECTION (hardware first):
# timeout 5                      A wedged GPU driver fails the trial instead of
#                                hanging the stream start forever
# -f lavfi -i nullsrc=...        Synthetic input for the one-frame trial encode
# -frames:v 1                    Encode exactly one frame, then exit
# -f null -                      Discard the trial's encoded output
#
# VIDEO ENCODING, h264_qsv tier (Intel Quick Sync):
# -c:v h264_qsv                  Intel Quick Sync H.264 — needs /dev/dri mapped, the
#                                render group (see gpuconfig), and the
#                                intel-media-driver + onevpl-intel-gpu packages
# -preset veryfast               Fastest QSV preset (TargetUsage 7); qsv has no "ultrafast"
# -async_depth 1                 Pipeline depth 1: the qsv equivalent of -tune
#                                zerolatency (the default of 4 buffers extra frames)
# -pix_fmt nv12                  Convert any input to the NV12 the GPU expects, so odd
#                                formats (10-bit, 4:2:2) don't kill the real encode
#                                after passing the 8-bit trial
# -global_quality 23             (uncapped only) ICQ quality mode ~ libx264's CRF 23
#                                default; qsv would otherwise default to a fixed 1 Mbps
#
# VIDEO ENCODING, h264_vaapi tier (AMD APUs, or Intel without a oneVPL runtime):
# -init_hw_device vaapi=va:...   Open the integrated GPU's render node as VA-API
#                                device "va" (global option, so it rides the exec
#                                line, not the per-output opts)
# -filter_hw_device va           Let filter graphs (hwupload) use that device
# -vf format=nv12,hwupload       Normalize to NV12 in software, then upload frames
#                                to the GPU; plays the -pix_fmt nv12 role
# -c:v h264_vaapi                VA-API H.264 encode (Mesa radeonsi on AMD, iHD on Intel)
#
# VIDEO ENCODING, libx264 software tier (fixes GOP and B-frame issues):
# -c:v libx264                   Encode to H.264 (re-encode to fix structure)
# -preset ultrafast              Fastest encoding preset (low CPU usage)
# -tune zerolatency              Optimize for low-latency streaming
# -x264-params ref=1             Use only 1 reference frame (reduces loop boundary complexity)
# -sc_threshold 0                Disable scene detection (prevents unexpected keyframes)
#
# VIDEO ENCODING, all tiers:
# -g 30                          GOP size: 30 frames (1s @ 30fps) for quick recovery
# -keyint_min 30                 Minimum keyframe interval: 30 frames
# -bf 0                          Disable B-frames (eliminates reference frame issues at loop)
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
# -rtsp_transport tcp            (RTSP output) Send RTP over the RTSP TCP connection. Over
#                                the default UDP, ffmpeg keeps streaming into the void if
#                                MediaMTX drops the session; over TCP the write fails,
#                                ffmpeg exits, and the supervisor's dead-process check
#                                restarts the stream.
# -f mpegts udp://<udp-target>?pkt_size=1316            KLV-preserving MPEG-TS feed
