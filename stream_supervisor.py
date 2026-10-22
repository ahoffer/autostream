#!/usr/bin/env python3
"""Autostream supervisor: turn the video files in /app/videos into live streams.

Wires the pieces together and runs the loop that drives them: filewatch reports
file changes, streams owns the slots and their ffmpeg processes, and streamapi
serves the control UI alongside.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import filewatch
import streamapi
from appconfig import Config
from streammodel import is_ignored
from streams import StreamManager


class OneLineFormatter(logging.Formatter):
    """Keep every record on one line so the log stays scannable: a multi-line
    argument (an exception text, an ffmpeg tail) is joined with "; "."""

    def format(self, record):
        return super().format(record).replace("\n", "; ")


def configure_logging(log_level):
    logging.addLevelName(logging.WARNING, "WARN")
    handler = logging.StreamHandler()
    handler.setFormatter(OneLineFormatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=log_level.upper(), handlers=[handler])


def wait_for_mediamtx(rtsp_port):
    log = logging.getLogger(__name__)
    log.debug("Waiting for MediaMTX to be available on port %d...", rtsp_port)
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("localhost", rtsp_port))
                break
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(1)
    log.debug("MediaMTX is ready")


def main():
    config = Config.from_env()
    configure_logging(config.log_level)
    streams = StreamManager(config)
    log = logging.getLogger(__name__)
    log.info("Supervisor starting, UI http://localhost:%d", config.api_port)

    wait_for_mediamtx(config.rtsp_port)
    # Before the first poll: its creates claim slots, which assign UDP ports
    # and adopt remembered settings.
    streams.load_persisted_state()
    streams.refresh_output_reachable()

    threading.Thread(target=streamapi.serve, args=(config.api_port, streams),
                     name="APIServer", daemon=True).start()

    # The files already present arrive together in the first poll that reports
    # creates (the watcher holds them for its quiet period first); those are
    # the boot list, not additions.
    booting = True
    for creates, modifies, deletions in filewatch.watch(config.videos_dir, is_ignored):
        # Deletions first so a rename (delete + create sharing a stream_name)
        # frees the slot before the new file tries to claim it.
        for filename in deletions:
            streams.handle_delete(config.videos_dir / filename)
        for filename in creates:
            streams.handle_create(config.videos_dir / filename, added=not booting)
        if creates:
            booting = False
        for filename in modifies:
            streams.handle_modify(config.videos_dir / filename)

        streams.refresh_output_reachable()
        streams.recover_udp_outputs()
        # Every tick: a poll() per process is cheap, and it keeps the status
        # honest — a dead output is noticed within one poll interval.
        streams.cleanup_dead_processes()


if __name__ == "__main__":
    main()
