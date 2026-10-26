import base64
import unittest

from runtimesettings import PORT_SETTINGS, RuntimeSettings, validation_error


class RuntimeSettingsTests(unittest.TestCase):
    def test_defaults_carry_first_ports_a_group_and_a_key_but_no_hosts(self):
        settings = RuntimeSettings()
        self.assertEqual((settings.destination_host, settings.advertised_host, settings.max_video_bitrate), (None, None, ''))
        self.assertEqual(settings.multicast_group, '239.255.0.1')
        self.assertEqual(len(base64.b64decode(settings.srtp_key, validate=True)), 30)
        self.assertEqual((settings.udp_first_port, settings.srt_listen_first_port), (40000, 41000))
        self.assertIsNone(settings.max_video_bps)
        self.assertIsNone(validation_error(settings))

    def test_document_round_trip_and_absent_keys_take_defaults(self):
        self.assertEqual(RuntimeSettings.from_document({}), RuntimeSettings())
        settings = RuntimeSettings('receiver', '239.1.1.2', 'publisher', '2M', 'a2V5', tcp_first_port=45000)
        self.assertEqual(RuntimeSettings.from_document(settings.to_document()), settings)
        self.assertEqual(settings.max_video_bps, 2_000_000)
        self.assertEqual(RuntimeSettings.from_document({'ignored': 1, 'udp_first_port': 50000}).udp_first_port, 50000)

    def test_invalid_documents_raise_with_the_reason(self):
        for document, message in (
            ({'udp_first_port': 0}, 'udp_first_port must be a whole number from 1 to 65535'),
            ({'tcp_first_port': '42000'}, 'tcp_first_port'),
            ({'multicast_group': '10.0.0.1'}, 'multicast address'),
            ({'multicast_group': 'group'}, 'multicast address'),
            ({'max_video_bitrate': 'bad'}, 'like 2M'),
            ({'max_video_bitrate': '0'}, 'positive'),
            ({'destination_host': 'a b'}, 'host name or address'),
            ({'advertised_host': 7}, 'host name or address'),
            ({'srtp_key': 'has space'}, 'srtp_key'),
            ({'srtp_key': ''}, 'srtp_key'),
            ({'multicast_group': None}, 'multicast address'),
        ):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, message):
                RuntimeSettings.from_document(document)

    def test_query_blanks_mean_unset_hosts_and_defaults_elsewhere(self):
        query = {'destination_host': [' receiver '], 'multicast_group': [''], 'advertised_host': [''],
                 'max_video_bitrate': [''], 'srtp_key': [''], **{name: [''] for name in PORT_SETTINGS}}
        self.assertEqual(RuntimeSettings.from_query(query), (RuntimeSettings(destination_host='receiver'), None))
        self.assertEqual(RuntimeSettings.from_query({'udp_first_port': ['abc']}), (None, 'udp_first_port must be a whole number'))
        self.assertEqual(RuntimeSettings.from_query({'udp_first_port': ['70000']})[1],
                         'udp_first_port must be a whole number from 1 to 65535')
        self.assertEqual(RuntimeSettings.from_query({'http_first_port': ['45000']})[0].http_first_port, 45000)
