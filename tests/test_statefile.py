import json
import unittest
from unittest.mock import patch

from runtimesettings import RuntimeSettings
from statefile import StreamStateStore
from testsupport import state_path


class PersistenceTests(unittest.TestCase):
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

    def test_document_without_settings_loads_and_save_settings_adds_them_beside_the_rest(self):
        path = state_path(self)
        path.write_text(json.dumps({'version': 2, 'assignments': {}, 'outputs': {}}))
        store = StreamStateStore(path)
        self.assertNotIn('settings', store.load())
        store.save_output('a/rtsp', {'filename': 'a.mp4', 'enabled': True, 'mode': None, 'loop_count': -1})
        self.assertTrue(store.save_settings(RuntimeSettings(destination_host='receiver').to_document()))
        reloaded = StreamStateStore(path).load()
        self.assertEqual(RuntimeSettings.from_document(reloaded['settings']),
                         RuntimeSettings(destination_host='receiver'))
        self.assertIn('a/rtsp', reloaded['outputs'])

    def test_invalid_settings_section_is_rejected_without_rewrite(self):
        path = state_path(self)
        raw = json.dumps({'version': 2, 'assignments': {}, 'outputs': {},
                          'settings': {'udp_base_port': 5, 'udp_last_port': 1}})
        path.write_text(raw)
        with self.assertRaisesRegex(ValueError, 'below'):
            StreamStateStore(path).load()
        self.assertEqual(path.read_text(), raw)

    def test_old_output_records_with_retired_keys_still_load(self):
        path = state_path(self)
        record = {'filename': 'a.mp4', 'enabled': True, 'mode': None, 'loop_count': -1,
                  'udp_host': 'old-receiver', 'max_video_bps': 2000000}
        path.write_text(json.dumps({'version': 2, 'assignments': {}, 'outputs': {'a/udp': record}}))
        self.assertEqual(StreamStateStore(path).load()['outputs']['a/udp']['udp_host'], 'old-receiver')

    def test_unknown_version_is_not_silently_overwritten(self):
        path = state_path(self)
        path.write_text('{"version": 99}')
        with self.assertRaises(ValueError):
            StreamStateStore(path).load()
        self.assertEqual(json.loads(path.read_text())['version'], 99)

