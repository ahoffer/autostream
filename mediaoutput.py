"""One delivery's behavior, called exclusively by the supervisor thread."""
from copy import deepcopy
import logging
from pathlib import Path
import shlex
import time
from urllib.parse import urlsplit

import ffmpegcmd
from ffmpegprocess import FFmpegProcess

log = logging.getLogger(__name__)


class MediaOutput:
    def __init__(self, output_id, source_path, choices, destination, *, store, resolver, logger):
        self._id = output_id
        self._source = Path(source_path)
        self._choices = deepcopy(choices)
        self._destination = destination
        self._store = store
        self._resolver = resolver
        self._logger = logger
        self._run = None
        self._completed = False
        self._crashes = 0
        self._error = None
        self._prepared = {}
        self._save(self._choices)

    def _save(self, choices):
        self._store.save_output(self._id, {**choices, 'filename': self._source.name})

    def start_streaming(self):
        choices = {**self._choices, 'enabled': True}
        self._save(choices)
        self._choices = choices
        self._reset_health()
        return self._restart()

    def ensure_streaming(self):
        if self._choices['enabled'] and self._run and self._run.observe()['returncode'] is None:
            return True
        return self.start_streaming()

    def stop_streaming(self):
        self._choices['enabled'] = False
        # Even when saving fails, stop now; propagate the failure to the caller.
        try:
            self._save(self._choices)
        finally:
            stopped = self._stop_run()
        return stopped

    def update_settings(self, changes):
        if set(changes) - {'mode', 'loop_count', 'gop_frames', 'frame_rate'}:
            raise ValueError('Unknown output setting')
        choices = {**self._choices, **changes}
        self._save(choices)
        self._choices = choices
        self._reset_health()
        if self._choices['enabled']:
            return self._restart()
        # Refresh the command preview without enabling a stopped output.
        self._prepared = ffmpegcmd.prepare(self._source, self._choices, self._destination)
        return True

    def _reset_health(self):
        self._completed = False
        self._crashes = 0
        self._error = None

    def _stop_run(self):
        if self._run is None:
            return True
        if not self._run.stop():
            self._error = 'Previous process has not exited; destination remains occupied'
            return False
        self._run = None
        return True

    def source_changed(self):
        self._reset_health()
        if self._choices['enabled']:
            return self._restart()
        self._prepared = ffmpegcmd.prepare(self._source, self._choices, self._destination)
        return True

    def retire(self):
        """Stop without changing saved intent; registry owns removed membership."""
        return self._stop_run()

    def _blocker(self):
        if not self._destination:
            return 'no free UDP port'
        if self._destination.startswith('udp:'):
            return self._resolver.start_blocker(urlsplit(self._destination).hostname)
        return None

    def _restart(self):
        if not self._source.is_file():
            self._error = 'Source file is unavailable'
            return False
        try:
            prepared = ffmpegcmd.prepare(self._source, self._choices, self._destination)
            if prepared['error']:
                if self._run is None:
                    self._prepared = prepared
                self._error = prepared['error']
                return False
            if not self._stop_run():
                return False
            self._prepared = prepared
            if self._blocker():
                return bool(self._destination)  # DNS waiting is accepted intent.
            run = FFmpegProcess(self._logger)
            run.start(prepared['argv'])
            self._run = run
            return True
        except (OSError, ValueError) as error:
            self._error = str(error)
            log.error('Cannot start %s: %s', self._id, error)
            return False

    def supervise(self):
        if not self._choices['enabled']:
            self._stop_run()
            return
        if self._run is not None:
            observation = self._run.observe()
            if observation['returncode'] is None:
                if time.monotonic() - observation['started_at'] >= 60:
                    if self._crashes:
                        self._error = None
                    self._crashes = 0
                return
            self._run = None
            argv = observation['argv']
            loops = int(argv[argv.index('-stream_loop') + 1])
            if observation['returncode'] == 0 and loops >= 0:
                self._completed = True
            else:
                self._crashes += 1
                self._error = f'FFmpeg exited {observation["returncode"]}: {observation["diagnostics"]}'
        if not self._completed and not self._blocker() and not self._prepared.get('error'):
            self._restart()

    def status(self):
        observation = self._run.observe() if self._run else None
        running = bool(observation and observation['returncode'] is None)
        state, reason = self._state(running)
        prepared = dict(self._prepared)
        argv = observation['argv'] if observation else prepared.get('argv', ())
        prepared.pop('argv', None)
        # Controls describe requested values, even if an old command still runs.
        source_rate = prepared.get('source_frame_rate')
        return {**prepared, **self._choices,
                'mode': self._choices['mode'] or prepared.get('mode', 'transcode'),
                'frame_rate': self._choices['frame_rate'] or source_rate,
                'frame_rate_explicit': self._choices['frame_rate'] is not None,
                'gop_frames': self._choices['gop_frames'] or ffmpegcmd.natural_gop_frames(self._choices['frame_rate'] or source_rate),
                'gop_explicit': self._choices['gop_frames'] is not None,
                'id': self._id, 'filename': self._source.name,
                'state': state, 'reason': reason, 'running': running,
                'stopping': running and not self._choices['enabled'],
                'command': shlex.join(argv) if argv else None,
                'error': self._error, 'destination': self._destination}

    def _state(self, running):
        if not self._choices['enabled']:
            return ('stopping', 'waiting for process exit') if running else ('stopped', 'stopped')
        if running:
            return ('crashing', f'died {self._crashes}x; relaunched, watching') if self._crashes else ('running', self._error)
        if self._completed:
            return 'completed', 'played to the end'
        blocker = self._blocker()
        if blocker:
            return ('waiting' if self._destination else 'failed'), blocker
        if self._crashes:
            return 'restarting', f'died {self._crashes}x; relaunching'
        if self._error:
            return 'failed', self._error
        return 'starting', 'starting'
