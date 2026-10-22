import unittest

from runtimesettings import RuntimeSettings, validation_error


class RuntimeSettingsTests(unittest.TestCase):
    def test_defaults_carry_the_consumer_default_range_and_nothing_else(self):
        settings = RuntimeSettings()
        self.assertEqual((settings.destination_host, settings.advertised_host, settings.max_video_bitrate), (None, None, ''))
        self.assertEqual((settings.udp_base_port, settings.udp_last_port), (40000, 40100))
        self.assertTrue(settings.has_port_range)
        self.assertIsNone(settings.max_video_bps)
        self.assertIsNone(validation_error(settings))

    def test_document_round_trip_and_absent_keys_take_defaults(self):
        self.assertEqual(RuntimeSettings.from_document({}), RuntimeSettings())
        settings = RuntimeSettings('receiver', 41000, 41010, 'publisher', '2M')
        self.assertEqual(RuntimeSettings.from_document(settings.to_document()), settings)
        self.assertEqual(settings.max_video_bps, 2_000_000)
        without_range = RuntimeSettings.from_document({'udp_base_port': None, 'udp_last_port': None, 'ignored': 1})
        self.assertFalse(without_range.has_port_range)
        self.assertTrue(settings.port_in_range(41010))
        self.assertFalse(settings.port_in_range(41011))

    def test_invalid_documents_raise_with_the_reason(self):
        for document, message in (
            ({'udp_base_port': 40100, 'udp_last_port': 40000}, 'below'),
            ({'udp_base_port': None}, 'together'),
            ({'udp_base_port': 0, 'udp_last_port': 10}, '1 to 65535'),
            ({'udp_base_port': '40000', 'udp_last_port': 40001}, 'whole numbers'),
            ({'max_video_bitrate': 'bad'}, 'like 2M'),
            ({'max_video_bitrate': '0'}, 'positive'),
            ({'destination_host': 'a b'}, 'host name or address'),
            ({'advertised_host': 7}, 'host name or address'),
        ):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, message):
                RuntimeSettings.from_document(document)

    def test_query_blanks_mean_unset_and_errors_are_returned_not_raised(self):
        query = {'destination_host': [' receiver '], 'udp_base_port': [''], 'udp_last_port': [''],
                 'advertised_host': [''], 'max_video_bitrate': ['']}
        self.assertEqual(RuntimeSettings.from_query(query),
                         (RuntimeSettings(destination_host='receiver', udp_base_port=None, udp_last_port=None), None))
        self.assertEqual(RuntimeSettings.from_query({'udp_base_port': ['abc'], 'udp_last_port': ['1']}),
                         (None, 'udp_base_port must be a whole number'))
        self.assertEqual(RuntimeSettings.from_query({'udp_base_port': ['2'], 'udp_last_port': ['1']})[1],
                         'udp_last_port is below udp_base_port')
