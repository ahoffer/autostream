#!/usr/bin/env python3
"""Autostream supervisor: turn the video files in /app/videos into live streams.

Wires the pieces together and runs the loop that drives them: filewatch reports
file changes, streams owns the slots and their ffmpeg processes, and streamapi
serves the control UI alongside.
"""
from __future__ import annotations

import logging
import threading

import filewatch
import streamapi
import streams


class OneLineFormatter(logging.Formatter):
    """Keep every record on one line so the log stays scannable: a multi-line
    argument (an exception text, an ffmpeg tail) is joined with "; "."""

    def format(self, record):
        return super().format(record).replace("\n", "; ")


def configure_logging():
    logging.addLevelName(logging.WARNING, "WARN")
    handler = logging.StreamHandler()
    handler.setFormatter(OneLineFormatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=streams.LOG_LEVEL.upper(), handlers=[handler])


def main():
    configure_logging()
    log = logging.getLogger(__name__)
    log.info("Supervisor starting, UI http://localhost:%d", streams.API_PORT)

    streams.wait_for_mediamtx()
    # Before the first poll: its creates claim slots, which assign UDP ports
    # and adopt remembered settings.
    streams.load_persisted_state()
    streams.refresh_encoder_tier()
    streams.refresh_output_reachable()

    threading.Thread(target=streamapi.serve, args=(streams.API_PORT,),
                     name="APIServer", daemon=True).start()

    # The files already present arrive together in the first poll that reports
    # creates (the watcher holds them for its quiet period first); those are
    # the boot list, not additions.
    booting = True
    for creates, modifies, deletions in filewatch.watch(streams.VIDEOS_DIR, streams.is_ignored):
        # Deletions first so a rename (delete + create sharing a stream_name)
        # frees the slot before the new file tries to claim it.
        for filename in deletions:
            streams.handle_delete(streams.VIDEOS_DIR / filename)
        for filename in creates:
            streams.handle_create(streams.VIDEOS_DIR / filename, added=not booting)
        if creates:
            booting = False
        for filename in modifies:
            streams.handle_modify(streams.VIDEOS_DIR / filename)

        streams.refresh_output_reachable()
        streams.recover_udp_outputs()
        # Every tick: a poll() per process is cheap, and it keeps the status
        # honest — a dead output is noticed within one poll interval.
        streams.cleanup_dead_processes()


if __name__ == "__main__":
    main()
