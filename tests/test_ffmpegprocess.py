from pathlib import Path
import sys
import unittest

import ffmpegprocess
from testsupport import make_config, state_path


class ProcessLoggingTests(unittest.TestCase):
    def test_process_output_is_captured_and_tail_is_reported(self):
        unique_path = state_path(self)
        name = unique_path.stem
        config = make_config(log_level="info", ffmpeg_log_dir=unique_path.parent)
        log_path = config.ffmpeg_log_dir / f"{name}.rtsp.log"
        self.addCleanup(log_path.unlink, missing_ok=True)
        process = ffmpegprocess.launch_output(
            [sys.executable, "-c", "import sys; print('first'); print('failure', file=sys.stderr)"],
            name, "rtsp", config)
        self.addCleanup(self.reap, process)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(set(log_path.read_text().splitlines()), {"first", "failure"})
        reason, detail = ffmpegprocess.ffmpeg_death_reason(name, "rtsp", config)
        self.assertIn(reason, {"first", "failure"})
        self.assertEqual(set(detail.splitlines()), {"first", "failure"})

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
