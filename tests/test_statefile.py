import json
import unittest
from unittest.mock import patch

from runtimesettings import RuntimeSettings
from statefile import StreamStateStore
from testsupport import state_path

RECORD = {'filename': 'boats.mp4', 'transport': 'udp', 'port': 40000, 'enabled': True,
          'mode': None, 'loop_count': -1, 'frame_rate': None, 'gop_frames': None}


class PersistenceTests(unittest.TestCase):
    def test_sequential_scoped_saves_preserve_other_streams_and_settings(self):
        path = state_path(self)
        store = StreamStateStore(path)
        store.save_stream('boats', RECORD)
        store.save_settings(RuntimeSettings(destination_host='receiver').to_document())
        store.save_stream('boats-srt', {**RECORD, 'transport': 'srt_listen', 'port': 41000})
        store.save_stream('boats', {**RECORD, 'enabled': False})
        store.delete_stream('boats-srt')
        reloaded = StreamStateStore(path).load()
        self.assertEqual(list(reloaded['streams']), ['boats'])
        self.assertFalse(reloaded['streams']['boats']['enabled'])
        self.assertEqual(RuntimeSettings.from_document(reloaded['settings']).destination_host, 'receiver')

    def test_rename_moves_the_record_and_keeps_its_place(self):
        path = state_path(self)
        store = StreamStateStore(path)
        store.save_stream('boats', RECORD)
        store.save_stream('boats-srt', {**RECORD, 'transport': 'srt_listen', 'port': 41000})
        store.rename_stream('boats', 'ships')
        reloaded = StreamStateStore(path).load()
        self.assertEqual(list(reloaded['streams']), ['ships', 'boats-srt'])
        self.assertEqual(reloaded['streams']['ships']['port'], 40000)

    def test_failed_replace_leaves_previous_document_retryable(self):
        for method, args in (
            ('save_stream', ('boats', {**RECORD, 'enabled': False})),
            ('delete_stream', ('boats',)),
            ('rename_stream', ('boats', 'ships')),
            ('save_settings', (RuntimeSettings(destination_host='receiver').to_document(),)),
        ):
            with self.subTest(method=method):
                path = state_path(self)
                store = StreamStateStore(path)
                store.save_stream('boats', RECORD)
                before = path.read_text()
                cached = store.load()
                operation = getattr(store, method)
                with patch('statefile.os.replace', side_effect=OSError('full')), self.assertRaises(OSError):
                    operation(*args)
                self.assertEqual(path.read_text(), before)
                self.assertEqual(store.load(), cached)
                self.assertTrue(operation(*args))
                self.assertEqual(store.load(), StreamStateStore(path).load())

    def test_document_without_settings_loads_with_defaults(self):
        path = state_path(self)
        path.write_text(json.dumps({'streams': {'boats': RECORD}}))
        document = StreamStateStore(path).load()
        self.assertNotIn('settings', document)
        self.assertEqual(RuntimeSettings.from_document(document.get('settings', {})), RuntimeSettings())

    def test_invalid_documents_are_rejected_without_rewrite(self):
        for document, message in (
            ({'streams': []}, 'Invalid state document'),
            ({'streams': {'bad name': RECORD}}, 'Invalid saved stream'),
            ({'streams': {'boats': {**RECORD, 'transport': 'pigeon'}}}, 'unknown transport'),
            ({'streams': {'boats': {**RECORD, 'port': 70000}}}, 'invalid port'),
            ({'streams': {'boats': {**RECORD, 'loop_count': -3}}}, 'invalid loop count'),
            ({'streams': {}, 'settings': {'udp_first_port': 0}}, 'udp_first_port'),
        ):
            with self.subTest(message=message):
                path = state_path(self)
                raw = json.dumps(document)
                path.write_text(raw)
                with self.assertRaisesRegex(ValueError, message):
                    StreamStateStore(path).load()
                self.assertEqual(path.read_text(), raw)
