from pathlib import Path
import subprocess
import sys
import unittest

import appconfig
from appconfig import Config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environ = {
            "MEDIAMTX_RTSP_PORT": "8554",
            "MEDIAMTX_HLS_PORT": "8888",
            "MEDIAMTX_RTMP_PORT": "1935",
            "STREAM_API_PORT": "8080",
            "LOG_LEVEL": "info",
        }

    def test_deployment_settings_load(self):
        config = Config.from_env(self.environ)
        self.assertEqual(config.rtsp_port, int(self.environ["MEDIAMTX_RTSP_PORT"]))
        self.assertEqual(config.log_level, "info")

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
    import mediastream
    import streamregistry
    import transports
    import runtimesettings
    import statefile
"""],
            cwd=Path(appconfig.__file__).resolve().parent, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
