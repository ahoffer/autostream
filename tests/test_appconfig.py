from pathlib import Path
import subprocess
import sys
import unittest

import appconfig
from appconfig import Config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environ = dict(
            line.split("=", 1)
            for line in (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )

    def test_example_settings_load_and_carry_no_topology_or_policy(self):
        config = Config.from_env(self.environ)
        self.assertEqual(config.rtsp_port, int(self.environ["MEDIAMTX_RTSP_PORT"]))
        self.assertEqual(config.log_level, "info")
        for retired in ("OUTPUT_HOST", "UDP_BASE_PORT", "UDP_LAST_PORT", "MAX_VIDEO_BITRATE", "ADVERTISED_HOST"):
            self.assertNotIn(retired, self.environ)

    def test_invalid_deployment_settings_fail_when_loaded(self):
        for field, value, message in (
            ("MEDIAMTX_RTSP_PORT", "", "MEDIAMTX_RTSP_PORT environment variable is not set"),
            ("LOG_LEVEL", "verbose", "LOG_LEVEL must be one of"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, message):
                Config.from_env(self.environ | {field: value})

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
    import runtimesettings
    import statefile
"""],
            cwd=Path(appconfig.__file__).resolve().parent, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
