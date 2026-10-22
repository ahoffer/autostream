"""Coordinate stream operations, file events, and process supervision.

Each manager owns an independent registry and reachability cache. Configuration,
models, persistence, encoding decisions, process I/O, and status rendering live
in separate modules; neither HTTP routing nor the polling loop belongs here.
"""
from __future__ import annotations

import logging
import socket
import time
from pathlib import Path

import ffmpegcmd
import ffmpegprocess
import streamstatus
from streammodel import stream_name_for
from streampolicy import encode_spec, udp_launchable
from streamregistry import StreamRegistry

log = logging.getLogger(__name__)

KEEP = object()   # update_settings argument: leave this setting as it is
STABLE_AFTER_SEC = 60   # a relaunched output that lasts this long is no longer "crashing"


class StreamManager:
    def __init__(self, config):
        self.config = config
        self.registry = StreamRegistry(config)
        self.output_reachable = False
        self._output_host_warned = False

    def load_persisted_state(self):
        self.registry.load_persisted_state()

    def get_stream_status(self):
        with self.registry.lock:
            return streamstatus.get_stream_status(self.registry.streams.values(), self.output_reachable)

    def refresh_output_reachable(self):
        """Refresh the cached OUTPUT_HOST reachability.

        ffmpeg aborts if the UDP destination host is unresolvable. So when the
        consumer (for example the cx-search video-streaming service) is not on
        the network, the udp output is held back rather than launched to die: it
        stays enabled and recover_udp_outputs launches it once the host resolves.
        A numeric IP or multicast group always resolves, so this only trips on a
        missing hostname.

        gethostbyname blocks for the resolver timeout when the host is down, so it
        must stay off the request path: status reads and launches sample the
        cache, and the poll loop refreshes it every tick.
        """
        try:
            socket.gethostbyname(self.config.output_host)
            self._output_host_warned = False
            self.output_reachable = True
        except OSError:
            if not self._output_host_warned:
                log.warning("OUTPUT_HOST %r does not resolve, rtsp/hls only until reachable",
                            self.config.output_host)
                self._output_host_warned = True
            self.output_reachable = False

    def launch_output(self, stream_name, output_name, log_start=True):
        """Start one output's ffmpeg process. Does not change enabled."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                log.warning("Cannot start unclaimed stream: %s", stream_name)
                return False
            output = stream.output(output_name)
            if not output.enabled:
                return False
            if output.occupied:
                log.warning("Output already %s: %s/%s",
                            "stopping" if output.stopping else "running", stream_name, output_name)
                return False
            if output_name == "udp":
                launchable, reason = udp_launchable(stream, self.output_reachable)
                if not launchable:
                    log.debug("Holding %s/udp back: %s", stream_name, reason)
                    return False
            if stream.profile is not None and not stream.profile.video_codecs:
                # Probed and found no video track (or unreadable: not a video, or
                # still being copied) — ffmpeg would only die at once. Say so and
                # wait for the file to change.
                output.last_error = "no video track found: not a video file, or still being copied"
                log.warning("%s/%s cannot start: %s", stream_name, output_name, output.last_error)
                return False
            spec = encode_spec(stream)
            argv = ffmpegcmd.build_argv(spec, output_name)
            try:
                process = ffmpegprocess.launch_output(argv, stream_name, output_name, self.config)
            except Exception as error:
                output.last_error = f"could not start ffmpeg: {error}"
                log.error("%s/%s cannot start: %s", stream_name, output_name, error)
                return False
            output.mark_started(process, argv)
            rtsp_url = stream.urls()["rtsp"]

        # The rtsp output coming up is the moment the stream is watchable, so
        # that is the one line a stream gets: the URL to open.
        if log_start and output_name == "rtsp":
            log.info("%s", rtsp_url)
        else:
            log.debug("Started %s/%s", stream_name, output_name)
        return True

    def stop_output(self, stream_name, output_name):
        """Stop one output's process. Does not change enabled."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            output = stream.output(output_name) if stream is not None else None
            if output is None or not output.occupied:
                log.warning("Output not running: %s/%s", stream_name, output_name)
                return False
            process = output.process
            should_terminate = not output.stopping
            output.stopping = True

        try:
            if not ffmpegprocess.stop_output(process, stream_name, output_name, should_terminate):
                return True
            with self.registry.lock:
                current = self.registry.streams.get(stream_name)
                if current is not None and current.output(output_name).process is process:
                    current.output(output_name).clear_process()
            log.debug("Stopped %s/%s", stream_name, output_name)
        except Exception as error:
            log.error("Error stopping %s/%s: %s", stream_name, output_name, error)

        return True

    def _relaunch_output(self, stream_name, output_name, log_start=True):
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            occupied = stream is not None and stream.output(output_name).occupied
        if occupied:
            self.stop_output(stream_name, output_name)
        return self.launch_output(stream_name, output_name, log_start)

    def refresh_stream_probe(self, stream_name):
        """Re-probe the file. Runs ffprobe outside the lock
        so other threads don't stall on I/O."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                return
            video_path = stream.video_path
        profile = ffmpegcmd.probe_file(video_path)
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is not None:
                stream.profile = profile

    def update_settings(self, stream_name, loop_count=KEEP, mode=KEEP, gop_frames=KEEP, frame_rate=KEEP):
        """Store new settings for a stream and apply them to whatever is running.

        mode None means the codec-derived default, frame_rate None the file's
        own rate, and gop_frames None one second at the output rate. A running
        output is relaunched so the process always matches the stored settings;
        a stopped stream just keeps them for its next start.
        """
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                return False
            if loop_count is not KEEP:
                stream.loop_count = loop_count
            if mode is not KEEP:
                stream.mode = mode
            if gop_frames is not KEEP:
                stream.gop_frames = gop_frames
            if frame_rate is not KEEP:
                stream.frame_rate = frame_rate
            for output in stream.outputs:
                output.reset_health()
            self.registry.persist()
            running = [output.name for output in stream.outputs if output.running]
            log.info("%s settings changed (mode=%s loop=%s fps=%s gop=%s)%s", stream_name,
                     stream.mode, stream.loop_count, stream.frame_rate, stream.gop_frames,
                     ", restarted" if running else "")
        if running:
            self.refresh_stream_probe(stream_name)
        for output_name in running:
            self._relaunch_output(stream_name, output_name, log_start=False)
        return True

    def start_stream(self, stream_name):
        """(Re)start a stream with its stored settings.

        A fully stopped stream comes up with both outputs. One that is up keeps
        its per-output choices: Restart relaunches the enabled outputs and leaves
        a deliberately stopped one down.
        """
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                log.warning("Cannot start unclaimed stream: %s", stream_name)
                return False
            if not any(output.enabled for output in stream.outputs):
                for output in stream.outputs:
                    output.enabled = True
            for output in stream.outputs:
                output.completed = False
                output.reset_health()
            self.registry.persist()
            enabled = [output.name for output in stream.outputs if output.enabled]
        self.refresh_stream_probe(stream_name)
        launched = [self._relaunch_output(stream_name, output_name) for output_name in enabled]
        return any(launched)

    def stop_stream(self, stream_name):
        """Stop every output of a stream and disable both."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                log.warning("Stream not found: %s", stream_name)
                return False
            for output in stream.outputs:
                output.enabled = False
            self.registry.persist()
            occupied = [output.name for output in stream.outputs if output.occupied]
        for output_name in occupied:
            self.stop_output(stream_name, output_name)
        if occupied:
            log.info("%s stopped", stream_name)
        return True

    def start_output(self, stream_name, output_name):
        """(Re)start one output and mark it enabled."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                return False
            output = stream.output(output_name)
            output.enabled = True
            output.completed = False
            output.reset_health()
            self.registry.persist()
        self.refresh_stream_probe(stream_name)
        if self._relaunch_output(stream_name, output_name):
            return True
        # A udp output held back (consumer unreachable) is not a failure: the
        # request was recorded and recover_udp_outputs launches it later.
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            return (output_name == "udp" and stream is not None
                    and stream.udp.enabled
                    and not udp_launchable(stream, self.output_reachable)[0])

    def disable_output(self, stream_name, output_name):
        """Stop one output and mark it disabled, so supervision leaves it down."""
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                return False
            output = stream.output(output_name)
            output.enabled = False
            self.registry.persist()
            occupied = output.occupied
        if occupied:
            self.stop_output(stream_name, output_name)
            log.info("%s/%s stopped", stream_name, output_name)
        return True

    def launch_enabled_outputs(self, stream_name, log_start=True):
        """Bring up whatever the stream's outputs say should be up."""
        self.refresh_stream_probe(stream_name)
        with self.registry.lock:
            stream = self.registry.streams.get(stream_name)
            if stream is None:
                return
            pending = [output.name for output in stream.outputs
                       if output.enabled and not output.occupied and not output.completed]
        for output_name in pending:
            self.launch_output(stream_name, output_name, log_start)

    def stream_exists(self, stream_name):
        """True if a slot is registered under this name."""
        with self.registry.lock:
            return stream_name in self.registry.streams

    def stop_all_streams(self):
        with self.registry.lock:
            names = list(self.registry.streams)
        for name in names:
            self.stop_stream(name)

    def start_all_streams(self):
        """Enable every output and bring up the ones that are down. Running
        outputs are left alone, so viewers are not dropped."""
        with self.registry.lock:
            for stream in self.registry.streams.values():
                for output in stream.outputs:
                    output.enabled = True
                    output.completed = False
            self.registry.persist()
            names = list(self.registry.streams)
        for name in names:
            self.launch_enabled_outputs(name)

    def handle_create(self, filepath, added=True):
        """Claim a slot for a file and bring it up. added is False for the files
        found at boot, which are listed by URL rather than announced."""
        path = Path(filepath)
        stream_name = stream_name_for(path)
        with self.registry.lock:
            if not self.registry.claim_slot(stream_name, path.name):
                return
            rtsp_url = self.registry.streams[stream_name].urls()["rtsp"]
        if added:
            log.info("Added %s  %s", path.name, rtsp_url)
        self.launch_enabled_outputs(stream_name, log_start=not added)

    def handle_delete(self, filepath):
        path = Path(filepath)
        with self.registry.lock:
            # Match the owning filename so deleting a file refused for a name
            # collision cannot remove another file's slot.
            stream = next((s for s in self.registry.streams.values() if s.filename == path.name), None)
            if stream is None:
                log.debug("Ignoring delete of %s; it owned no slot", path.name)
                return
            stream_name = stream.name
            occupied = [output.name for output in stream.outputs if output.occupied]

        log.info("Removed %s", path.name)
        for output_name in occupied:
            self.stop_output(stream_name, output_name)
        with self.registry.lock:
            stream = self.registry.streams.pop(stream_name, None)
            if stream is not None:
                self.registry.retain_removed(stream, removed_at=time.time())

    def handle_modify(self, filepath):
        path = Path(filepath)
        stream_name = stream_name_for(path)
        with self.registry.lock:
            if not self.registry.claim_slot(stream_name, path.name):
                return
            stream = self.registry.streams[stream_name]
            running = [output.name for output in stream.outputs if output.running]
            for output in stream.outputs:
                # Completion belongs to the old file; preserve explicit Stop choices.
                output.completed = False
                output.reset_health()

        log.info("Updated %s%s", path.name, ", restarted" if running else "")
        # Re-probe, relaunch what is running, and bring up enabled outputs that
        # were held back (for example a file that was still being copied).
        self.launch_enabled_outputs(stream_name, log_start=False)
        for output_name in running:
            self._relaunch_output(stream_name, output_name, log_start=False)

    def cleanup_dead_processes(self):
        now = time.time()
        with self.registry.lock:
            snapshot = [(stream.name, output.name, output.process)
                        for stream in self.registry.streams.values()
                        for output in stream.outputs if output.occupied]
            for stream in self.registry.streams.values():
                for output in stream.outputs:
                    if (output.running and output.crashes
                            and now - (output.started_at or now) > STABLE_AFTER_SEC):
                        log.info("%s/%s stable", stream.name, output.name)
                        output.reset_health()

        dead = [(stream_name, output_name, process)
                for stream_name, output_name, process in snapshot if process.poll() is not None]

        for stream_name, output_name, process in dead:
            with self.registry.lock:
                current = self.registry.streams.get(stream_name)
                output = current.output(output_name) if current is not None else None
                # If a concurrent start replaced our entry with a fresh process,
                # don't orphan it — leave the new process alone.
                if output is None or output.process is not process:
                    continue
                stopping = output.stopping
                enabled = output.enabled
                loop_count = current.loop_count
                video_path = current.video_path
                output.clear_process()
                if loop_count != -1 and process.returncode == 0:
                    output.completed = True

            if stopping or not enabled:
                continue

            if loop_count != -1 and process.returncode == 0:
                log.info("%s/%s finished (loop off)", stream_name, output_name)
                continue

            reason, error = ffmpegprocess.ffmpeg_death_reason(stream_name, output_name, self.config)
            crashes = 0
            with self.registry.lock:
                current = self.registry.streams.get(stream_name)
                if current is not None:
                    output = current.output(output_name)
                    output.crashes += 1
                    crashes = output.crashes
                    output.last_error = f"ffmpeg exited with code {process.returncode}\n{error}"
            crashed = f"{stream_name}/{output_name} crashed (rc={process.returncode})"

            try:
                if not Path(video_path).exists():
                    log.warning("%s, not restarted: file missing", crashed)
                    continue
            except OSError as error:
                log.warning("%s, not restarted: %s", crashed, error)
                continue
            if not self.launch_output(stream_name, output_name, log_start=False):
                log.error("%s, not restarted", crashed)
                continue
            log.warning("%s, restarted; crash %d: %s", crashed, crashes, reason)

    def recover_udp_outputs(self):
        """Launch udp outputs that were held back, once OUTPUT_HOST resolves.

        Reachability is sampled at launch, so an enabled udp output that was
        skipped while the consumer was off the network stays down until this
        brings it up. Slots with no port from the range are skipped: launching
        them would never succeed.
        """
        if not self.output_reachable:
            return
        with self.registry.lock:
            pending = [stream.name for stream in self.registry.streams.values()
                       if stream.udp_port is not None and stream.udp.enabled
                       and not stream.udp.occupied and not stream.udp.completed]
        for name in pending:
            self.launch_output(name, "udp")
