import logging
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ffmpegprocess import FFmpegProcess


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.Logger('process-test')
        self.logger.addHandler(logging.NullHandler())

    def run_until_exit(self, code):
        run = FFmpegProcess(self.logger)
        self.addCleanup(run.stop)
        run.start([sys.executable, '-c', code])
        deadline = time.monotonic() + 5
        while run.observe()['returncode'] is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(run.stop())
        return run.observe()

    def test_large_unterminated_output_cannot_block_child_and_tail_is_bounded(self):
        result = self.run_until_exit("import sys; sys.stdout.write('x'*1000000); sys.stdout.flush(); print('stdout-tail'); print('stderr-tail', file=sys.stderr)")
        self.assertEqual(result['returncode'], 0)
        self.assertLessEqual(len(result['diagnostics'].encode()), FFmpegProcess.TAIL_BYTES)
        self.assertIn('stdout-tail', result['diagnostics'])
        self.assertIn('stderr-tail', result['diagnostics'])

    def test_broken_logger_does_not_prevent_cleanup(self):
        with patch.object(self.logger, 'info', side_effect=OSError('disk full')):
            self.assertEqual(self.run_until_exit("print('x'*1000000)")['returncode'], 0)

    def test_spawn_failure_closes_capture_and_propagates(self):
        run = FFmpegProcess(self.logger)
        with patch('ffmpegprocess.subprocess.Popen', side_effect=OSError('no executable')):
            with self.assertRaises(OSError):
                run.start(['unused'])
        self.assertIsNone(run._capture)
        self.assertTrue(run.stop())

    def test_terminates_child_and_does_not_close_borrowed_logger(self):
        run = FFmpegProcess(self.logger)
        self.addCleanup(run.stop)
        run.start([sys.executable, '-c', 'import time; time.sleep(30)'])
        with patch.object(self.logger.handlers[0], 'close') as close:
            self.assertTrue(run.stop())
            close.assert_not_called()
        self.assertIsNotNone(run.observe()['returncode'])
        self.assertIsNone(run._capture)
        with self.assertRaises(RuntimeError):
            run.start(['unused'])

    def test_kill_escalation_and_timeout_retain_handle(self):
        with patch('ffmpegprocess.subprocess.Popen') as popen:
            child = popen.return_value
            child.poll.return_value = None
            child.wait.side_effect = subprocess.TimeoutExpired('child', .01)
            run = FFmpegProcess(self.logger)
            run.start(['child'])
            self.assertFalse(run.stop())
            child.terminate.assert_called_once()
            child.kill.assert_called_once()
            self.assertIsNotNone(run._capture)
            child.poll.return_value = -9
            self.assertTrue(run.stop())
            self.assertIsNone(run._capture)
