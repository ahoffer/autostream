from pathlib import Path
import unittest
from unittest.mock import patch

import statefile
from streammodel import Stream
from streamregistry import StreamRegistry
from testsupport import make_config, state_path


class UdpPortAllocationTests(unittest.TestCase):
    def setUp(self):
        self.registry = StreamRegistry(make_config(udp_last_port=40001))
        self.enterContext(patch.object(self.registry, "persist"))
        saved = statefile.PersistedState(
            {"b": 40000, "c": 40001},
            {
                "b": statefile.PersistedStream("b.mp4", mode="transcode", removed_at=10),
                "c": statefile.PersistedStream("c.mp4", removed_at=20),
            },
        )
        with patch.object(statefile, "load_state", return_value=saved):
            self.registry.load_persisted_state()
        self.scan = self.enterContext(patch.object(Path, "iterdir", return_value=[]))

    def test_startup_preserves_ports_for_files_not_yet_registered(self):
        self.scan.return_value = [Path(name + ".mp4") for name in ("a", "b", "c")]
        with self.registry.lock:
            for name in ("a", "b", "c"):
                self.registry.claim_slot(name, name + ".mp4")
        self.assertEqual(
            {name: stream.udp_port for name, stream in self.registry.streams.items()},
            {"a": None, "b": 40000, "c": 40001},
        )
        self.assertEqual(self.registry.streams["b"].mode, "transcode")

    def test_reclaims_oldest_absent_file(self):
        with self.registry.lock:
            self.assertEqual(self.registry.udp_port_for("a"), 40000)
        self.assertEqual(self.registry.udp_ports, {"a": 40000, "c": 40001})
        self.assertNotIn("b", self.registry.retained_streams)

    def test_active_slot_keeps_port_even_if_saved_filename_is_absent(self):
        self.registry.streams["b"] = Stream("b", "B.mp4", 40000, config=self.registry.config)
        self.scan.return_value = [Path("B.mp4")]
        with self.registry.lock:
            self.assertEqual(self.registry.udp_port_for("a"), 40001)
        self.assertEqual(self.registry.udp_ports, {"b": 40000, "a": 40001})

    def test_directory_scan_failure_preserves_assignments(self):
        self.scan.side_effect = PermissionError("Cannot read videos")
        with self.registry.lock:
            self.assertIsNone(self.registry.udp_port_for("a"))
        self.assertEqual(self.registry.udp_ports, {"b": 40000, "c": 40001})
        self.assertEqual(set(self.registry.retained_streams), {"b", "c"})


class RegistryPersistenceTests(unittest.TestCase):
    def test_removed_stream_restores_settings_and_port_after_restart(self):
        config = make_config(state_path=state_path(self))
        registry = StreamRegistry(config)
        with registry.lock:
            registry.claim_slot("demo", "demo.mp4")
            stream = registry.streams.pop("demo")
            stream.mode = "transcode"
            stream.loop_count = 2
            stream.gop_frames = 12
            stream.frame_rate = 24
            stream.udp.enabled = False
            registry.retain_removed(stream, removed_at=123)

        restored = StreamRegistry(config)
        restored.load_persisted_state()
        with restored.lock:
            restored.claim_slot("demo", "demo.mp4")
        stream = restored.streams["demo"]
        self.assertEqual((stream.udp_port, stream.mode, stream.loop_count,
                          stream.gop_frames, stream.frame_rate),
                         (40000, "transcode", 2, 12, 24))
        self.assertTrue(stream.rtsp.enabled)
        self.assertFalse(stream.udp.enabled)
        self.assertNotIn("demo", restored.retained_streams)
        self.assertEqual(statefile.load_state(config.state_path).streams["demo"].removed_at, None)


if __name__ == "__main__":
    unittest.main()
