from pathlib import Path
import unittest
import subprocess
from unittest.mock import Mock, patch

import ffmpegcmd
import ffmpegprocess
import streams
from streammodel import Stream
from testsupport import make_config


class StreamStartStopTests(unittest.TestCase):
    def setUp(self):
        self.manager = streams.StreamManager(make_config())
        self.stream = Stream("demo", "demo.mp4", 40000, config=self.manager.config)
        for output in self.stream.outputs:
            output.enabled = False
        self.manager.registry.streams["demo"] = self.stream
        self.enterContext(patch.object(self.manager.registry, "persist"))
        self.enterContext(patch.object(self.manager, "output_reachable", True))
        self.spawn = self.enterContext(patch.object(ffmpegprocess.subprocess, "Popen"))
        self.profile = ffmpegcmd.FileProfile(("h264",), (), True, "yuv420p", 1000000, 25)
        self.probe = self.enterContext(patch.object(ffmpegcmd, "probe_file", return_value=self.profile))

    def test_stop_during_probe_cancels_start(self):
        for output_name, reachable in (("rtsp", True), ("udp", True), ("udp", False)):
            with self.subTest(output=output_name, reachable=reachable):
                self.spawn.reset_mock()

                def stop_during_probe(video_path):
                    # Complete Stop before the pending Start resumes from probing.
                    self.assertTrue(self.manager.disable_output("demo", output_name))
                    return self.profile

                self.probe.side_effect = stop_during_probe
                with patch.object(self.manager, "output_reachable", reachable):
                    started = self.manager.start_output("demo", output_name)

                self.assertFalse(started)
                self.spawn.assert_not_called()
                output = self.stream.output(output_name)
                self.assertFalse(output.enabled)
                self.assertIsNone(output.process)

    def test_start_without_stop_launches_output(self):
        self.assertTrue(self.manager.start_output("demo", "rtsp"))
        self.spawn.assert_called_once()
        self.assertTrue(self.stream.rtsp.enabled)
        self.assertIs(self.stream.rtsp.process, self.spawn.return_value)

    def test_enabled_udp_waiting_for_destination_still_reports_success(self):
        with patch.object(self.manager, "output_reachable", False):
            self.assertTrue(self.manager.start_output("demo", "udp"))
        self.spawn.assert_not_called()
        self.assertTrue(self.stream.udp.enabled)
        self.assertIsNone(self.stream.udp.process)

    def test_wedged_process_keeps_destination_occupied(self):
        self.manager.start_output("demo", "rtsp")
        process = self.stream.rtsp.process
        process.wait.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
        with self.assertLogs(level="WARNING"):
            self.manager.disable_output("demo", "rtsp")
            self.assertFalse(self.manager.start_output("demo", "rtsp"))
        self.assertIs(self.stream.rtsp.process, process)
        self.assertTrue(self.stream.rtsp.stopping)
        process.kill.assert_called_once()
        self.spawn.assert_called_once()

    def test_successful_stop_clears_only_the_process_that_was_stopped(self):
        self.manager.start_output("demo", "rtsp")
        old_process = self.stream.rtsp.process
        replacement = Mock()

        def replace_while_waiting(timeout):
            self.stream.rtsp.mark_started(replacement, ["replacement"])

        old_process.wait.side_effect = replace_while_waiting
        self.manager.stop_output("demo", "rtsp")
        self.assertIs(self.stream.rtsp.process, replacement)
        self.assertFalse(self.stream.rtsp.stopping)

    def test_crash_restarts_output_and_retains_diagnostic(self):
        self.manager.start_output("demo", "rtsp")
        self.stream.rtsp.process.poll.return_value = 1
        self.stream.rtsp.process.returncode = 1
        with patch.object(Path, "exists", return_value=True), self.assertLogs(level="WARNING"):
            self.manager.cleanup_dead_processes()
        self.assertEqual(self.spawn.call_count, 2)
        status = self.manager.get_stream_status()[0]["outputs"]["rtsp"]
        self.assertEqual(status["state"], "crashing")
        self.assertIn("ffmpeg exited with code 1", status["error"])

    def test_successful_finite_loop_is_not_restarted(self):
        self.stream.loop_count = 0
        self.manager.start_output("demo", "rtsp")
        self.stream.rtsp.process.poll.return_value = 0
        self.stream.rtsp.process.returncode = 0
        self.manager.cleanup_dead_processes()
        self.spawn.assert_called_once()
        self.assertTrue(self.stream.rtsp.completed)
        self.assertIsNone(self.stream.rtsp.process)


class StreamFileModificationTests(unittest.TestCase):
    def setUp(self):
        self.manager = streams.StreamManager(make_config())
        self.stream = Stream("demo", "demo.mp4", 40000, loop_count=0, config=self.manager.config)
        for output in self.stream.outputs:
            output.completed = True
        self.manager.registry.streams["demo"] = self.stream
        self.enterContext(patch.object(self.manager.registry, "persist"))
        self.enterContext(patch.object(self.manager, "output_reachable", True))
        self.spawn = self.enterContext(patch.object(ffmpegprocess.subprocess, "Popen"))
        profile = ffmpegcmd.FileProfile(("h264",), (), True, "yuv420p", 1000000, 25)
        self.enterContext(patch.object(ffmpegcmd, "probe_file", return_value=profile))

    def test_modified_completed_file_launches_only_enabled_outputs(self):
        for stopped in ((), ("rtsp",), ("udp",), ("rtsp", "udp")):
            with self.subTest(stopped=stopped):
                self.spawn.reset_mock()
                for output in self.stream.outputs:
                    output.clear_process()
                    output.enabled = True
                    output.completed = True
                if len(stopped) == 2:
                    self.manager.stop_stream("demo")
                else:
                    for output_name in stopped:
                        self.manager.disable_output("demo", output_name)

                self.manager.handle_modify(Path("demo.mp4"))

                self.assertEqual(self.spawn.call_count, 2 - len(stopped))
                for output in self.stream.outputs:
                    self.assertEqual(output.enabled, output.name not in stopped)
                    self.assertEqual(output.running, output.enabled)
                    self.assertFalse(output.completed)

    def test_modified_completed_udp_recovers_when_destination_returns(self):
        with patch.object(self.manager, "output_reachable", False):
            self.manager.handle_modify(Path("demo.mp4"))
        self.assertTrue(self.stream.rtsp.running)
        self.assertFalse(self.stream.udp.running)
        self.assertFalse(self.stream.udp.completed)

        self.manager.recover_udp_outputs()

        self.assertTrue(self.stream.udp.running)
        self.assertEqual(self.spawn.call_count, 2)

    def test_unchanged_completed_file_stays_completed(self):
        self.manager.launch_enabled_outputs("demo")
        self.manager.recover_udp_outputs()

        self.spawn.assert_not_called()
        for output in self.stream.outputs:
            self.assertTrue(output.completed)


class StreamFilenameTests(unittest.TestCase):
    def setUp(self):
        self.manager = streams.StreamManager(make_config())

    def test_punctuation_only_stem_is_skipped_on_create_and_modify(self):
        with (patch.object(self.manager.registry, "streams", {}),
              patch.object(self.manager.registry, "udp_port_for") as allocate,
              patch.object(self.manager.registry, "persist") as persist,
              patch.object(self.manager, "launch_enabled_outputs") as launch,
              self.assertLogs("streamregistry", level="WARNING")):
            self.manager.handle_create(Path("!!!.mp4"))
            self.manager.handle_modify(Path("!!!.mp4"))
            self.assertEqual(self.manager.registry.streams, {})
            allocate.assert_not_called()
            persist.assert_not_called()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
