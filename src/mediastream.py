"""One stream's behavior: a video delivered over one transport, called exclusively by the supervisor thread."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import logging
from pathlib import Path
import shlex
import time

import ffmpegcmd
from ffmpegprocess import FFmpegProcess

log = logging.getLogger(__name__)

# The user's choices and their defaults in one place: this mapping is also the
# allowlist of keys that come back from the state file, so retired keys drop out.
DEFAULT_CHOICES = {'enabled': True, 'mode': None, 'loop_count': -1, 'frame_rate': None, 'gop_frames': None}
USER_SETTINGS = ('mode', 'loop_count', 'gop_frames', 'frame_rate')


@dataclass(frozen=True)
class StreamBinding:
    """Which file a stream plays and which transport and port carry it; saved beside the choices."""
    filename: str
    transport: str
    port: int | None


@dataclass(frozen=True)
class Delivery:
    """How ffmpeg delivers one stream; when url is None, blocker says what the registry still lacks."""
    url: str | None
    muxer: str = 'mpegts'
    carries_klv: bool = True
    extra_args: tuple = ()
    resolve_host: str | None = None   # a name that must resolve before pushing
    client_bound: bool = False        # ffmpeg serves one client and exits when it leaves
    blocker: str | None = None


# A listener that has run this long was serving a client; its exit is the client leaving, not a crash.
CLIENT_SERVED_SECONDS = 5


class MediaStream:
    def __init__(self, name, source_path, binding, choices, delivery, *, max_video_bps, store, resolver, logger):
        self._name = name
        self._source = Path(source_path)
        self._binding = binding
        self._choices = deepcopy(choices)
        self._delivery = delivery
        self._max_video_bps = max_video_bps
        self._store = store
        self._resolver = resolver
        self._logger = logger
        self._run = None
        self._completed = False
        self._crashes = 0
        self._error = None
        self._prepared = {}

    @property
    def binding(self):
        return self._binding

    @property
    def delivery(self):
        return self._delivery

    def _save(self, choices):
        self._store.save_stream(self._name, {**asdict(self._binding), **choices})

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
        return self.rebind(self._source, self._binding, self._delivery, changes)

    def rebind(self, source_path, binding, delivery, changes):
        """Play another file or use another transport, with any setting changes, in one restart."""
        if set(changes) - set(USER_SETTINGS):
            raise ValueError('Unknown stream setting')
        self._source = Path(source_path)
        self._binding = binding
        self._delivery = delivery
        choices = {**self._choices, **changes}
        self._save(choices)
        self._choices = choices
        self._reset_health()
        if self._choices['enabled']:
            return self._restart()
        return self._refresh_preview()

    def rename(self, name, delivery, logger):
        """Carry a new name and the delivery derived from it; the saved record moves with it.
        The caller decides whether the process restarts."""
        self._store.rename_stream(self._name, name)
        self._name, self._delivery, self._logger = name, delivery, logger

    def settings_changed(self, delivery, max_video_bps):
        """Registry-owned facts changed: restart only when the command they produce differs."""
        self._delivery, self._max_video_bps = delivery, max_video_bps
        if self._blocker():
            self._prepared = {}
            return self._stop_run() if self._choices['enabled'] else True
        try:
            prepared = self._prepare()
        except (OSError, ValueError) as error:
            self._error = str(error)
            return False
        observation = self._run.observe() if self._run else None
        running_same_command = bool(observation and observation['returncode'] is None
                                    and tuple(prepared['argv']) == tuple(observation['argv']))
        if not self._choices['enabled'] or running_same_command:
            self._prepared = prepared
            return True
        self._reset_health()
        return self._restart(prepared)

    def _refresh_preview(self):
        """Show what would run without starting a stopped stream."""
        if self._blocker():
            self._prepared = {}
            return True
        self._prepared = self._prepare()
        return True

    def _prepare(self):
        return ffmpegcmd.prepare(self._source, self._choices, self._delivery, self._max_video_bps)

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
        return self._refresh_preview()

    def retire(self):
        """Stop without changing saved intent; the registry owns membership."""
        return self._stop_run()

    def _blocker(self):
        if not self._source.is_file():
            return 'source file missing'
        if self._delivery.url is None:
            return self._delivery.blocker
        if self._delivery.resolve_host:
            return self._resolver.start_blocker(self._delivery.resolve_host)
        return None

    def _restart(self, prepared=None):
        try:
            if self._blocker():
                # A missing file or destination is accepted intent: the next
                # supervision tick starts the stream once it clears.
                self._prepared = {}
                return self._stop_run()
            if prepared is None:
                prepared = self._prepare()
            if prepared['error']:
                if self._run is None:
                    self._prepared = prepared
                self._error = prepared['error']
                return False
            if not self._stop_run():
                return False
            self._prepared = prepared
            if self._delivery.muxer == 'hls':
                Path(self._delivery.url).parent.mkdir(parents=True, exist_ok=True)
            run = FFmpegProcess(self._logger)
            run.start(prepared['argv'])
            self._run = run
            return True
        except (OSError, ValueError) as error:
            self._error = str(error)
            log.error('Cannot start %s: %s', self._name, error)
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
            elif self._delivery.client_bound and time.monotonic() - observation['started_at'] >= CLIENT_SERVED_SECONDS:
                self._error = None   # the client left; listen again
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
        return {**prepared, **self._choices, **asdict(self._binding),
                'mode': self._choices['mode'] or prepared.get('mode', 'transcode'),
                'frame_rate': self._choices['frame_rate'] or source_rate,
                'frame_rate_explicit': self._choices['frame_rate'] is not None,
                'gop_frames': self._choices['gop_frames'] or ffmpegcmd.natural_gop_frames(self._choices['frame_rate'] or source_rate),
                'gop_explicit': self._choices['gop_frames'] is not None,
                'name': self._name,
                'state': state, 'reason': reason, 'running': running,
                'stopping': running and not self._choices['enabled'],
                'command': shlex.join(argv) if argv else None,
                'error': self._error, 'delivery': self._delivery.url}

    def _state(self, running):
        if not self._choices['enabled']:
            return ('stopping', 'waiting for process exit') if running else ('stopped', 'stopped')
        if running:
            return ('crashing', f'died {self._crashes}x; relaunched, watching') if self._crashes else ('running', self._error)
        if self._completed:
            return 'completed', 'played to the end'
        blocker = self._blocker()
        if blocker:
            return 'waiting', blocker
        if self._crashes:
            return 'restarting', f'died {self._crashes}x; relaunching'
        if self._error:
            return 'failed', self._error
        return 'starting', 'starting'
