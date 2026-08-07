#!/bin/sh
# Stream a video file to MediaMTX (RTSP + HLS) and, when a UDP target is given,
# also as MPEG-TS over UDP with KLV/data streams preserved.
#
# Usage: stream-video.sh <video-file> <stream-path> [loop-count] [bitrate-cap] [udp-target] [video-mode] [audio-mode]
#
# Two outputs are produced from one ffmpeg process:
#   1. RTSP -> MediaMTX (which also republishes it as HLS). This is the human /
#      RTSP-client view. RTP cannot carry KLV, so only video+audio are mapped.
#   2. MPEG-TS over UDP to <udp-target> (host:port), with EVERY stream mapped and
#      data/KLV copied through untouched. This is the metadata-preserving feed.
#
# The supervisor decides WHETHER (mode, bitrate cap, eligibility); this script
# owns the ffmpeg syntax. In transcode mode video is re-encoded to a clean GOP
# structure to fix looping artifacts and to honor the bitrate cap, with the
# encoder auto-selected at every start: hardware first (Intel Quick Sync, then
# VA-API), libx264 when no hardware works. In passthrough mode the source video
# is sent unmodified — no decode, no encode, no GPU.
#
# Every flag is explained once, in the FLAG EXPLANATIONS table at the bottom.

VIDEO_FILE="$1"
STREAM_PATH="$2"
LOOP_COUNT="${3:--1}"
BITRATE_CAP="$4"
UDP_TARGET="$5"                # host:port for the KLV MPEG-TS/UDP feed; empty = RTSP only
VIDEO_MODE="${6:-transcode}"   # passthrough|transcode; the supervisor verified eligibility
AUDIO_MODE="${7:-encode}"      # copy|encode; copy means every audio track is already AAC
RTSP_PORT="${MEDIAMTX_RTSP_PORT:?MEDIAMTX_RTSP_PORT is not set}"

# Shared encode settings. Deliberately space-split.
QSV_OPTS="-c:v h264_qsv -preset veryfast -async_depth 1 -g 30 -keyint_min 30 -bf 0"
VAAPI_OPTS="-vf format=nv12,hwupload -c:v h264_vaapi -g 30 -keyint_min 30 -bf 0"
X264_OPTS="-c:v libx264 -preset ultrafast -tune zerolatency -g 30 -keyint_min 30 -sc_threshold 0 -bf 0 -x264-params ref=1"

if [ "$AUDIO_MODE" = "copy" ]; then
  AUDIO_OPTS="-c:a copy"
else
  AUDIO_OPTS="-c:a aac -b:a 128k"
fi

# gpuconfig passes the integrated GPU's render node through compose; the
# default covers a hand-run container with the device mapped.
VAAPI_DEV="-init_hw_device vaapi=va:${VAAPI_RENDER_NODE:-/dev/dri/renderD128} -filter_hw_device va"

# One throwaway frame through the candidate encoder; exit status is the gate.
trial() {
  timeout 5 ffmpeg -hide_banner -loglevel error -f lavfi \
    -i "nullsrc=size=320x240:rate=30" -frames:v 1 "$@" -f null - >/dev/null
}

if [ "$VIDEO_MODE" = "passthrough" ]; then
  # The supervisor verified every video track is H.264/H.265: send the source
  # bits unmodified. A bitrate cap cannot apply to copied packets (the
  # supervisor passes none), -vsync cfr is an encode-path operation ffmpeg
  # rejects with copy, and +igndts is dropped because copied B-frame packets
  # need their input DTS to be interleaved correctly.
  VIDEO_OPTS="-c:v copy"
  HWDEV_OPTS=""
  VSYNC_OPTS=""
  BITRATE_OPTS=""
  TS_FIX="-fflags +genpts -avoid_negative_ts make_zero -max_muxing_queue_size 1024"
  ENCODER="copy (passthrough)"
else
  VSYNC_OPTS="-vsync cfr"
  TS_FIX="-fflags +genpts+igndts -avoid_negative_ts make_zero -max_muxing_queue_size 1024"

  # Encoder selection: try hardware encoders first via a one-frame trial encode —
  # Intel Quick Sync, then generic VA-API (the AMD path). Missing /dev/dri, wrong
  # render group, or a missing GPU runtime fails a trial in under a second and
  # the next tier stands. Trials run the full option string so an option this
  # driver rejects can never pass the trial and then kill the real encode.
  # Stateless per start: the supervisor's restart path re-picks every time.
  # The qsv tier also decodes on the GPU when the input codec allows: decoding,
  # not encoding, was most of the CPU cost. This must be an explicit decoder
  # (-c:v h264_qsv etc. before -i), NOT -hwaccel qsv: -hwaccel renegotiates the
  # frame format when the decoder flushes at the -stream_loop seam and the run
  # dies with "Impossible to convert between the formats" one file-length in.
  # An explicit decoder keeps one output format for the whole run and survives
  # the seam. Gated to 8-bit 4:2:0 input so exotic profiles the GPU cannot
  # decode stay on the never-fails software decoder. The vaapi tier deliberately
  # decodes in software: without hardware-frame filters it would decode to
  # system memory and re-upload, which measured slower than software decode;
  # revisit on real AMD hardware.
  # head -1 because MPEG-TS inputs list the stream twice (program + stream table)
  IN_FORMAT=$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,pix_fmt -of csv=p=0 "$VIDEO_FILE" 2>/dev/null | head -n 1)
  QSV_DECODER=""
  case "$IN_FORMAT" in
    h264,yuv420p|h264,yuvj420p|h264,nv12)       QSV_DECODER="-c:v h264_qsv" ;;
    hevc,yuv420p|hevc,yuvj420p|hevc,nv12)       QSV_DECODER="-c:v hevc_qsv" ;;
    mjpeg,yuv420p|mjpeg,yuvj420p|mjpeg,nv12)    QSV_DECODER="-c:v mjpeg_qsv" ;;
    mpeg2video,yuv420p|mpeg2video,yuvj420p)     QSV_DECODER="-c:v mpeg2_qsv" ;;
    vp9,yuv420p|vp9,yuvj420p|vp9,nv12)          QSV_DECODER="-c:v vp9_qsv" ;;
  esac

  HWDEV_OPTS=""
  if trial $QSV_OPTS; then
    VIDEO_OPTS="$QSV_OPTS"
    HWDEV_OPTS="$QSV_DECODER"
    ENCODER="h264_qsv (Intel Quick Sync)${QSV_DECODER:+, GPU decode}"
  elif trial $VAAPI_DEV $VAAPI_OPTS; then
    VIDEO_OPTS="$VAAPI_OPTS"
    HWDEV_OPTS="$VAAPI_DEV"
    ENCODER="h264_vaapi (VA-API hardware)"
  else
    VIDEO_OPTS="$X264_OPTS"
    ENCODER="libx264 (software; hardware trials failed, see above)"
  fi

  BITRATE_OPTS=""
  if [ -n "$BITRATE_CAP" ]; then
    BITRATE_OPTS="-b:v $BITRATE_CAP -maxrate $BITRATE_CAP -bufsize $BITRATE_CAP"
  elif [ "$VIDEO_OPTS" = "$QSV_OPTS" ]; then
    # Uncapped libx264 defaults to CRF 23; uncapped h264_qsv would default to a
    # fixed 1 Mbps. ICQ mode at the same quality number restores parity.
    BITRATE_OPTS="-global_quality 23"
  fi
fi
echo "encoder: $ENCODER, audio: $([ "$AUDIO_MODE" = copy ] && echo copy || echo aac)" >&2

# Output 1: RTSP -> MediaMTX, video+audio only.
set -- -map 0:v? -map 0:a? $VIDEO_OPTS $BITRATE_OPTS $AUDIO_OPTS $TS_FIX $VSYNC_OPTS \
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
# MODE SELECTION (args 6 and 7):
# -c:v copy                      (passthrough) Send the source video unmodified. The
#                                supervisor only requests this after verifying every
#                                video track is H.264/H.265 (all MediaMTX carries).
#                                No GOP normalization happens, so loop recovery and
#                                HLS segmenting follow the source's keyframe cadence,
#                                and the bitrate cap is inert.
# -c:a copy                      (audio-mode copy) All mapped audio tracks were
#                                already AAC — re-encoding would burn CPU to lose
#                                quality. Decided by the supervisor per start.
#
# ENCODER SELECTION (transcode mode, hardware first):
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
# -c:v <codec>_qsv before -i     Decode on the GPU too — decoding, not encoding, was
#                                most of the CPU cost (~4x measured on 1080p MJPEG).
#                                Must be an explicit decoder, NOT -hwaccel qsv, which
#                                dies at the -stream_loop seam when the decoder flush
#                                renegotiates the frame format ("Impossible to
#                                convert between the formats"). Applied only to
#                                8-bit 4:2:0 input; anything else decodes in software
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
# -b:v/-maxrate/-bufsize <cap>   Cap the video bitrate (transcode only, when a cap is passed)
#
# AUDIO ENCODING (audio-mode encode):
# -c:a aac                       Encode to AAC
# -b:a 128k                      Audio bitrate: 128 kbps
#
# TIMESTAMP FIXES (eliminates negative DTS and discontinuities):
# -fflags +genpts                Regenerate presentation timestamps (fixes loop discontinuities)
# -fflags +igndts                (transcode only) Ignore input DTS (eliminates negative
#                                -0.067s DTS). Under passthrough the copied packets'
#                                input DTS are the only correct interleaving order for
#                                B-frame sources, so there it is dropped.
# -avoid_negative_ts make_zero   Shift all timestamps to start at 0 (prevents negative values)
# -vsync cfr                     (RTSP output, transcode only) Constant frame rate (even
#                                frame spacing at loop point); an encode-path operation
#                                ffmpeg rejects when combined with -c:v copy
# -max_interleave_delta 1000     (UDP output) Bound how long the muxer waits to interleave the
#                                sparse KLV data stream against video/audio.
# -bsf:d setts=dts=max(DTS,PREV_OUTDTS)
#                                (UDP output) Force copied data-stream DTS to stay monotonic
#                                after the video transcode retimes the program, and as a
#                                backstop at loop seams under passthrough — max() is a
#                                no-op while DTS are already monotonic. In the script
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
