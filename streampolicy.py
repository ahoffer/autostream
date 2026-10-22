"""Pure encoding and launch decisions from stream state and configuration."""
import ffmpegcmd


# Passthrough policy: the codecs MediaMTX carries over RTSP unencoded. Only
# H.264 passes through by default: HLS through MediaMTX and browsers need it,
# while HEVC, MPEG-2, MPEG-4 part 2 and MJPEG reach RTSP consumers only (the
# way a real camera with that encoder would), so they take an explicit request.
PASSTHROUGH_CODECS = frozenset({"h264", "hevc", "mpeg2video", "mpeg4", "mjpeg"})
PASSTHROUGH_DEFAULT_CODECS = frozenset({"h264"})


def default_mode(video_codecs):
    """Codec-derived default mode: all-H.264 files pass through, all else transcodes.

    None or empty (never probed, probe failed, or no video track) defaults to
    transcode — the mode that works for everything.
    """
    if video_codecs and all(c in PASSTHROUGH_DEFAULT_CODECS for c in video_codecs):
        return "passthrough"
    return "transcode"


def passthrough_eligible(video_codecs):
    """True when every video track can ride MediaMTX unencoded.

    All tracks matter because the generated commands map every video track.
    """
    return bool(video_codecs) and all(c in PASSTHROUGH_CODECS for c in video_codecs)


def bitrate_cap_for(bitrate_bps, config):
    """MAX_VIDEO_BITRATE when the video exceeds it, else empty string.

    An unknown bitrate (None) gets no cap: there is nothing to compare against.
    """
    if config.max_video_bps and bitrate_bps and bitrate_bps > config.max_video_bps:
        return config.max_video_bitrate
    return ""


def encode_spec(stream):
    """The supervisor's decisions for this stream, from its profile and choices.

    Passthrough eligibility is re-derived from the profile, so a replaced file
    or failed probe degrades to transcode instead of producing a command
    ffmpeg cannot run. The caller must synchronize reads of mutable stream state.
    """
    profile = stream.profile or ffmpegcmd.UNKNOWN_PROFILE
    requested_mode = stream.mode or default_mode(profile.video_codecs)
    output_rate = stream.frame_rate or profile.frame_rate
    gop_frames = stream.gop_frames or ffmpegcmd.natural_gop_frames(output_rate)
    passthrough = requested_mode == "passthrough" and passthrough_eligible(profile.video_codecs)
    return ffmpegcmd.EncodeSpec(
        video_path=stream.video_path,
        stream_name=stream.name,
        loop_count=stream.loop_count,
        gop_frames=gop_frames,
        frame_rate=stream.frame_rate,
        passthrough=passthrough,
        audio_copy=profile.audio_copyable,
        # Copied packets can't honor a cap.
        bitrate_cap="" if passthrough else bitrate_cap_for(profile.bitrate_bps, stream.config),
        rtsp_port=stream.config.rtsp_port,
        udp_target=f"{stream.config.output_host}:{stream.udp_port}" if stream.udp_port else None,
    )


def default_argv(stream, output_name):
    """The generated command for one output, or None when it cannot be built."""
    return ffmpegcmd.build_argv(encode_spec(stream), output_name)


def udp_launchable(stream, output_reachable):
    """Whether the udp output can be launched now, with the reason when not.

    No port from the range is permanent for this slot; an unreachable consumer
    is transient, and launching anyway would only make ffmpeg die at start.
    """
    if stream.udp_port is None:
        return False, f"no free UDP port in {stream.config.udp_base_port}-{stream.config.udp_last_port}"
    if not output_reachable:
        return False, f"{stream.config.output_host} unreachable"
    return True, None

