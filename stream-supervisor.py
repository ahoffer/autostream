#!/usr/bin/env python3
"""Autostream supervisor: turn the video files in /app/videos into live streams.

Wires the pieces together and runs the loop that drives them: filewatch reports
file changes, streams owns the slots and their ffmpeg processes, and streamapi
serves the control UI alongside.
"""
from __future__ import annotations

import logging
import threading
import time

import filewatch
import streamapi
import streams


def main():
    logging.basicConfig(
        level=streams.LOG_LEVEL.upper(),
        format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)
    log.info("Stream supervisor starting...")

    streams.wait_for_mediamtx()
    streams.refresh_output_reachable()

    threading.Thread(target=streamapi.serve, args=(streams.API_PORT,),
                     name="APIServer", daemon=True).start()

    last_cleanup = time.time()
    for creates, modifies, deletions in filewatch.watch(streams.VIDEOS_DIR, streams.is_ignored):
        # Deletions first so a rename (delete + create sharing a stream_name)
        # frees the slot before the new file tries to claim it.
        for filename in deletions:
            streams.handle_delete(streams.VIDEOS_DIR / filename)
        for filename in creates:
            streams.handle_create(streams.VIDEOS_DIR / filename)
        for filename in modifies:
            streams.handle_modify(streams.VIDEOS_DIR / filename)

        streams.refresh_output_reachable()
        streams.recover_udp_outputs()

        if time.time() - last_cleanup > 30:
            streams.cleanup_dead_processes()
            last_cleanup = time.time()


if __name__ == "__main__":
    main()
