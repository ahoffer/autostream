from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from queue import Queue
import threading
import unittest

from outputregistry import stream_name_for
from outputsupport import setup_outputs
from streamapi import StreamHandler, _execute_request
from streamresponses import group_outputs
from stream_supervisor import run_loop


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source = setup_outputs(self)
        self.name = stream_name_for(self.source)
        self.queue = Queue(maxsize=8)
        self.stop = threading.Event()
        self.snapshot = b'[]'
        self.execution_threads = []
        def publish():
            self.snapshot = json.dumps(group_outputs(self.registry.status(),
                {'rtsp': 'rtsp://test:8554', 'hls': 'http://test:8888'})).encode()
        def execute(*args):
            self.execution_threads.append(threading.get_ident())
            return _execute_request(self.registry, *args)
        publish()
        self.owner = threading.Thread(target=run_loop, args=(self.registry, self.queue, publish, self.stop))
        self.owner.start()
        handler = partial(StreamHandler, requests=self.queue, read_status=lambda: self.snapshot, execute=execute)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.http = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.http.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.stop.set()
        self.owner.join(3)
        self.assertFalse(self.owner.is_alive())

    def request(self, method, path):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read().decode()
            return response.status, json.loads(body) if response.getheader('Content-Type') == 'application/json' else body
        finally:
            connection.close()

    def test_settings_are_output_specific_and_execute_on_supervisor(self):
        base = f'/api/streams/{self.name}'
        status, body = self.request('POST', base + '/outputs/rtsp/settings?mode=transcode&fps=12&loop=0')
        self.assertEqual((status, body), (200, {'success': True}))
        _, groups = self.request('GET', '/api/streams')
        outputs = groups[0]['outputs']
        self.assertEqual(outputs['rtsp']['frame_rate'], 12)
        self.assertEqual(outputs['udp']['frame_rate'], 30)
        self.assertEqual(self.execution_threads, [self.owner.ident])
        self.assertNotIn('frame_rate', groups[0])
        self.assertEqual(self.request('POST', base + '/settings?loop=1')[0], 410)

    def test_invalid_settings_leave_output_unchanged(self):
        for query in ('fps=nan', 'fps=121', 'loop=-2', 'gop=0', 'mode=unknown', 'fps='):
            self.assertEqual(self.request('POST', f'/api/streams/{self.name}/outputs/rtsp/settings?{query}')[0], 400)
        self.assertEqual(self.execution_threads, [])

    def test_routes_start_stop_all_and_serve_ui(self):
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('POST', '/api/streams/stop-all')[1], {'success': True})
        outputs = self.request('GET', '/api/streams')[1][0]['outputs'].values()
        self.assertFalse(any(output['enabled'] for output in outputs))
        self.assertEqual(self.request('POST', '/api/streams/start-all')[1], {'success': True})
        outputs = self.request('GET', '/api/streams')[1][0]['outputs'].values()
        self.assertTrue(all(output['running'] for output in outputs))
        self.assertEqual(self.request('POST', '/api/streams/absent/outputs/rtsp/start')[0], 404)

    def test_full_queue_rejects_mutation_but_cached_get_still_responds(self):
        from concurrent.futures import Future
        entered, release = threading.Event(), threading.Event()
        def slow_operation():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release work')
            return {'success': True}, 200
        self.queue.put((slow_operation, Future()))
        self.assertTrue(entered.wait(2))
        try:
            for _ in range(self.queue.maxsize):
                self.queue.put_nowait((lambda: ({'success': True}, 200), Future()))
            self.assertEqual(self.request('POST', '/api/streams/stop-all')[0], 503)
            self.assertEqual(self.request('GET', '/api/streams')[0], 200)
        finally:
            release.set()

    def test_failed_save_still_publishes_actual_stop(self):
        from unittest.mock import patch
        with patch.object(self.store, 'save_output', side_effect=OSError('disk full')):
            status, body = self.request('POST', f'/api/streams/{self.name}/outputs/rtsp/stop')
        self.assertEqual(status, 500)
        self.assertIn('disk full', body['error'])
        rtsp = self.request('GET', '/api/streams')[1][0]['outputs']['rtsp']
        self.assertFalse(rtsp['enabled'])
        self.assertFalse(rtsp['running'])
