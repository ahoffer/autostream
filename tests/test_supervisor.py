from concurrent.futures import Future
from queue import Queue
import threading
import unittest
from unittest.mock import patch

import ffmpegcmd
from streamsupport import FakeProcess, create, inventory, setup_registry
from stream_supervisor import run_loop


class SequentialSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source, _ = setup_registry(self)
        self.rtsp = create(self.registry, 'boats', self.source, 'rtsp')
        self.udp = create(self.registry, 'boats-udp', self.source, 'udp')

    def test_stop_queued_during_prepare_executes_after_start_and_prevents_recovery(self):
        self.rtsp.stop_streaming()
        queue, stop = Queue(), threading.Event()
        entered, release = threading.Event(), threading.Event()
        prepare = ffmpegcmd.prepare
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release preparation')
            return prepare(*args)
        snapshot = []
        def publish():
            snapshot[:] = self.registry.status()
        started, stopped = Future(), Future()
        queue.put((self.rtsp.start_streaming, started))
        with patch('ffmpegcmd.prepare', side_effect=slow):
            thread = threading.Thread(target=run_loop, args=(self.registry, queue, publish, stop))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                queue.put((self.rtsp.stop_streaming, stopped))
                self.assertFalse(stopped.done())
                release.set()
                self.assertTrue(started.result(timeout=2))
                self.assertTrue(stopped.result(timeout=2))
                self.assertFalse(next(row for row in snapshot if row['name'] == 'boats')['enabled'])
            finally:
                release.set()
                stop.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(FakeProcess.runs), 3)  # A brief start before Stop is intentional.
        self.assertTrue(all(run.code is not None for run in FakeProcess.runs))
        self.assertTrue(self.store.load()['streams']['boats-udp']['enabled'])

    def test_queued_lookup_of_a_deleted_stream_finds_nothing(self):
        from streamapi import _execute_request
        self.assertTrue(self.registry.delete_stream('boats'))
        response, code = _execute_request(self.registry, 'start', 'boats', {})
        self.assertEqual(code, 404)
        self.assertIn('not found', response['error'])

    def test_file_observation_on_shared_queue_runs_on_owner_and_parks_its_streams(self):
        queue, stop = Queue(), threading.Event()
        applied = threading.Event()
        seen = []
        def publish():
            seen.append((threading.get_ident(), self.registry.status()))
            applied.set()
        self.source.unlink()
        queue.put((inventory(deletes=(self.source,)), None))
        thread = threading.Thread(target=run_loop, args=(self.registry, queue, publish, stop))
        thread.start()
        try:
            self.assertTrue(applied.wait(2))
            owner, rows = seen[0]
            self.assertEqual(owner, thread.ident)
            self.assertEqual({(row['state'], row['reason']) for row in rows}, {('waiting', 'source file missing')})
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
