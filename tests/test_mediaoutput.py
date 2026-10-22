import logging
import unittest
from unittest.mock import patch

from mediaoutput import DEFAULT_CHOICES, Destination, MediaOutput
from outputregistry import stream_name_for
from outputsupport import FakeProcess, setup_outputs


class OutputBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source = setup_outputs(self)
        name = stream_name_for(self.source)
        self.rtsp = self.registry.get_output(name + '/rtsp')
        self.udp = self.registry.get_output(name + '/udp')

    def test_settings_and_stop_affect_only_one_output_and_are_saved(self):
        udp_run = FakeProcess.runs[1]
        self.assertTrue(self.rtsp.update_settings({'mode': 'transcode', 'frame_rate': 12, 'loop_count': 0}))
        self.assertEqual(self.rtsp.status()['frame_rate'], 12)
        self.assertEqual(self.udp.status()['frame_rate'], 30)
        self.assertIsNone(udp_run.code)
        self.assertTrue(self.rtsp.stop_streaming())
        self.assertEqual(self.rtsp.status()['state'], 'stopped')
        self.assertTrue(self.udp.status()['running'])
        saved = self.store.load()['outputs'][self.rtsp.status()['id']]
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['loop_count'], 0)

    def test_stuck_shutdown_prevents_replacement_and_reports_actual_command(self):
        old = FakeProcess.runs[0]
        old.stuck = True
        command = self.rtsp.status()['command']
        self.assertFalse(self.rtsp.update_settings({'frame_rate': 12}))
        self.assertEqual(len(FakeProcess.runs), 2)
        self.assertEqual(self.rtsp.status()['command'], command)
        self.assertEqual(self.rtsp.status()['frame_rate'], 12)
        self.assertFalse(self.rtsp.retire())
        old.stuck = False
        self.assertTrue(self.rtsp.retire())

    def test_finite_completion_stays_complete_until_file_changes(self):
        self.rtsp.update_settings({'loop_count': 0})
        FakeProcess.runs[-1].finish()
        count = len(FakeProcess.runs)
        self.rtsp.supervise()
        self.rtsp.supervise()
        self.assertEqual(len(FakeProcess.runs), count)
        self.assertEqual(self.rtsp.status()['state'], 'completed')
        self.rtsp.source_changed()
        self.assertTrue(self.rtsp.status()['running'])

    def test_crash_recovery_then_stable_run_clears_history(self):
        run = FakeProcess.runs[0]
        run.tail = 'failure detail'
        run.finish(1)
        self.rtsp.supervise()
        self.assertEqual(self.rtsp.status()['state'], 'crashing')
        self.assertIn('failure detail', self.rtsp.status()['error'])
        FakeProcess.runs[-1].started -= 61
        self.rtsp.supervise()
        self.assertEqual(self.rtsp.status()['state'], 'running')
        self.assertIsNone(self.rtsp.status()['error'])

    def test_udp_waits_for_cached_dns_then_recovers(self):
        self.udp.stop_streaming()
        self.resolver.blocked = True
        self.assertTrue(self.udp.start_streaming())
        self.assertEqual(self.udp.status()['state'], 'waiting')
        self.resolver.blocked = False
        self.udp.supervise()
        self.assertTrue(self.udp.status()['running'])

    def test_missing_destination_is_accepted_intent_with_the_registry_reason(self):
        output = MediaOutput('other/udp', self.source, dict(DEFAULT_CHOICES), Destination(None, 'no destination host configured'),
                             max_video_bps=None, store=self.store, resolver=self.resolver, logger=logging.Logger('other'))
        count = len(FakeProcess.runs)
        self.assertTrue(output.start_streaming())
        self.assertEqual((output.status()['state'], output.status()['reason']), ('waiting', 'no destination host configured'))
        self.assertEqual(len(FakeProcess.runs), count)

    def test_start_all_keeps_running_processes(self):
        self.assertTrue(self.registry.start_all_outputs())
        self.assertEqual(len(FakeProcess.runs), 2)

    def test_status_never_probes_or_resolves(self):
        with patch('ffmpegcmd.probe_file', side_effect=AssertionError('status probed')), patch.object(self.resolver, 'refresh', side_effect=AssertionError('status resolved')):
            self.assertTrue(self.rtsp.status()['running'])

    def test_save_failure_is_reported_but_stop_is_honored(self):
        with patch.object(self.store, 'save_output', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                self.rtsp.stop_streaming()
        self.assertFalse(self.rtsp.status()['running'])

    def test_shutdown_does_not_save_disabled_choices(self):
        self.assertTrue(self.registry.stop_all_outputs(persist=False))
        self.assertTrue(all(row['enabled'] for row in self.store.load()['outputs'].values()))
        self.assertTrue(all(run.code is not None for run in FakeProcess.runs))

    def test_stopped_output_settings_refresh_preview_without_start(self):
        self.rtsp.stop_streaming()
        count = len(FakeProcess.runs)
        self.rtsp.update_settings({'mode': 'transcode', 'frame_rate': 12})
        self.rtsp.source_changed()
        self.assertEqual(len(FakeProcess.runs), count)
        self.assertIn('-r 12', self.rtsp.status()['command'])

    def test_invalid_media_waits_for_file_change_without_launch_loop(self):
        import ffmpegcmd
        self.rtsp.stop_streaming()
        count = len(FakeProcess.runs)
        with patch('ffmpegcmd.probe_file', return_value=ffmpegcmd.UNKNOWN_PROFILE) as probe:
            self.assertFalse(self.rtsp.start_streaming())
            self.rtsp.supervise()
            self.rtsp.supervise()
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(FakeProcess.runs), count)
        self.assertTrue(self.rtsp.source_changed())
        self.assertTrue(self.rtsp.status()['running'])
