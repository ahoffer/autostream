"""Render stream state for the HTTP API without I/O or registry access.

The caller holds the registry lock while rendering its streams.
"""
import shlex

import ffmpegcmd
from streampolicy import (PASSTHROUGH_CODECS, default_argv, default_mode,
                          encode_spec, passthrough_eligible)


def _output_state(stream, output, output_reachable):
    """(state, reason): the output's state word for the UI and, when it is not
    simply running, the human explanation."""
    if not output.enabled:
        return "stopped", "stopped"
    if output.name == "udp" and stream.udp_port is None:
        # Permanent for this slot, so report it ahead of the transient reasons.
        return "failed", f"no free UDP port in {stream.config.udp_base_port}-{stream.config.udp_last_port}"
    if output.running:
        if output.crashes:
            return "crashing", f"died {output.crashes}x; relaunched, watching"
        return "running", None
    if output.completed:
        return "completed", "played to the end"
    if output.name == "udp" and not output_reachable:
        return "waiting", f"{stream.config.output_host} unreachable"
    if output.crashes:
        return "restarting", f"died {output.crashes}x; relaunching"
    if output.last_error:
        return "failed", output.last_error
    return "starting", "starting"


def _output_status(stream, output, output_reachable):
    argv = output.launched_argv if output.running else default_argv(stream, output.name)
    state, reason = _output_state(stream, output, output_reachable)
    return {
        "state": state,
        "running": output.running,
        "enabled": output.enabled,
        "stopping": output.stopping,
        "command": shlex.join(argv) if argv else None,
        "reason": reason,
        "error": output.last_error if output.crashes else None,
    }


def get_stream_status(streams, output_reachable):
    """Get status of all streams"""
    result = []
    for stream in streams:
        urls = stream.urls()
        spec = encode_spec(stream)
        profile = stream.profile or ffmpegcmd.UNKNOWN_PROFILE
        video_codecs = profile.video_codecs
        source_rate = profile.frame_rate
        requested_mode = stream.mode or default_mode(video_codecs)
        passthrough_active = stream.rtsp.running and spec.passthrough
        if requested_mode != "passthrough" or passthrough_active:
            passthrough_reason = None
        elif video_codecs is not None and not passthrough_eligible(video_codecs):
            # Permanent for this file, so report it ahead of the transient reasons.
            blocked = sorted({c for c in video_codecs if c not in PASSTHROUGH_CODECS})
            passthrough_reason = (f"video codec {', '.join(blocked)} cannot pass through "
                                  f"(H.264, H.265, MPEG-2, MPEG-4, MJPEG only)")
        elif not stream.rtsp.running:
            passthrough_reason = "stream stopped"
        elif video_codecs is None:
            passthrough_reason = "codec probe failed; transcoding"
        else:
            passthrough_reason = "starting"
        result.append({
            "name": stream.name,
            "filename": stream.filename,
            "running": stream.running,
            "stopping": stream.stopping,
            "loop_count": stream.loop_count,
            "gop_frames": spec.gop_frames,
            "gop_explicit": stream.gop_frames is not None,
            "frame_rate": stream.frame_rate or source_rate,
            "frame_rate_explicit": stream.frame_rate is not None,
            "source_frame_rate": source_rate,
            "mode": requested_mode,
            "effective_mode": "passthrough" if spec.passthrough else "transcode",
            "passthrough_eligible": (passthrough_eligible(video_codecs)
                                     if video_codecs is not None else None),
            "ineligible_codecs": sorted({c for c in video_codecs or () if c not in PASSTHROUGH_CODECS}),
            "passthrough_active": passthrough_active,
            "passthrough_reason": passthrough_reason,
            "encoder": spec.encoder_label,
            "rtsp_url": urls["rtsp"],
            "hls_url": urls["hls"],
            "udp_url": urls["udp"],
            "outputs": {output.name: _output_status(stream, output, output_reachable) for output in stream.outputs},
        })
    return result

