"""Launch, stop, and diagnose FFmpeg processes without owning stream state."""
import logging
import os
import subprocess

log = logging.getLogger(__name__)

FFMPEG_LOG_TAIL_BYTES = 4096
FFMPEG_REASON_CHARS = 80


def _ffmpeg_log_path(stream_name, output_name, config):
    return config.ffmpeg_log_dir / f"{stream_name}.{output_name}.log"


def _ffmpeg_tail(stream_name, output_name, config):
    """The end of an output's ffmpeg log, or "" when there is none."""
    try:
        with open(_ffmpeg_log_path(stream_name, output_name, config), "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, log_file.tell() - FFMPEG_LOG_TAIL_BYTES))
            return log_file.read().decode(errors="replace").strip()
    except OSError as error:
        log.warning("No ffmpeg log for %s/%s: %s", stream_name, output_name, error)
        return ""


def _last_lines(text, count):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def ffmpeg_death_reason(stream_name, output_name, config):
    """Why an output's ffmpeg died: (line for the log, lines for the UI).

    ffmpeg output goes to a per-output file rather than the console (except in
    debug mode, where it was already visible), so the reason is read back from
    that file. The log gets the last line, cut short to stay on one line; the
    UI gets the last two lines whole.
    """
    if config.show_ffmpeg_output:
        return "see the container log", "see the container log"
    tail = _ffmpeg_tail(stream_name, output_name, config)
    if not tail:
        return "ffmpeg exited without output", "ffmpeg exited without output"
    last_line = _last_lines(tail, 1)
    if len(last_line) > FFMPEG_REASON_CHARS:
        last_line = last_line[:FFMPEG_REASON_CHARS - 1] + "…"
    return last_line, _last_lines(tail, 2)


def launch_output(argv, stream_name, output_name, config):
    """Launch one command, directing its output to the configured destination."""
    if config.show_ffmpeg_output:
        process = subprocess.Popen(argv)
    else:
        config.ffmpeg_log_dir.mkdir(exist_ok=True)
        # The child keeps its own fd, so closing ours right away is safe.
        with open(_ffmpeg_log_path(stream_name, output_name, config), "wb") as ffmpeg_log:
            process = subprocess.Popen(argv, stdout=ffmpeg_log, stderr=ffmpeg_log)
    return process


def stop_output(process, stream_name, output_name, should_terminate):
    """Return whether the process exited; a wedged process still owns its slot."""
    if should_terminate:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if not should_terminate:
            log.warning("Output still stopping: %s/%s", stream_name, output_name)
            return False
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Better to leak a wedged process than to hang the supervisor forever
            # (can happen on stuck NFS/CIFS reads inside ffmpeg). Keep the output
            # blocked so a second ffmpeg process can't reuse the same destination.
            log.error("%s/%s did not die after SIGKILL, leaking pid %d",
                      stream_name, output_name, process.pid)
            return False
    return True
