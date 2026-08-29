"""Everything ffmpeg: probing a file, picking an encoder, and building argv.

The supervisor decides WHETHER (mode, bitrate cap, eligibility, which outputs
run); this module owns the ffmpeg syntax that turns those decisions into a
command line. Each output of a stream is its own ffmpeg process, so build_argv
produces one complete command per output:

  rtsp  RTSP -> MediaMTX (which also republishes it as HLS). This is the
        human / RTSP-client view. RTP cannot carry KLV, so only video+audio
        are mapped.
  udp   MPEG-TS over UDP to host:port, with EVERY stream mapped and data/KLV
        copied through untouched. This is the metadata-preserving feed.

In transcode mode video is re-encoded to a clean GOP structure to fix looping
artifacts and to honor the bitrate cap, with the encoder auto-selected by a
one-frame trial: hardware first (Intel Quick Sync, then VA-API), libx264 when
no hardware works. In passthrough mode the source video is sent unmodified —
no decode, no encode, no GPU.

Every flag is explained once, in the FLAG EXPLANATIONS table at the bottom.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

OUTPUT_NAMES = ("rtsp", "udp")

# GOP fallback in frames when the file's frame rate is unknown. The natural
# default is one second of frames at the file's own rate (natural_gop_frames);
# each stream can override it in the UI.
DEFAULT_GOP_FRAMES = 30


def natural_gop_frames(frame_rate):
    """One second of frames at the output rate, the GOP used unless overridden."""
    if frame_rate:
        return max(1, round(frame_rate))
    return DEFAULT_GOP_FRAMES

ENCODER_TIERS = ("qsv", "vaapi", "x264")
# Keyed by tier: the label is looked up from the cached tier for logs and status.
ENCODER_LABELS = {
    "qsv": "h264_qsv (Intel Quick Sync)",
    "vaapi": "h264_vaapi (VA-API hardware)",
    "x264": "libx264 (software)",
}

# Keyed by (codec_name, pix_fmt): the decoder table the qsv tier consults.
# Gated to 8-bit 4:2:0 input so exotic profiles the GPU cannot decode stay on
# the never-fails software decoder.
QSV_DECODERS = {
    ("h264", "yuv420p"): "h264_qsv", ("h264", "yuvj420p"): "h264_qsv", ("h264", "nv12"): "h264_qsv",
    ("hevc", "yuv420p"): "hevc_qsv", ("hevc", "yuvj420p"): "hevc_qsv", ("hevc", "nv12"): "hevc_qsv",
    ("mjpeg", "yuv420p"): "mjpeg_qsv", ("mjpeg", "yuvj420p"): "mjpeg_qsv", ("mjpeg", "nv12"): "mjpeg_qsv",
    ("mpeg2video", "yuv420p"): "mpeg2_qsv", ("mpeg2video", "yuvj420p"): "mpeg2_qsv",
    ("vp9", "yuv420p"): "vp9_qsv", ("vp9", "yuvj420p"): "vp9_qsv", ("vp9", "nv12"): "vp9_qsv",
}


@dataclass(frozen=True)
class FileProfile:
    """What ffprobe says about a video file; the inputs to every encode decision.

    None in a field means the probe failed or the file has no such track.
    audio_copyable is True only when each audio track is AAC that already
    carries global headers (extradata): ADTS AAC — the packaging MPEG-TS
    sources use — has none, and ffmpeg's RTSP muxer needs the headers at SDP
    time, before the first packet flows, so the aac_adtstoasc bitstream filter
    cannot rescue a copy. Such audio is re-encoded instead.
    """
    video_codecs: tuple | None
    audio_codecs: tuple | None
    audio_copyable: bool
    pix_fmt: str | None
    bitrate_bps: int | None
    frame_rate: float | None


UNKNOWN_PROFILE = FileProfile(None, None, False, None, None, None)


def _parse_frame_rate(text):
    """ffprobe's "30000/1001" rational (or "0/0" for unknown) as a float, or None."""
    try:
        numerator, denominator = (int(part) for part in str(text).split("/"))
    except ValueError:
        return None
    return numerator / denominator if numerator > 0 and denominator > 0 else None


def probe_file(video_path):
    """Probe the file with ffprobe; UNKNOWN_PROFILE when the probe fails.

    The codec tuples are index-ordered and cover every track, because the
    generated commands map every track. Bitrate comes from the first video
    stream, falling back to the container: ffprobe reports N/A for both on
    some MPEG-TS files, which is simply an unknown bitrate.

    JSON output rather than csv: csv emits fields in ffprobe's internal order,
    not the requested order, and MPEG-TS inputs repeat streams under a programs
    section — json.loads()["streams"] dodges both traps.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             "stream=index,codec_type,codec_name,pix_fmt,extradata_size,bit_rate,avg_frame_rate"
             ":format=bit_rate",
             "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as error:
        log.warning("Error probing %s: %s", Path(video_path).name, error)
        return UNKNOWN_PROFILE
    if result.returncode != 0:
        return UNKNOWN_PROFILE
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return UNKNOWN_PROFILE
    ordered = sorted(report.get("streams", []), key=lambda entry: entry.get("index", 0))
    video_streams = [entry for entry in ordered if entry.get("codec_type") == "video"]
    audio_streams = [entry for entry in ordered if entry.get("codec_type") == "audio"]
    bitrate = None
    for candidate in (*(entry.get("bit_rate") for entry in video_streams[:1]),
                      report.get("format", {}).get("bit_rate")):
        if isinstance(candidate, str) and candidate.isdigit():
            bitrate = int(candidate)
            break
    return FileProfile(
        video_codecs=tuple(entry.get("codec_name", "") for entry in video_streams),
        audio_codecs=tuple(entry.get("codec_name", "") for entry in audio_streams),
        audio_copyable=all(entry.get("codec_name") == "aac" and entry.get("extradata_size", 0) > 0
                           for entry in audio_streams),
        pix_fmt=video_streams[0].get("pix_fmt") if video_streams else None,
        bitrate_bps=bitrate,
        frame_rate=_parse_frame_rate(video_streams[0].get("avg_frame_rate")) if video_streams else None,
    )


def qsv_decoder_for(video_codec, pix_fmt):
    """The GPU decoder for this input, or None to decode in software."""
    return QSV_DECODERS.get((video_codec, pix_fmt))


def _vaapi_device_opts(render_node):
    return ["-init_hw_device", f"vaapi=va:{render_node}", "-filter_hw_device", "va"]


def _tier_video_opts(tier, gop_frames):
    gop = str(gop_frames)
    if tier == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-async_depth", "1",
                "-g", gop, "-keyint_min", gop, "-bf", "0"]
    if tier == "vaapi":
        return ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                "-g", gop, "-keyint_min", gop, "-bf", "0"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-bf", "0",
            "-x264-params", "ref=1"]


def _trial_succeeds(encoder_opts):
    """One throwaway frame through the candidate encoder; exit status is the gate."""
    try:
        result = subprocess.run(
            ["timeout", "5", "ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "nullsrc=size=320x240:rate=30", "-frames:v", "1",
             *encoder_opts, "-f", "null", "-"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def detect_encoder_tier(vaapi_render_node):
    """Pick the first encoder tier that works: qsv, then vaapi, then x264.

    Hardware encoders are tried via a one-frame trial encode — Intel Quick
    Sync, then generic VA-API (the AMD path). Missing /dev/dri, wrong render
    group, or a missing GPU runtime fails a trial in under a second and the
    next tier stands. Trials run the full option string so an option this
    driver rejects can never pass the trial and then kill the real encode.
    """
    if _trial_succeeds(_tier_video_opts("qsv", DEFAULT_GOP_FRAMES)):
        return "qsv"
    if _trial_succeeds([*_vaapi_device_opts(vaapi_render_node),
                        *_tier_video_opts("vaapi", DEFAULT_GOP_FRAMES)]):
        return "vaapi"
    return "x264"


@dataclass(frozen=True)
class EncodeSpec:
    """Every decision the supervisor made for one stream, ready to become argv.

    udp_target is None when the stream has no UDP port, in which case the udp
    command cannot be built.
    """
    video_path: str
    stream_name: str
    loop_count: int
    gop_frames: int
    frame_rate: float | None    # output rate under transcode; None keeps the source's
    passthrough: bool
    audio_copy: bool
    bitrate_cap: str
    encoder_tier: str
    qsv_decoder: str | None
    vaapi_render_node: str
    rtsp_port: int
    udp_target: str | None

    @property
    def encoder_label(self):
        if self.passthrough:
            return "copy (passthrough)"
        label = ENCODER_LABELS[self.encoder_tier]
        if self.encoder_tier == "qsv" and self.qsv_decoder:
            label += ", GPU decode"
        return label


def build_argv(spec, output_name):
    """The complete ffmpeg command for one output of a stream, or None.

    None only for the udp output of a stream without a UDP target.
    """
    if output_name == "udp" and spec.udp_target is None:
        return None

    if spec.passthrough:
        # The supervisor verified every video track is one MediaMTX carries: send the
        # source bits unmodified. A bitrate cap cannot apply to copied packets
        # (the supervisor passes none), -vsync cfr is an encode-path operation
        # ffmpeg rejects with copy, and +igndts is dropped because copied
        # B-frame packets need their input DTS to be interleaved correctly.
        input_opts = []
        video_opts = ["-c:v", "copy"]
        bitrate_opts = []
        vsync_opts = []
        ts_fix = ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
                  "-max_muxing_queue_size", "1024"]
    else:
        # The qsv tier also decodes on the GPU when the input codec allows:
        # decoding, not encoding, was most of the CPU cost. This must be an
        # explicit decoder (-c:v h264_qsv etc. before -i), NOT -hwaccel qsv:
        # -hwaccel renegotiates the frame format when the decoder flushes at
        # the -stream_loop seam and the run dies with "Impossible to convert
        # between the formats" one file-length in. The vaapi tier deliberately
        # decodes in software: without hardware-frame filters it would decode
        # to system memory and re-upload, which measured slower than software
        # decode; revisit on real AMD hardware.
        if spec.encoder_tier == "qsv" and spec.qsv_decoder:
            input_opts = ["-c:v", spec.qsv_decoder]
        elif spec.encoder_tier == "vaapi":
            input_opts = _vaapi_device_opts(spec.vaapi_render_node)
        else:
            input_opts = []
        video_opts = _tier_video_opts(spec.encoder_tier, spec.gop_frames)
        if spec.frame_rate:
            video_opts += ["-r", f"{spec.frame_rate:g}"]
        if spec.bitrate_cap:
            bitrate_opts = ["-b:v", spec.bitrate_cap, "-maxrate", spec.bitrate_cap,
                            "-bufsize", spec.bitrate_cap]
        elif spec.encoder_tier == "qsv":
            # Uncapped libx264 defaults to CRF 23; uncapped h264_qsv would
            # default to a fixed 1 Mbps. ICQ mode at the same quality number
            # restores parity.
            bitrate_opts = ["-global_quality", "23"]
        else:
            bitrate_opts = []
        vsync_opts = ["-vsync", "cfr"]
        ts_fix = ["-fflags", "+genpts+igndts", "-avoid_negative_ts", "make_zero",
                  "-max_muxing_queue_size", "1024"]

    audio_opts = ["-c:a", "copy"] if spec.audio_copy else ["-c:a", "aac", "-b:a", "128k"]

    argv = ["ffmpeg", "-hide_banner", "-nostats", *input_opts,
            "-re", "-stream_loop", str(spec.loop_count), "-i", spec.video_path]
    if output_name == "rtsp":
        argv += ["-map", "0:v?", "-map", "0:a?", *video_opts, *bitrate_opts, *audio_opts,
                 *ts_fix, *vsync_opts,
                 "-f", "rtsp", "-rtsp_transport", "tcp",
                 f"rtsp://localhost:{spec.rtsp_port}/{spec.stream_name}"]
    else:
        argv += ["-map", "0", "-copy_unknown", *video_opts, *bitrate_opts, *audio_opts,
                 "-c:d", "copy", "-max_interleave_delta", "1000",
                 "-bsf:d", "setts=dts=max(DTS\\,PREV_OUTDTS)",
                 *ts_fix, "-f", "mpegts", f"udp://{spec.udp_target}?pkt_size=1316"]
    return argv


# FLAG EXPLANATIONS
#
# -hide_banner                   No version/build banner, so the captured log is signal
# -nostats                       No periodic progress line; it would grow the per-stream
#                                log file the supervisor captures stderr into forever
# ffmpeg                         Every command must start with ffmpeg; the supervisor
#                                runs argv directly, no shell
# -re                            Read input at native frame rate (real-time streaming)
# -stream_loop N                 Loop the input N times; -1 = forever
# -i <video-file>                Input video file
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
# MODE SELECTION (passthrough / audio copy, decided by the supervisor):
# -c:v copy                      (passthrough) Send the source video unmodified. The
#                                supervisor only requests this after verifying every
#                                video track is one MediaMTX carries over RTSP.
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
# -g <gop>                       GOP size in frames, the stream's GOP setting
#                                (default 30 = 1s @ 30fps) for quick recovery
# -keyint_min <gop>              Minimum keyframe interval, same value
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
# -r <fps>                       (transcode only, when the stream's FPS setting is set)
#                                Output frame rate; frames are dropped or duplicated
#                                to hit it. Absent, the source rate is kept
# -vsync cfr                     (RTSP output, transcode only) Constant frame rate (even
#                                frame spacing at loop point); an encode-path operation
#                                ffmpeg rejects when combined with -c:v copy
# -max_interleave_delta 1000     (UDP output) Bound how long the muxer waits to interleave the
#                                sparse KLV data stream against video/audio.
# -bsf:d setts=dts=max(DTS,PREV_OUTDTS)
#                                (UDP output) Force copied data-stream DTS to stay monotonic
#                                after the video transcode retimes the program, and as a
#                                backstop at loop seams under passthrough — max() is a
#                                no-op while DTS are already monotonic. The comma is
#                                escaped as \, for ffmpeg's filter parser.
#
# STREAM RELIABILITY:
# -max_muxing_queue_size 1024    Prevent buffer overflows during encoding
#
# OUTPUTS:
# -f rtsp rtsp://localhost:<rtsp-port>/<stream-name>   Publish to MediaMTX (serves RTSP + HLS)
# -rtsp_transport tcp            (RTSP output) Send RTP over the RTSP TCP connection. Over
#                                the default UDP, ffmpeg keeps streaming into the void if
#                                MediaMTX drops the session; over TCP the write fails,
#                                ffmpeg exits, and the supervisor's dead-process check
#                                restarts the stream.
# -f mpegts udp://<udp-target>?pkt_size=1316            KLV-preserving MPEG-TS feed
