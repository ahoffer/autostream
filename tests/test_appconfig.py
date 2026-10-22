from pathlib import Path
import subprocess
import sys
import unittest

from appconfig import Config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environ = dict(
            line.split("=", 1)
            for line in (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )

    def test_example_settings_load_and_optional_bitrate_can_be_empty(self):
        config = Config.from_env(self.environ)
        self.assertEqual(config.rtsp_port, int(self.environ["MEDIAMTX_RTSP_PORT"]))
        self.assertEqual(config.max_video_bps, 2_000_000)
        self.environ["MAX_VIDEO_BITRATE"] = ""
        self.assertIsNone(Config.from_env(self.environ).max_video_bps)

    def test_invalid_deployment_settings_fail_when_loaded(self):
        for field, value, message in (
            ("OUTPUT_HOST", "", "OUTPUT_HOST environment variable is not set"),
            ("LOG_LEVEL", "verbose", "LOG_LEVEL must be one of"),
            ("UDP_LAST_PORT", "1", "is below UDP_BASE_PORT"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, message):
                Config.from_env(self.environ | {field: value})
        with self.assertRaises(ValueError):
            Config.from_env(self.environ | {"MAX_VIDEO_BITRATE": "bad"})

    def test_imports_need_no_environment_or_filesystem_writes(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", """
import os
from pathlib import Path
from unittest.mock import patch
os.environ.clear()
with patch.object(Path, 'mkdir', side_effect=AssertionError('import wrote to disk')):
    import stream_supervisor
    import streamapi
    import mediaoutput
    import outputregistry
    import statefile
"""],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
