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


def fake_ffmpeg(test):
    """Fake processes and a fixed probe profile for the rest of the test."""
    FakeProcess.runs = []
    for patcher in (patch('mediastream.FFmpegProcess', FakeProcess), patch('ffmpegcmd.probe_file', return_value=TEST_PROFILE)):
        patcher.start()
        test.addCleanup(patcher.stop)


def test_config(test, root):
    return make_config(videos_dir=root, state_path=state_path(test), hls_dir=temporary_directory(test, 'autostream-hls-'))


def build_registry(config, store):
    resolver = FakeResolver()
    registry = StreamRegistry(config, store, resolver, lambda name: logging.Logger(name))
    registry.load_persisted_state()
    return registry, resolver


def setup_registry(test, settings=TEST_SETTINGS):
    """A registry over one stable video file, fake processes and DNS, and saved settings."""
    root = temporary_directory(test, 'autostream-videos-')
    with tempfile.NamedTemporaryFile(dir=root, suffix='.mp4', delete=False) as file:
        source = Path(file.name)
    config = test_config(test, root)
    store = StreamStateStore(config.state_path)
    store.save_settings(settings.to_document())
    registry, resolver = build_registry(config, store)
    fake_ffmpeg(test)
    registry.supervise(inventory(source))
    return registry, store, resolver, source, config


def setup_fresh_registry(test, filenames):
    """A first start: the named videos are present and no state file was ever written."""
    root = temporary_directory(test, 'autostream-videos-')
    for filename in filenames:
        (root / filename).touch()
    config = test_config(test, root)
    store = StreamStateStore(config.state_path)
    registry, _ = build_registry(config, store)
    fake_ffmpeg(test)
    return registry, store, root


def create(registry, name, source, transport, start=True, **choices):
    created, error = registry.create_stream(name, source.name, transport, choices, start=start)
    assert created, error
    return registry.get_stream(name)
