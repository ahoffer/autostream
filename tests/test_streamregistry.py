from dataclasses import replace
import logging
from pathlib import Path
import unittest

from runtimesettings import RuntimeSettings
from streamregistry import StreamRegistry
from streamsupport import FakeProcess, TEST_SETTINGS, create, inventory, setup_registry


class CreateAndDeleteTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source, self.config = setup_registry(self)

    def test_create_assigns_the_lowest_free_port_of_the_transport_and_starts(self):
        udp = create(self.registry, 'boats-udp', self.source, 'udp')
        srt = create(self.registry, 'boats-srt', self.source, 'srt_listen')
        second_udp = create(self.registry, 'boats-udp2', self.source, 'udp')
        self.assertEqual((udp.binding.port, srt.binding.port, second_udp.binding.port), (40000, 41000, 40001))
        self.assertEqual(udp.status()['delivery'], 'udp://receiver:40000?pkt_size=1316')
        self.assertEqual(srt.status()['delivery'], 'srt://0.0.0.0:41000?mode=listener')
        self.assertTrue(all(row['running'] for row in self.registry.status()))
        self.assertEqual(len(FakeProcess.runs), 3)
        saved = self.store.load()['streams']['boats-udp']
        self.assertEqual((saved['filename'], saved['transport'], saved['port'], saved['enabled']),
                         (self.source.name, 'udp', 40000, True))

    def test_rtsp_and_hls_take_no_port_and_hls_writes_under_the_hls_directory(self):
        rtsp = create(self.registry, 'boats', self.source, 'rtsp')
        hls = create(self.registry, 'boats-hls', self.source, 'hls')
        self.assertIsNone(rtsp.binding.port)
        self.assertIsNone(hls.binding.port)
        self.assertEqual(rtsp.status()['delivery'], 'rtsp://localhost:8554/boats')
        self.assertEqual(hls.status()['delivery'], str(self.config.hls_dir / 'boats-hls' / 'index.m3u8'))
        self.assertTrue((self.config.hls_dir / 'boats-hls').is_dir())
        self.assertIn('-hls_segment_type', FakeProcess.runs[1].argv)

    def test_invalid_or_duplicate_creation_is_refused_with_a_reason(self):
        create(self.registry, 'boats', self.source, 'rtsp')
        for name, transport, filename, message in (
            ('boats', 'udp', self.source.name, 'exists'),
            ('bad name', 'udp', self.source.name, 'letters'),
            ('x', 'carrier-pigeon', self.source.name, 'Unknown transport'),
            ('y', 'udp', '', 'Choose a video file'),
        ):
            with self.subTest(name=name, transport=transport):
                created, error = self.registry.create_stream(name, filename, transport, {})
                self.assertFalse(created)
                self.assertIn(message, error)
        self.assertEqual(len(self.registry.status()), 1)

    def test_create_stopped_prepares_a_preview_without_a_process(self):
        create(self.registry, 'boats-udp', self.source, 'udp', start=False, mode='transcode')
        status = self.registry.get_stream('boats-udp').status()
        self.assertEqual((status['state'], status['running']), ('stopped', False))
        self.assertIn('libx264', status['command'])
        self.assertEqual(len(FakeProcess.runs), 0)

    def test_delete_stops_the_process_frees_the_port_and_removes_the_record(self):
        create(self.registry, 'boats-udp', self.source, 'udp')
        run = FakeProcess.runs[0]
        self.assertTrue(self.registry.delete_stream('boats-udp'))
        self.assertEqual(run.code, -15)
        self.assertIsNone(self.registry.get_stream('boats-udp'))
        self.assertNotIn('boats-udp', self.store.load()['streams'])
        self.assertEqual(create(self.registry, 'again', self.source, 'udp').binding.port, 40000)

    def test_delete_refuses_while_the_process_cannot_stop(self):
        create(self.registry, 'boats-udp', self.source, 'udp')
        FakeProcess.runs[0].stuck = True
        self.assertFalse(self.registry.delete_stream('boats-udp'))
        self.assertIsNotNone(self.registry.get_stream('boats-udp'))
        FakeProcess.runs[0].stuck = False


class RebindTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source, self.config = setup_registry(self)
        self.stream = create(self.registry, 'boats', self.source, 'udp')

    def another_file(self, name='other.mp4'):
        other = self.source.with_name(name)
        other.touch()
        return other

    def test_changing_transport_takes_a_port_of_the_new_kind_and_restarts_once(self):
        self.assertTrue(self.registry.update_stream('boats', {'transport': 'srt_listen', 'loop_count': 2}))
        status = self.stream.status()
        self.assertEqual((status['transport'], status['port'], status['loop_count']), ('srt_listen', 41000, 2))
        self.assertEqual(FakeProcess.runs[0].code, -15)
        self.assertEqual(len(FakeProcess.runs), 2)
        self.assertIn('srt://0.0.0.0:41000?mode=listener', FakeProcess.runs[1].argv)
        self.assertEqual(self.store.load()['streams']['boats']['transport'], 'srt_listen')

    def test_multicast_takes_a_random_high_port_outside_every_first_port_range(self):
        self.assertTrue(self.registry.update_stream('boats', {'transport': 'multicast'}))
        status = self.stream.status()
        self.assertTrue(50000 <= status['port'] <= 59999, status['port'])
        self.assertEqual(status['delivery'], f'udp://239.1.1.2:{status["port"]}?pkt_size=1316&ttl=1')
        second = create(self.registry, 'boats-mcast-2', self.source, 'multicast')
        self.assertTrue(50000 <= second.binding.port <= 59999)
        self.assertNotEqual(second.binding.port, status['port'])
        self.assertEqual(self.store.load()['streams']['boats-mcast-2']['port'], second.binding.port)

    def test_renaming_keeps_the_port_the_record_and_the_place_in_the_list(self):
        create(self.registry, 'other', self.source, 'srt_listen')
        self.assertTrue(self.registry.update_stream('boats', {'name': 'ships'}))
        self.assertIsNone(self.registry.get_stream('boats'))
        self.assertEqual([row['name'] for row in self.registry.status()], ['ships', 'other'])
        self.assertEqual(self.registry.get_stream('ships').status()['port'], 40000)
        self.assertEqual(list(self.store.load()['streams']), ['ships', 'other'])
        # A UDP command does not name the stream, so nothing restarts.
        self.assertEqual(len(FakeProcess.runs), 2)
        self.assertIsNone(FakeProcess.runs[0].code)

    def test_renaming_restarts_the_kinds_whose_command_names_the_stream(self):
        create(self.registry, 'r', self.source, 'rtsp')
        self.assertTrue(self.registry.update_stream('r', {'name': 'r2'}))
        self.assertEqual(FakeProcess.runs[1].code, -15)
        self.assertIn('rtsp://localhost:8554/r2', FakeProcess.runs[2].argv)
        self.assertEqual(self.registry.get_stream('r2').status()['delivery'], 'rtsp://localhost:8554/r2')

    def test_renaming_with_other_changes_applies_both_in_one_restart(self):
        self.assertTrue(self.registry.update_stream('boats', {'name': 'ships', 'loop_count': 3}))
        status = self.registry.get_stream('ships').status()
        self.assertEqual((status['name'], status['loop_count']), ('ships', 3))
        self.assertEqual(len(FakeProcess.runs), 2)
        self.assertEqual(self.store.load()['streams']['ships']['loop_count'], 3)

    def test_renaming_to_a_taken_or_invalid_name_is_refused(self):
        create(self.registry, 'other', self.source, 'srt_listen')
        for name in ('other', 'bad name'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.registry.update_stream('boats', {'name': name})
        self.assertIsNotNone(self.registry.get_stream('boats'))

    def test_changing_the_file_restarts_on_the_new_source_and_keeps_the_port(self):
        other = self.another_file()
        self.assertTrue(self.registry.update_stream('boats', {'filename': other.name}))
        status = self.stream.status()
        self.assertEqual((status['filename'], status['port']), (other.name, 40000))
        self.assertIn(str(other), FakeProcess.runs[1].argv)

    def test_a_missing_source_waits_and_starts_when_the_file_appears(self):
        created, _ = self.registry.create_stream('ghost', 'later.mp4', 'udp', {})
        self.assertTrue(created)
        status = self.registry.get_stream('ghost').status()
        self.assertEqual((status['state'], status['reason']), ('waiting', 'source file missing'))
        self.assertEqual(len(FakeProcess.runs), 1)
        later = self.another_file('later.mp4')
        self.registry.supervise(inventory(self.source, later, creates=(later,)))
        self.assertTrue(self.registry.get_stream('ghost').status()['running'])

    def test_deleting_the_file_stops_the_stream_until_it_returns(self):
        self.source.unlink()
        self.registry.supervise(inventory(deletes=(self.source,)))
        status = self.stream.status()
        self.assertEqual((status['state'], status['reason']), ('waiting', 'source file missing'))
        self.assertEqual(FakeProcess.runs[0].code, -15)
        self.source.touch()
        self.registry.supervise(inventory(self.source, creates=(self.source,)))
        self.assertTrue(self.stream.status()['running'])
        self.assertEqual(len(FakeProcess.runs), 2)

    def test_a_modified_file_restarts_only_the_streams_playing_it(self):
        other = self.another_file()
        create(self.registry, 'other', other, 'udp')
        other_run = FakeProcess.runs[1]
        self.registry.supervise(inventory(self.source, other, modifies=(self.source,)))
        self.assertEqual(len(FakeProcess.runs), 3)
        self.assertEqual(FakeProcess.runs[0].code, -15)
        self.assertIsNone(other_run.code)


class SettingsTests(unittest.TestCase):
    def test_transports_needing_settings_wait_with_the_reason(self):
        registry, _, _, source, _ = setup_registry(self, settings=RuntimeSettings())
        reasons = {}
        for transport in ('udp', 'multicast', 'srtp', 'srt_listen'):
            reasons[transport] = create(registry, transport, source, transport).status()['reason']
        self.assertEqual(reasons['udp'], 'no destination host configured')
        self.assertEqual(reasons['multicast'], 'no multicast group configured')
        self.assertEqual(reasons['srtp'], 'no destination host configured')
        self.assertIsNone(reasons['srt_listen'])
        self.assertEqual(len(FakeProcess.runs), 1)

    def test_apply_settings_restarts_only_streams_whose_delivery_changed(self):
        registry, store, resolver, source, _ = setup_registry(self)
        create(registry, 'u', source, 'udp')
        create(registry, 's', source, 'srt_listen')
        srt_run = FakeProcess.runs[1]
        settings = replace(TEST_SETTINGS, destination_host='other-receiver')
        self.assertTrue(registry.apply_settings(settings))
        self.assertEqual(len(FakeProcess.runs), 3)
        self.assertEqual(FakeProcess.runs[0].code, -15)
        self.assertIsNone(srt_run.code)
        self.assertIn('udp://other-receiver:40000?pkt_size=1316', FakeProcess.runs[2].argv)
        self.assertEqual(store.load()['settings'], settings.to_document())
        registry.supervise()
        self.assertEqual(resolver.refreshed, {'other-receiver'})

    def test_first_port_changes_affect_new_streams_only(self):
        registry, _, _, source, _ = setup_registry(self)
        first = create(registry, 'u', source, 'udp')
        self.assertTrue(registry.apply_settings(replace(TEST_SETTINGS, udp_first_port=50000)))
        self.assertEqual(first.status()['port'], 40000)
        self.assertEqual(len(FakeProcess.runs), 1)
        self.assertEqual(create(registry, 'v', source, 'udp').status()['port'], 50000)

    def test_a_setting_a_waiting_stream_needs_starts_it_on_save(self):
        registry, _, _, source, _ = setup_registry(self, settings=RuntimeSettings())
        stream = create(registry, 'u', source, 'udp')
        self.assertEqual(stream.status()['state'], 'waiting')
        self.assertTrue(registry.apply_settings(RuntimeSettings(destination_host='receiver')))
        self.assertTrue(stream.status()['running'])

    def test_cap_from_settings_applies_to_transcoding_streams_only(self):
        registry, _, _, source, _ = setup_registry(self)
        transcoded = create(registry, 't', source, 'rtsp', mode='transcode')
        copied = create(registry, 'c', source, 'udp')
        copied_run = FakeProcess.runs[1]
        self.assertIn('-maxrate 2000000', transcoded.status()['command'])
        self.assertTrue(registry.apply_settings(replace(TEST_SETTINGS, max_video_bitrate='3M')))
        self.assertIn('-maxrate 3000000', transcoded.status()['command'])
        self.assertEqual(len(FakeProcess.runs), 3)
        self.assertIsNone(copied_run.code)

    def test_restart_restores_streams_with_ports_and_choices_from_state(self):
        registry, store, resolver, source, config = setup_registry(self)
        create(registry, 'u', source, 'udp', loop_count=2).stop_streaming()
        create(registry, 's', source, 'srt_listen')
        restarted = StreamRegistry(config, store, resolver, lambda name: logging.Logger(name))
        restarted.load_persisted_state()
        restarted.supervise(inventory(source))
        rows = {row['name']: row for row in restarted.status()}
        self.assertEqual((rows['u']['port'], rows['u']['loop_count'], rows['u']['enabled']), (40000, 2, False))
        self.assertEqual((rows['s']['port'], rows['s']['running']), (41000, True))
        self.assertEqual(create(restarted, 'v', source, 'udp').status()['port'], 40001)
