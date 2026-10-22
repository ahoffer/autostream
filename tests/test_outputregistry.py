import logging
from pathlib import Path
import unittest
from unittest.mock import patch

from outputregistry import OutputRegistry, stream_name_for
from outputsupport import FakeProcess, inventory, setup_outputs
from testsupport import make_config


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source = setup_outputs(self, udp_last_port=40000)
        self.name = stream_name_for(self.source)

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
        config = make_config(videos_dir=self.source.parent, state_path=self.store._path, udp_last_port=40000)
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
