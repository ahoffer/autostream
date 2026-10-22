"""Deterministic process and DNS collaborators for behavioral tests."""
import logging
from pathlib import Path
import shutil
import tempfile
import time
from unittest.mock import patch

import ffmpegcmd
from outputregistry import OutputRegistry
from runtimesettings import RuntimeSettings
from statefile import StreamStateStore
from testsupport import make_config, state_path

TEST_SETTINGS = RuntimeSettings(destination_host='receiver', udp_base_port=40000, udp_last_port=40100,
                                max_video_bitrate='2M')


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


def inventory(*paths, deletes=(), modifies=(), unstable=()):
    names = tuple(path.name for path in paths)
    return {'creates': (), 'modifies': tuple(path.name for path in modifies),
            'deletes': tuple(path.name for path in deletes), 'present': names + tuple(path.name for path in unstable),
            'stable': names}


def setup_outputs(test, settings=TEST_SETTINGS, **config_changes):
    root = Path(tempfile.mkdtemp(prefix='autostream-videos-'))
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    with tempfile.NamedTemporaryFile(dir=root, suffix='.mp4', delete=False) as file:
        source = Path(file.name)
    test.addCleanup(source.unlink, missing_ok=True)
    config = make_config(videos_dir=root, state_path=state_path(test), **config_changes)
    store = StreamStateStore(config.state_path)
    store.save_settings(settings.to_document())
    resolver = FakeResolver()
    registry = OutputRegistry(config, store, resolver, lambda name: logging.Logger(name))
    registry.load_persisted_state()
    FakeProcess.runs = []
    patcher = patch('mediaoutput.FFmpegProcess', FakeProcess)
    patcher.start()
    test.addCleanup(patcher.stop)
    profile = ffmpegcmd.MediaProbeResult(('h264',), ('aac',), True, 'yuv420p', 4000000, 30.)
    patcher = patch('ffmpegcmd.probe_file', return_value=profile)
    patcher.start()
    test.addCleanup(patcher.stop)
    registry.supervise(inventory(source))
    return registry, store, resolver, source
