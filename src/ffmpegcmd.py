"""Everything ffmpeg: probing a file and building argv.

MediaStream decides whether delivery is enabled. This module probes media,
resolves encoding choices, and renders the resulting command line. Each stream
is its own ffmpeg process, so build_argv produces one complete command per
stream, shaped by its Delivery:

  rtsp muxer     RTSP -> MediaMTX (which republishes it as HLS and RTMP). RTP
                 cannot carry KLV, so only video+audio are mapped.
  other muxers   MPEG-TS, plain or inside RTP, or as HLS segments, with EVERY
                 stream mapped and data/KLV copied through untouched. These
                 are the metadata-preserving feeds.

In transcode mode video is re-encoded to a clean GOP structure to fix looping
artifacts and to honor the bitrate cap, using libx264 in software. In
passthrough mode the source video is sent unmodified — no decode or encode.

Every flag is explained once, in the FLAG EXPLANATIONS table at the bottom.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# GOP fallback in frames when the file's frame rate is unknown. The natural
# default is one second of frames at the file's own rate (natural_gop_frames);
# each stream can override it in the UI.
DEFAULT_GOP_FRAMES = 30


def natural_gop_frames(frame_rate):
    """One second of frames at the output rate, the GOP used unless overridden."""
    if frame_rate:
        return max(1, round(frame_rate))
    return DEFAULT_GOP_FRAMES

@dataclass(frozen=True)
class MediaProbeResult:
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
    data_codecs: tuple | None = ()   # KLV shows up here as 'klv'


UNKNOWN_PROFILE = MediaProbeResult(None, None, False, None, None, None, None)


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
    data_streams = [entry for entry in ordered if entry.get("codec_type") == "data"]
    bitrate = None
    for candidate in (*(entry.get("bit_rate") for entry in video_streams[:1]),
                      report.get("format", {}).get("bit_rate")):
        if isinstance(candidate, str) and candidate.isdigit():
            bitrate = int(candidate)
            break
    return MediaProbeResult(
        video_codecs=tuple(entry.get("codec_name", "") for entry in video_streams),
        audio_codecs=tuple(entry.get("codec_name", "") for entry in audio_streams),
        audio_copyable=all(entry.get("codec_name") == "aac" and entry.get("extradata_size", 0) > 0
                           for entry in audio_streams),
        pix_fmt=video_streams[0].get("pix_fmt") if video_streams else None,
        bitrate_bps=bitrate,
        frame_rate=_parse_frame_rate(video_streams[0].get("avg_frame_rate")) if video_streams else None,
        data_codecs=tuple(entry.get("codec_name", "") for entry in data_streams),
    )


@dataclass(frozen=True)
class FFmpegCommandSettings:
    """Immutable encoding decisions for one command, after media inspection."""
    loop_count: int
    gop_frames: int
    frame_rate: float | None    # output rate under transcode; None keeps the source's
    passthrough: bool
    audio_copy: bool
    bitrate_cap: str

    @property
    def encoder_label(self):
        if self.passthrough:
            return "copy (passthrough)"
        return "libx264 (software)"


def build_argv(source_path, spec, delivery):
    """Render the prepared decisions as one complete FFmpeg command for one delivery."""
    if spec.passthrough:
        # The supervisor verified every video track is one MediaMTX carries: send the
        # source bits unmodified. A bitrate cap cannot apply to copied packets
        # (the supervisor passes none), -vsync cfr is an encode-path operation
        # ffmpeg rejects with copy, and +igndts is dropped because copied
        # B-frame packets need their input DTS to be interleaved correctly.
        video_opts = ["-c:v", "copy"]
        bitrate_opts = []
        vsync_opts = []
        ts_fix = ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
                  "-max_muxing_queue_size", "1024"]
    else:
        gop = str(spec.gop_frames)
        video_opts = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                      "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-bf", "0",
                      "-x264-params", "ref=1"]
        if spec.frame_rate:
            video_opts += ["-r", f"{spec.frame_rate:g}"]
        if spec.bitrate_cap:
            bitrate_opts = ["-b:v", spec.bitrate_cap, "-maxrate", spec.bitrate_cap,
                            "-bufsize", spec.bitrate_cap]
        else:
            bitrate_opts = []
        vsync_opts = ["-vsync", "cfr"]
        ts_fix = ["-fflags", "+genpts+igndts", "-avoid_negative_ts", "make_zero",
                  "-max_muxing_queue_size", "1024"]

    audio_opts = ["-c:a", "copy"] if spec.audio_copy else ["-c:a", "aac", "-b:a", "128k"]

    argv = ["ffmpeg", "-hide_banner", "-nostats",
            "-re", "-stream_loop", str(spec.loop_count), "-i", str(source_path)]
    if delivery.carries_klv:
        # Inside RTP the transport stream is re-muxed once more, and that outer
        # muxer insists on monotonic timestamps, so there the muxer must be
        # allowed to interleave the sparse KLV packets properly.
        interleave = [] if delivery.muxer == "rtp_mpegts" else ["-max_interleave_delta", "1000"]
        argv += ["-map", "0", "-copy_unknown", *video_opts, *bitrate_opts, *audio_opts,
                 "-c:d", "copy", *interleave,
                 "-bsf:d", "setts=dts=max(DTS\\,PREV_OUTDTS)", *ts_fix]
    else:
        argv += ["-map", "0:v?", "-map", "0:a?", *video_opts, *bitrate_opts, *audio_opts,
                 *ts_fix, *vsync_opts]
    argv += list(delivery.extra_args)
    if delivery.muxer == "rtsp":
        argv += ["-f", "rtsp", "-rtsp_transport", "tcp", delivery.url]
    elif delivery.muxer == "hls":
        argv += ["-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_flags", "delete_segments",
                 "-hls_segment_type", "mpegts",
                 "-hls_segment_filename", str(Path(delivery.url).with_name("seg%05d.ts")), delivery.url]
    else:
        argv += ["-f", delivery.muxer, delivery.url]
    return argv


PASSTHROUGH_CODECS = frozenset({'h264', 'hevc', 'mpeg2video', 'mpeg4', 'mjpeg'})


def prepare(source_path, settings, delivery, max_video_bps=None):
    """Probe once and prepare one stream, with no live domain objects."""
    profile = probe_file(source_path)
    return _command_for_profile(source_path, settings, delivery, profile, max_video_bps)


def _command_for_profile(source_path, settings, delivery, profile, max_video_bps=None):
    """Keep decisions reproducible from a probe and explicit command inputs."""
    eligible = bool(profile.video_codecs) and all(c in PASSTHROUGH_CODECS for c in profile.video_codecs)
    default_mode = 'passthrough' if profile.video_codecs and set(profile.video_codecs) == {'h264'} else 'transcode'
    mode = settings.get('mode') or default_mode
    passthrough = mode == 'passthrough' and eligible
    fps = settings.get('frame_rate')
    gop = settings.get('gop_frames') or natural_gop_frames(fps or profile.frame_rate)
    limit = max_video_bps
    cap = str(limit) if limit and profile.bitrate_bps and profile.bitrate_bps > limit and not passthrough else ''
    spec = FFmpegCommandSettings(settings['loop_count'], gop, fps, passthrough, profile.audio_copyable, cap)
    return {
        'argv': tuple(build_argv(source_path, spec, delivery)) if delivery.url else (),
        'error': None if profile.video_codecs else 'No video track found; waiting for the file to change',
        'video_codecs': list(profile.video_codecs or ()), 'audio_codecs': list(profile.audio_codecs or ()),
        'data_codecs': list(profile.data_codecs or ()),
        'mode': mode, 'effective_mode': 'passthrough' if passthrough else 'transcode',
        'encoder': spec.encoder_label, 'passthrough_eligible': eligible if profile.video_codecs is not None else None,
        'ineligible_codecs': sorted(set(profile.video_codecs or ()) - PASSTHROUGH_CODECS),
        'source_frame_rate': profile.frame_rate,
        'frame_rate': fps or profile.frame_rate, 'frame_rate_explicit': fps is not None,
        'gop_frames': gop, 'gop_explicit': settings.get('gop_frames') is not None,
    }


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
# VIDEO ENCODING (fixes GOP and B-frame issues):
# -c:v libx264                   Encode to H.264 (re-encode to fix structure)
# -preset ultrafast              Fastest encoding preset (low CPU usage)
# -tune zerolatency              Optimize for low-latency streaming
# -x264-params ref=1             Use only 1 reference frame (reduces loop boundary complexity)
# -sc_threshold 0                Disable scene detection (prevents unexpected keyframes)
#
# VIDEO STRUCTURE AND BITRATE:
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
# -max_interleave_delta 1000     (transport stream outputs except RTP) Bound how long the
#                                muxer waits to interleave the sparse KLV data stream
#                                against video/audio. Omitted for rtp_mpegts: its outer
#                                RTP muxer rejects the out-of-order timestamps that an
#                                early flush produces, so the default wait applies there.
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
# OUTPUTS (one per transport kind; see transports.py for the URL of each):
# -f rtsp rtsp://localhost:<rtsp-port>/<stream-name>   Publish to MediaMTX (serves RTSP, HLS, RTMP)
# -rtsp_transport tcp            (RTSP output) Send RTP over the RTSP TCP connection. Over
#                                the default UDP, ffmpeg keeps streaming into the void if
#                                MediaMTX drops the session; over TCP the write fails,
#                                ffmpeg exits, and the supervisor's dead-process check
#                                restarts the stream.
# -f mpegts <url>                KLV-preserving MPEG-TS over UDP (?pkt_size=1316 keeps 7
#                                TS packets per datagram), multicast (&ttl=1 stays on the
#                                LAN), SRT (?mode=listener waits for the consumer), TCP
#                                (?listen=1) or HTTP (-listen 1, one client at a time)
# -f rtp_mpegts <url>            The same transport stream wrapped in RTP, plain or over
#                                SRTP (-srtp_out_suite/-srtp_out_params carry the cipher
#                                and the shared key)
# -f hls ...                     The transport stream cut into 2-second segments, 6 kept,
#                                old ones deleted; the API server serves the directory
