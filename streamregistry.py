"""Slot ownership, retained settings, and stable UDP port allocation.

The registry owns the lock shared with StreamManager. Callers hold it while
reading or mutating streams, allocating ports, or saving a snapshot. Process
control and probing belong to the manager and run outside this module.
"""
import logging
import threading

import statefile
from streammodel import Stream

log = logging.getLogger(__name__)


def _persisted_stream(stream, removed_at=None):
    return statefile.PersistedStream(
        filename=stream.filename, mode=stream.mode, loop_count=stream.loop_count,
        gop_frames=stream.gop_frames, frame_rate=stream.frame_rate, removed_at=removed_at,
        rtsp=statefile.PersistedOutput(stream.rtsp.enabled),
        udp=statefile.PersistedOutput(stream.udp.enabled),
    )


class StreamRegistry:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.streams = {}
        self.retained_streams = {}
        self.udp_ports = {}
        self.next_udp_port = config.udp_base_port

    def load_persisted_state(self):
        """Seed the registry's memory from the state file. Call once, before the
        first file events: they claim slots, which assigns ports."""
        state = statefile.load_state(self.config.state_path)
        with self.lock:
            self.udp_ports.update({name: port for name, port in state.udp_ports.items() if name})
            self.retained_streams.update({name: record for name, record in state.streams.items() if name})
            if self.udp_ports:
                self.next_udp_port = max(self.config.udp_base_port, max(self.udp_ports.values()) + 1)
        if state.streams:
            log.debug("Loaded settings for %d stream(s) from %s", len(state.streams), self.config.state_path)

    def persist(self):
        """Write the state file. Must be called while holding self.lock."""
        records = dict(self.retained_streams)
        for stream in self.streams.values():
            records[stream.name] = _persisted_stream(stream)
        statefile.save_state(self.config.state_path, statefile.PersistedState(dict(self.udp_ports), records))

    def udp_port_for(self, stream_name):
        """Return the stream's UDP port, assigning the next free one on first use.

        The range is a contract with the downstream consumer, so a port stays
        with its stream name — including after the file is removed — for as long
        as the range has room. Only when it is used up is the port of the
        longest-removed file reclaimed (with its remembered settings), so churn
        of temporary files cannot exhaust the range while a removed-and-restored
        file normally keeps its udp:// URL. Returns None when every port belongs
        to a present file; the caller streams RTSP/HLS only.
        Must be called while holding self.lock.
        """
        port = self.udp_ports.get(stream_name)
        if port is not None:
            return port
        if self.next_udp_port <= self.config.udp_last_port:
            port = self.next_udp_port
            self.next_udp_port += 1
        else:
            # Saved files may still be waiting for discovery at startup. Their
            # ports remain reserved even before they have an active slot.
            try:
                present_filenames = {path.name for path in self.config.videos_dir.iterdir()}
            except OSError as error:
                log.warning("Cannot check which UDP ports can be reclaimed: %s", error)
                return None
            removed = [(record.removed_at or 0, name) for name, record in self.retained_streams.items()
                       if name in self.udp_ports and name not in self.streams
                       and record.filename not in present_filenames]
            if not removed:
                log.warning("UDP port range %d-%d is used up; %s streams RTSP/HLS only",
                            self.config.udp_base_port, self.config.udp_last_port, stream_name)
                return None
            _, oldest = min(removed)
            port = self.udp_ports.pop(oldest)
            del self.retained_streams[oldest]
            log.info("UDP port %d moves from removed %s to %s", port, oldest, stream_name)
        self.udp_ports[stream_name] = port
        return port

    def claim_slot(self, stream_name, filename):
        """Register (or refresh) the slot binding stream_name to filename.

        A new slot adopts the remembered settings when they belong to the same
        filename. Returns True if the slot belongs to this filename after the
        call, False if the name is invalid or belongs to a different file (caller
        should skip). Must be called under self.lock.
        """
        if not stream_name:
            log.warning("Skipping invalid video filename: %r", filename)
            return False
        stream = self.streams.get(stream_name)
        if stream is None:
            stream = Stream(name=stream_name, filename=filename, udp_port=self.udp_port_for(stream_name),
                            config=self.config)
            remembered = self.retained_streams.get(stream_name)
            if remembered is not None and remembered.filename == filename:
                del self.retained_streams[stream_name]
                stream.mode = remembered.mode
                stream.loop_count = remembered.loop_count
                stream.gop_frames = remembered.gop_frames
                stream.frame_rate = remembered.frame_rate
                stream.rtsp.enabled = remembered.rtsp.enabled
                stream.udp.enabled = remembered.udp.enabled
            self.streams[stream_name] = stream
            self.persist()
            return True
        if stream.filename != filename:
            log.warning("Stream name collision: %s already owned by %r; skipping %r "
                        "(after removing the owner, touch this file or restart to stream it)",
                        stream_name, stream.filename, filename)
            return False
        return True

    def retain_removed(self, stream, removed_at):
        """Remember a removed slot's choices. Caller holds lock."""
        self.retained_streams[stream.name] = _persisted_stream(stream, removed_at)
        self.persist()
