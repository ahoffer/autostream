from dataclasses import replace
import logging
from pathlib import Path
import unittest
from unittest.mock import patch

from outputregistry import OutputRegistry, stream_name_for
from outputsupport import FakeProcess, TEST_SETTINGS, inventory, setup_outputs
from runtimesettings import RuntimeSettings
from testsupport import make_config

ONE_PORT = replace(TEST_SETTINGS, udp_last_port=40000)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source = setup_outputs(self, settings=ONE_PORT)
        self.name = stream_name_for(self.source)

    def udp_status(self, name=None):
        return next(row for row in self.registry.status() if row['id'] == (name or self.name) + '/udp')

    def another_file(self):
        other = self.source.with_name(self.source.stem + '-other.mp4')
        other.touch()
        self.addCleanup(other.unlink, missing_ok=True)
        return other

    def test_removed_file_restores_choices_and_port(self):
        rtsp = self.registry.get_output(self.name + '/rtsp')
        rtsp.update_settings({'loop_count': 2, 'frame_rate': 15})
        rtsp.stop_streaming()
        self.registry.supervise(inventory(deletes=(self.source,)))
        self.assertIsNone(self.registry.get_output(self.name + '/rtsp'))
        self.registry.supervise(inventory(self.source))
        restored = self.registry.get_output(self.name + '/rtsp').status()
        self.assertFalse(restored['enabled'])
        self.assertEqual(restored['loop_count'], 2)
        self.assertEqual(restored['frame_rate'], 15)
        self.assertTrue(self.registry.get_output(self.name + '/udp').status()['destination'].endswith(':40000'))

    def test_returned_file_waits_for_old_process_then_registers_from_inventory(self):
        old = FakeProcess.runs[1]
        old.stuck = True
        self.registry.supervise(inventory(deletes=(self.source,)))
        self.registry.supervise(inventory(self.source))
        self.assertIsNone(self.registry.get_output(self.name + '/udp'))
        self.assertEqual(len(FakeProcess.runs), 2)
        self.assertEqual(self.registry.status()[1]['state'], 'retiring')
        old.stuck = False
        self.registry.supervise()
        self.assertEqual(len(FakeProcess.runs), 4)
        self.assertTrue(self.registry.get_output(self.name + '/udp').status()['running'])

    def test_retiring_assignment_cannot_be_reclaimed(self):
        FakeProcess.runs[1].stuck = True
        other = self.another_file()
        self.registry.supervise(inventory(other, deletes=(self.source,)))
        self.assertIsNone(self.registry.get_output(stream_name_for(other) + '/udp').status()['destination'])
        FakeProcess.runs[1].stuck = False

    def test_saved_unstable_file_keeps_port_before_discovery(self):
        config = make_config(videos_dir=self.source.parent, state_path=self.store._path)
        restarted = OutputRegistry(config, self.store, self.resolver, lambda name: logging.Logger(name))
        restarted.load_persisted_state()
        other = self.another_file()
        restarted.supervise(inventory(other, unstable=(self.source,)))
        self.assertIsNone(restarted.get_output(stream_name_for(other) + '/udp').status()['destination'])

    def test_reclaims_absent_retired_assignment(self):
        other = self.another_file()
        self.registry.supervise(inventory(other, deletes=(self.source,)))
        self.assertTrue(self.registry.get_output(stream_name_for(other) + '/udp').status()['destination'].endswith(':40000'))

    def test_registry_uses_received_inventory_without_scanning(self):
        with patch.object(Path, 'iterdir', side_effect=AssertionError('registry scanned')):
            self.registry.supervise(inventory(self.source))
        self.assertEqual(len(FakeProcess.runs), 2)

    def test_collision_and_invalid_name_do_not_replace_outputs(self):
        count = len(FakeProcess.runs)
        self.assertFalse(self.registry.register_outputs(self.source.with_suffix('.avi')))
        self.assertFalse(self.registry.register_outputs(self.source.with_name('...!.mp4')))
        self.assertEqual(len(FakeProcess.runs), count)

    def test_destination_change_restarts_enabled_udp_outputs_only(self):
        rtsp_run, udp_run = FakeProcess.runs
        self.assertTrue(self.registry.apply_settings(replace(ONE_PORT, destination_host='other-receiver')))
        self.assertEqual(len(FakeProcess.runs), 3)
        self.assertIsNone(rtsp_run.code)
        self.assertEqual(udp_run.code, -15)
        self.assertIn('udp://other-receiver:40000?pkt_size=1316', FakeProcess.runs[2].argv)
        self.registry.supervise()
        self.assertEqual(self.resolver.refreshed, {'other-receiver'})

    def test_destination_change_updates_a_stopped_output_preview_without_starting_it(self):
        udp = self.registry.get_output(self.name + '/udp')
        udp.stop_streaming()
        count = len(FakeProcess.runs)
        self.assertTrue(self.registry.apply_settings(replace(ONE_PORT, destination_host='other-receiver')))
        self.assertEqual(len(FakeProcess.runs), count)
        self.assertIn('other-receiver', udp.status()['command'])
        self.assertEqual(udp.status()['state'], 'stopped')

    def test_range_change_keeps_ports_and_flags_those_outside(self):
        udp_run = FakeProcess.runs[1]
        self.assertTrue(self.registry.apply_settings(replace(ONE_PORT, udp_base_port=50000, udp_last_port=50010)))
        udp = self.udp_status()
        self.assertEqual((udp['udp_port'], udp['port_outside_range'], udp['destination']), (40000, True, 'udp://receiver:40000'))
        self.assertIsNone(udp_run.code)
        self.assertEqual(len(FakeProcess.runs), 2)
        other = self.another_file()
        self.registry.supervise(inventory(self.source, other))
        self.assertEqual(self.udp_status(stream_name_for(other))['destination'], 'udp://receiver:50000')
        self.assertTrue(self.registry.apply_settings(replace(ONE_PORT, udp_base_port=40000, udp_last_port=50010)))
        self.assertFalse(self.udp_status()['port_outside_range'])

    def test_cap_from_settings_applies_to_transcoding_outputs_only(self):
        rtsp = self.registry.get_output(self.name + '/rtsp')
        rtsp.update_settings({'mode': 'transcode'})
        self.assertIn('-maxrate 2000000', rtsp.status()['command'])
        passthrough_udp_run = FakeProcess.runs[1]
        count = len(FakeProcess.runs)
        self.assertTrue(self.registry.apply_settings(replace(ONE_PORT, max_video_bitrate='3M')))
        self.assertIn('-maxrate 3000000', rtsp.status()['command'])
        self.assertEqual(len(FakeProcess.runs), count + 1)
        self.assertIsNone(passthrough_udp_run.code)

    def test_restored_choices_drop_retired_keys(self):
        udp_id = self.name + '/udp'
        saved = self.store.load()['outputs'][udp_id]
        self.store.save_output(udp_id, {**saved, 'loop_count': 4, 'udp_host': 'old-receiver', 'max_video_bps': 1})
        self.registry.supervise(inventory(deletes=(self.source,)))
        self.registry.supervise(inventory(self.source))
        status = self.registry.get_output(udp_id).status()
        self.assertEqual((status['loop_count'], status['destination']), (4, 'udp://receiver:40000'))
        self.assertNotIn('udp_host', status)
        self.assertNotIn('udp_host', self.store.load()['outputs'][udp_id])

    def test_apply_settings_persists_even_when_a_restart_fails(self):
        FakeProcess.runs[1].stuck = True
        settings = replace(ONE_PORT, destination_host='other-receiver')
        self.assertFalse(self.registry.apply_settings(settings))
        self.assertEqual(self.store.load()['settings'], settings.to_document())
        FakeProcess.runs[1].stuck = False


class FreshInstallTests(unittest.TestCase):
    def test_without_a_port_range_udp_waits_and_no_port_is_assigned(self):
        registry, store, _, source = setup_outputs(self, settings=RuntimeSettings(udp_base_port=None, udp_last_port=None))
        name = stream_name_for(source)
        udp = registry.get_output(name + '/udp').status()
        self.assertIsNone(store.load()['assignments'][name]['udp_port'])
        self.assertEqual((udp['state'], udp['reason'], udp['destination']), ('waiting', 'no UDP port range configured', None))
        self.assertTrue(registry.get_output(name + '/rtsp').status()['running'])
        self.assertEqual(len(FakeProcess.runs), 1)

    def test_default_settings_assign_a_port_but_wait_for_a_destination_host(self):
        registry, store, _, source = setup_outputs(self, settings=RuntimeSettings())
        name = stream_name_for(source)
        udp = registry.get_output(name + '/udp').status()
        self.assertEqual(store.load()['assignments'][name]['udp_port'], 40000)
        self.assertEqual((udp['state'], udp['reason']), ('waiting', 'no destination host configured'))
        self.assertEqual(len(FakeProcess.runs), 1)

    def test_setting_a_range_assigns_ports_and_starts_waiting_udp_outputs(self):
        registry, store, _, source = setup_outputs(self, settings=RuntimeSettings(udp_base_port=None, udp_last_port=None))
        name = stream_name_for(source)
        settings = RuntimeSettings(destination_host='receiver', udp_base_port=41000, udp_last_port=41010)
        self.assertTrue(registry.apply_settings(settings))
        udp = registry.get_output(name + '/udp').status()
        self.assertEqual((udp['destination'], udp['running']), ('udp://receiver:41000', True))
        self.assertEqual(store.load()['assignments'][name]['udp_port'], 41000)
        self.assertEqual(store.load()['settings'], settings.to_document())
