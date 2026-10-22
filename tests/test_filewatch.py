from pathlib import Path
from queue import Queue, Empty
import threading
import unittest
from unittest.mock import patch

from filewatch import FileChangeDebouncer, watch
from stream_supervisor import _watch_files


class WatcherTests(unittest.TestCase):
    def test_growth_resets_stability_and_startup_uses_same_check(self):
        watcher = FileChangeDebouncer(2)
        self.assertFalse(watcher.poll({'a.mp4': (1, 10)})['stable'])
        self.assertFalse(watcher.poll({'a.mp4': (1, 10)})['stable'])
        self.assertFalse(watcher.poll({'a.mp4': (2, 20)})['stable'])
        watcher.poll({'a.mp4': (2, 20)})
        batch = watcher.poll({'a.mp4': (2, 20)})
        self.assertEqual(batch['creates'], ('a.mp4',))
        self.assertEqual(batch['stable'], ('a.mp4',))
        self.assertEqual(watcher.poll({})['deletes'], ('a.mp4',))

    def test_scan_error_preserves_known_files(self):
        stop = threading.Event()
        snapshots = [{'a.mp4': (1, 10)}, {'a.mp4': (1, 10)}, OSError('offline'), {'a.mp4': (1, 10)}, {}]
        with patch('filewatch._scan', side_effect=snapshots):
            generator = watch(Path('/unused'), lambda path: False, .001, .001, stop_event=stop)
            next(generator)
            self.assertEqual(next(generator)['creates'], ('a.mp4',))
            self.assertFalse(next(generator)['deletes'])
            self.assertEqual(next(generator)['deletes'], ('a.mp4',))
            generator.close()

    def test_full_queue_preserves_event_and_shutdown_interrupts_wait(self):
        queue = Queue(maxsize=1)
        queue.put(('occupied', None))
        stop = threading.Event()
        observed = threading.Event()
        def batches(*args, **kwargs):
            observed.set()
            yield {'creates': ('a.mp4',)}
        with patch('filewatch.watch', side_effect=batches):
            thread = threading.Thread(target=_watch_files, args=(Path('/unused'), queue, stop))
            thread.start()
            self.assertTrue(observed.wait(2))
            queue.get()
            self.assertEqual(queue.get(timeout=2)[0]['creates'], ('a.mp4',))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            queue.put(('occupied', None))
            thread = threading.Thread(target=_watch_files, args=(Path('/unused'), queue, stop))
            thread.start()
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_unexpected_failure_signals_shutdown(self):
        stop = threading.Event()
        with patch('filewatch.watch', side_effect=RuntimeError('broken watcher')):
            _watch_files(Path('/unused'), Queue(), stop)
        self.assertTrue(stop.is_set())
