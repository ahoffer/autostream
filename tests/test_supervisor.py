from concurrent.futures import Future
from functools import partial
from queue import Queue
import threading
import unittest
from unittest.mock import patch

import ffmpegcmd
from outputregistry import stream_name_for
from outputsupport import FakeProcess, setup_outputs
from stream_supervisor import run_loop


class SequentialSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source = setup_outputs(self)

    def test_stop_queued_during_prepare_executes_after_start_and_prevents_recovery(self):
        output = self.registry.get_output(stream_name_for(self.source) + '/rtsp')
        output.stop_streaming()
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
        queue.put((output.start_streaming, started))
        with patch('ffmpegcmd.prepare', side_effect=slow):
            thread = threading.Thread(target=run_loop, args=(self.registry, queue, publish, stop))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                queue.put((output.stop_streaming, stopped))
                self.assertFalse(stopped.done())
                release.set()
                self.assertTrue(started.result(timeout=2))
                self.assertTrue(stopped.result(timeout=2))
                self.assertFalse(next(row for row in snapshot if row['id'].endswith('/rtsp'))['enabled'])
            finally:
                release.set()
                stop.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(FakeProcess.runs), 3)  # A brief start before Stop is intentional.
        self.assertTrue(all(run.code is not None for run in FakeProcess.runs))
        self.assertTrue(self.store.load()['outputs'][stream_name_for(self.source) + '/udp']['enabled'])

    def test_queued_lookup_after_removal_does_not_keep_live_output_reference(self):
        from streamapi import _execute_request
        from outputsupport import inventory
        self.registry.supervise(inventory(deletes=(self.source,)))
        response, code = _execute_request(self.registry, 'start', stream_name_for(self.source) + '/rtsp', {})
        self.assertEqual(code, 404)
        self.assertIn('not found', response['error'])

    def test_file_observation_on_shared_queue_runs_on_owner_and_removes_outputs(self):
        from outputsupport import inventory
        queue, stop = Queue(), threading.Event()
        applied = threading.Event()
        seen = []
        def publish():
            seen.append((threading.get_ident(), self.registry.status()))
            applied.set()
        queue.put((inventory(deletes=(self.source,)), None))
        thread = threading.Thread(target=run_loop, args=(self.registry, queue, publish, stop))
        thread.start()
        try:
            self.assertTrue(applied.wait(2))
            self.assertEqual(seen[0], (thread.ident, []))
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
