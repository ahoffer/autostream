from dataclasses import fields
import unittest

from runtimesettings import PORT_SETTINGS, RuntimeSettings
from transports import KINDS, KINDS_BY_ID

SAMPLE_FIELDS = {**RuntimeSettings(destination_host='receiver', multicast_group='239.1.1.2', srtp_key='a2V5').to_document(),
                 'port': 40001, 'name': 'boats', 'host': 'publisher', 'rtsp_port': 8554, 'hls_port': 8888,
                 'rtmp_port': 1935, 'api_port': 8080, 'hls_dir': '/app/hls'}


class TransportTableTests(unittest.TestCase):
    def test_ids_are_unique_and_every_template_formats_from_the_known_fields(self):
        self.assertEqual(len(KINDS_BY_ID), len(KINDS))
        for kind in KINDS:
            with self.subTest(kind=kind.id):
                self.assertNotIn('{', kind.delivery.format(**SAMPLE_FIELDS))
                for _, template in kind.register:
                    self.assertNotIn('{', template.format(**SAMPLE_FIELDS))
                for arg in kind.extra_output_args:
                    self.assertNotIn('{', arg.format(**SAMPLE_FIELDS))

    def test_needs_and_first_ports_name_real_settings(self):
        setting_names = {field.name for field in fields(RuntimeSettings)}
        for kind in KINDS:
            with self.subTest(kind=kind.id):
                self.assertTrue(set(kind.needs) <= setting_names)
                self.assertIn(kind.first_port_setting, (None, *PORT_SETTINGS))
                self.assertIn(kind.direction, ('push', 'listen', 'serve'))
                self.assertIn(kind.muxer, ('rtsp', 'mpegts', 'rtp_mpegts', 'hls'))

    def test_only_mediamtx_drops_klv_and_push_kinds_name_their_target(self):
        self.assertEqual([kind.id for kind in KINDS if not kind.carries_klv], ['rtsp'])
        self.assertEqual([kind.id for kind in KINDS if kind.random_port], ['multicast'])
        self.assertEqual([kind.id for kind in KINDS if not kind.takes_port], ['rtsp', 'hls'])
        for kind in KINDS:
            if kind.direction == 'push':
                self.assertTrue({'destination_host', 'multicast_group'} & set(kind.needs), kind.id)
            else:
                self.assertNotIn('destination_host', kind.needs, kind.id)
