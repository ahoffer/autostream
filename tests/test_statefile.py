import json
import unittest
from unittest.mock import patch

from statefile import StreamStateStore
from testsupport import state_path


class PersistenceTests(unittest.TestCase):
    def test_migration_preserves_independent_enablement_settings_ports_and_backup(self):
        path = state_path(self)
        backup = path.with_suffix('.json.v1.bak')
        self.addCleanup(backup.unlink, missing_ok=True)
        old = {'version': 1, 'udp_ports': {'boats': 40005}, 'streams': {'boats': {
            'filename': 'boats.mp4', 'mode': 'transcode', 'loop_count': 2,
            'frame_rate': 12, 'gop_frames': 24, 'removed_at': 123,
            'outputs': {'rtsp': {'enabled': False}, 'udp': {'enabled': True}}}}}
        raw = json.dumps(old)
        path.write_text(raw)
        store = StreamStateStore(path)
        migrated = store.load()
        self.assertEqual(backup.read_text(), raw)
        self.assertEqual(migrated['assignments']['boats'], {'filename': 'boats.mp4', 'udp_port': 40005, 'removed_at': 123})
        rtsp, udp = migrated['outputs']['boats/rtsp'], migrated['outputs']['boats/udp']
        self.assertFalse(rtsp['enabled'])
        self.assertTrue(udp['enabled'])
        self.assertEqual(rtsp['frame_rate'], 12)
        rtsp.update(frame_rate=15)
        store.save_output('boats/rtsp', rtsp)
        reloaded = StreamStateStore(path).load()
        self.assertEqual(reloaded['outputs']['boats/rtsp']['frame_rate'], 15)
        self.assertEqual(reloaded['outputs']['boats/udp']['frame_rate'], 12)

    def test_sequential_scoped_saves_preserve_other_outputs_and_assignments(self):
        path = state_path(self)
        store = StreamStateStore(path)
        choices = {'filename': 'boats.mp4', 'enabled': True, 'mode': None, 'loop_count': -1}
        store.save_output('boats/rtsp', choices)
        store.save_output('boats/udp', choices)
        store.save_assignments({'boats': {'filename': 'boats.mp4', 'udp_port': 40000, 'removed_at': None}})
        store.save_output('boats/rtsp', {**choices, 'enabled': False})
        reloaded = StreamStateStore(path).load()
        self.assertFalse(reloaded['outputs']['boats/rtsp']['enabled'])
        self.assertTrue(reloaded['outputs']['boats/udp']['enabled'])
        self.assertEqual(reloaded['assignments']['boats']['udp_port'], 40000)

    def test_failed_replace_leaves_previous_document_retryable(self):
        path = state_path(self)
        store = StreamStateStore(path)
        store.save_output('a/rtsp', {'enabled': True})
        before = path.read_text()
        with patch('statefile.os.replace', side_effect=OSError('full')), self.assertRaises(OSError):
            store.save_output('a/rtsp', {'enabled': False})
        self.assertEqual(path.read_text(), before)
        self.assertTrue(store.save_output('a/rtsp', {'enabled': False}))

    def test_unknown_version_is_not_silently_overwritten(self):
        path = state_path(self)
        path.write_text('{"version": 99}')
        with self.assertRaises(ValueError):
            StreamStateStore(path).load()
        self.assertEqual(json.loads(path.read_text())['version'], 99)

    def test_invalid_migration_does_not_replace_original(self):
        path = state_path(self)
        raw = json.dumps({'version': 1, 'streams': {'bad': {'filename': 'bad.mp4', 'loop_count': -3}}})
        path.write_text(raw)
        with self.assertRaises(ValueError):
            StreamStateStore(path).load()
        self.assertEqual(path.read_text(), raw)
