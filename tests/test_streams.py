import os
from pathlib import Path
import unittest
from unittest.mock import patch

import ffmpegcmd
import statefile


defaults = dict(
    line.split("=", 1)
    for line in (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
    if line and not line.startswith("#")
)
with patch.dict(os.environ, defaults), patch.object(Path, "mkdir"):
    import streams


class StreamStartStopTests(unittest.TestCase):
    def setUp(self):
        self.stream = streams.Stream("demo", "demo.mp4", 40000)
        for output in self.stream.outputs:
            output.enabled = False
        self.enterContext(patch.object(streams, "_streams_by_name", {"demo": self.stream}))
        self.enterContext(patch.object(streams, "_persist"))
        self.enterContext(patch.object(streams, "_output_reachable", True))
        self.enterContext(patch.object(streams, "SHOW_FFMPEG_OUTPUT", True))
        self.spawn = self.enterContext(patch.object(streams.subprocess, "Popen"))
        self.profile = ffmpegcmd.FileProfile(("h264",), (), True, "yuv420p", 1000000, 25)
        self.probe = self.enterContext(patch.object(ffmpegcmd, "probe_file", return_value=self.profile))

    def test_stop_during_probe_cancels_start(self):
        for output_name, reachable in (("rtsp", True), ("udp", True), ("udp", False)):
            with self.subTest(output=output_name, reachable=reachable):
                self.spawn.reset_mock()

                def stop_during_probe(video_path):
                    # Complete Stop before the pending Start resumes from probing.
                    self.assertTrue(streams.disable_output("demo", output_name))
                    return self.profile

                self.probe.side_effect = stop_during_probe
                with patch.object(streams, "_output_reachable", reachable):
                    started = streams.start_output("demo", output_name)

                self.assertFalse(started)
                self.spawn.assert_not_called()
                output = self.stream.output(output_name)
                self.assertFalse(output.enabled)
                self.assertIsNone(output.process)

    def test_start_without_stop_launches_output(self):
        self.assertTrue(streams.start_output("demo", "rtsp"))
        self.spawn.assert_called_once()
        self.assertTrue(self.stream.rtsp.enabled)
        self.assertIs(self.stream.rtsp.process, self.spawn.return_value)

    def test_enabled_udp_waiting_for_destination_still_reports_success(self):
        with patch.object(streams, "_output_reachable", False):
            self.assertTrue(streams.start_output("demo", "udp"))
        self.spawn.assert_not_called()
        self.assertTrue(self.stream.udp.enabled)
        self.assertIsNone(self.stream.udp.process)


class StreamFileModificationTests(unittest.TestCase):
    def setUp(self):
        self.stream = streams.Stream("demo", "demo.mp4", 40000, loop_count=0)
        for output in self.stream.outputs:
            output.completed = True
        self.enterContext(patch.object(streams, "_streams_by_name", {"demo": self.stream}))
        self.enterContext(patch.object(streams, "_persist"))
        self.enterContext(patch.object(streams, "_output_reachable", True))
        self.enterContext(patch.object(streams, "SHOW_FFMPEG_OUTPUT", True))
        self.spawn = self.enterContext(patch.object(streams.subprocess, "Popen"))
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
                    streams.stop_stream("demo")
                else:
                    for output_name in stopped:
                        streams.disable_output("demo", output_name)

                streams.handle_modify(Path("demo.mp4"))

                self.assertEqual(self.spawn.call_count, 2 - len(stopped))
                for output in self.stream.outputs:
                    self.assertEqual(output.enabled, output.name not in stopped)
                    self.assertEqual(output.running, output.enabled)
                    self.assertFalse(output.completed)

    def test_modified_completed_udp_recovers_when_destination_returns(self):
        with patch.object(streams, "_output_reachable", False):
            streams.handle_modify(Path("demo.mp4"))
        self.assertTrue(self.stream.rtsp.running)
        self.assertFalse(self.stream.udp.running)
        self.assertFalse(self.stream.udp.completed)

        streams.recover_udp_outputs()

        self.assertTrue(self.stream.udp.running)
        self.assertEqual(self.spawn.call_count, 2)

    def test_unchanged_completed_file_stays_completed(self):
        streams.launch_enabled_outputs("demo")
        streams.recover_udp_outputs()

        self.spawn.assert_not_called()
        for output in self.stream.outputs:
            self.assertTrue(output.completed)


class StreamFilenameTests(unittest.TestCase):
    def test_punctuation_only_stem_is_skipped_on_create_and_modify(self):
        with (patch.object(streams, "_streams_by_name", {}),
              patch.object(streams, "_udp_port_for") as allocate,
              patch.object(streams, "_persist") as persist,
              patch.object(streams, "launch_enabled_outputs") as launch,
              self.assertLogs(streams.log, level="WARNING")):
            streams.handle_create(Path("!!!.mp4"))
            streams.handle_modify(Path("!!!.mp4"))
            self.assertEqual(streams._streams_by_name, {})
            allocate.assert_not_called()
            persist.assert_not_called()
            launch.assert_not_called()


class UdpPortAllocationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(streams, "_streams_by_name", {}))
        self.enterContext(patch.object(streams, "_retained_streams", {}))
        self.enterContext(patch.object(streams, "_udp_ports", {}))
        self.enterContext(patch.object(streams, "_next_udp_port", 40000))
        self.enterContext(patch.object(streams, "UDP_BASE_PORT", 40000))
        self.enterContext(patch.object(streams, "UDP_LAST_PORT", 40001))
        self.enterContext(patch.object(streams, "_persist"))
        saved = statefile.PersistedState(
            {"b": 40000, "c": 40001},
            {
                "b": statefile.PersistedStream("b.mp4", mode="transcode", removed_at=10),
                "c": statefile.PersistedStream("c.mp4", removed_at=20),
            },
        )
        with patch.object(statefile, "load_state", return_value=saved):
            streams.load_persisted_state()
        self.scan = self.enterContext(patch.object(Path, "iterdir", return_value=[]))

    def test_startup_preserves_ports_for_files_not_yet_registered(self):
        self.scan.return_value = [Path(name + ".mp4") for name in ("a", "b", "c")]
        with streams._state_lock:
            for name in ("a", "b", "c"):
                streams._claim_slot(name, name + ".mp4")
        self.assertEqual(
            {name: stream.udp_port for name, stream in streams._streams_by_name.items()},
            {"a": None, "b": 40000, "c": 40001},
        )
        self.assertEqual(streams._streams_by_name["b"].mode, "transcode")

    def test_reclaims_oldest_absent_file(self):
        with streams._state_lock:
            self.assertEqual(streams._udp_port_for("a"), 40000)
        self.assertEqual(streams._udp_ports, {"a": 40000, "c": 40001})
        self.assertNotIn("b", streams._retained_streams)

    def test_active_slot_keeps_port_even_if_saved_filename_is_absent(self):
        streams._streams_by_name["b"] = streams.Stream("b", "B.mp4", 40000)
        self.scan.return_value = [Path("B.mp4")]
        with streams._state_lock:
            self.assertEqual(streams._udp_port_for("a"), 40001)
        self.assertEqual(streams._udp_ports, {"b": 40000, "a": 40001})

    def test_directory_scan_failure_preserves_assignments(self):
        self.scan.side_effect = PermissionError("Cannot read videos")
        with streams._state_lock:
            self.assertIsNone(streams._udp_port_for("a"))
        self.assertEqual(streams._udp_ports, {"b": 40000, "c": 40001})
        self.assertEqual(set(streams._retained_streams), {"b", "c"})


if __name__ == "__main__":
    unittest.main()
