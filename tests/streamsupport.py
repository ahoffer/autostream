"""Deterministic process and DNS collaborators for behavioral tests."""
import logging
from pathlib import Path
import shutil
import tempfile
import time
from unittest.mock import patch

import ffmpegcmd
from runtimesettings import RuntimeSettings
from statefile import StreamStateStore
from streamregistry import StreamRegistry
from testsupport import make_config, state_path

TEST_SETTINGS = RuntimeSettings(destination_host='receiver', multicast_group='239.1.1.2',
                                srtp_key='c2VjcmV0a2V5', max_video_bitrate='2M')
TEST_PROFILE = ffmpegcmd.MediaProbeResult(('h264',), ('aac',), True, 'yuv420p', 4000000, 30., ('klv',))


class FakeProcess:
    runs = []

    def __init__(self, logger):
        self.argv = ()
        self.code = None
        self.started = None
        self.stuck = False
        self.tail = ''
        self.runs.append(self)

    def start(self, argv):
        self.argv = tuple(argv)
        self.started = time.monotonic()

    def observe(self):
        return {'returncode': self.code, 'argv': self.argv, 'started_at': self.started, 'diagnostics': self.tail}

    def stop(self):
        if self.stuck:
            return False
        self.code = -15
        return True

    def finish(self, code=0):
        self.code = code


class FakeResolver:
    blocked = False
    refreshed = frozenset()

    def refresh(self, hosts):
        self.refreshed = frozenset(hosts)

    def start_blocker(self, host):
        return 'DNS unresolved' if self.blocked else None


def inventory(*paths, deletes=(), modifies=(), unstable=(), creates=()):
    names = tuple(path.name for path in paths)
    return {'creates': tuple(path.name for path in creates), 'modifies': tuple(path.name for path in modifies),
            'deletes': tuple(path.name for path in deletes), 'present': names + tuple(path.name for path in unstable),
            'stable': names}


def temporary_directory(test, prefix):
    root = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    return root


def setup_registry(test, settings=TEST_SETTINGS):
    """A registry over one stable video file, fake processes and DNS, and saved settings."""
    root = temporary_directory(test, 'autostream-videos-')
    with tempfile.NamedTemporaryFile(dir=root, suffix='.mp4', delete=False) as file:
        source = Path(file.name)
    config = make_config(videos_dir=root, state_path=state_path(test), hls_dir=temporary_directory(test, 'autostream-hls-'))
    store = StreamStateStore(config.state_path)
    store.save_settings(settings.to_document())
    resolver = FakeResolver()
    registry = StreamRegistry(config, store, resolver, lambda name: logging.Logger(name))
    registry.load_persisted_state()
    FakeProcess.runs = []
    patcher = patch('mediastream.FFmpegProcess', FakeProcess)
    patcher.start()
    test.addCleanup(patcher.stop)
    patcher = patch('ffmpegcmd.probe_file', return_value=TEST_PROFILE)
    patcher.start()
    test.addCleanup(patcher.stop)
    registry.supervise(inventory(source))
    return registry, store, resolver, source, config


def create(registry, name, source, transport, start=True, **choices):
    created, error = registry.create_stream(name, source.name, transport, choices, start=start)
    assert created, error
    return registry.get_stream(name)
