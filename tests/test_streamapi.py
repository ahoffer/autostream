from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from queue import Queue
import threading
import unittest

from streamapi import StreamHandler, _execute_request
from streamresponses import StatusSnapshot, UrlFacts
from streamsupport import create, setup_registry
from stream_supervisor import run_loop


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.store, self.resolver, self.source, self.config = setup_registry(self)
        create(self.registry, 'boats', self.source, 'rtsp')
        create(self.registry, 'boats-udp', self.source, 'udp')
        self.queue = Queue(maxsize=8)
        self.stop = threading.Event()
        self.snapshot = None
        self.execution_threads = []
        def publish():
            self.snapshot = StatusSnapshot(tuple(self.registry.status()), self.registry.settings(), self.registry.videos())
        def execute(*args):
            self.execution_threads.append(threading.get_ident())
            return _execute_request(self.registry, *args)
        publish()
        self.owner = threading.Thread(target=run_loop, args=(self.registry, self.queue, publish, self.stop))
        self.owner.start()
        handler = partial(StreamHandler, requests=self.queue, read_snapshot=lambda: self.snapshot, execute=execute,
                          url_facts=UrlFacts(8554, 8888, 1935, 8080), hls_dir=self.config.hls_dir)
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

    def fetch(self, method, path, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            connection.close()

    def request(self, method, path, headers=None):
        status, content_type, body = self.fetch(method, path, headers)
        return status, json.loads(body) if content_type == 'application/json' else body.decode()

    def stream(self, name, headers=None):
        return next(row for row in self.request('GET', '/api/streams', headers)[1] if row['name'] == name)

    def test_create_list_and_delete_a_stream(self):
        status, body = self.request('POST', f'/api/streams?name=boats-srt&file={self.source.name}&transport=srt_listen&mode=transcode&fps=15')
        self.assertEqual((status, body), (200, {'success': True}))
        row = self.stream('boats-srt', headers={'Host': 'operator-console:9080'})
        self.assertEqual((row['transport'], row['port'], row['frame_rate'], row['running']), ('srt_listen', 41000, 15, True))
        self.assertEqual(row['register'], [['register', 'srt://operator-console:41000']])
        self.assertEqual(row['delivery'], 'srt://0.0.0.0:41000?mode=listener')
        self.assertEqual(self.request('POST', '/api/streams/boats-srt/delete'), (200, {'success': True}))
        self.assertNotIn('boats-srt', [row['name'] for row in self.request('GET', '/api/streams')[1]])
        self.assertEqual(self.execution_threads, [self.owner.ident] * 2)

    def test_register_urls_follow_the_host_header_unless_an_advertised_host_is_set(self):
        row = self.stream('boats', headers={'Host': 'operator-console:9080'})
        self.assertEqual(row['register'], [['rtsp', 'rtsp://operator-console:8554/boats'],
                                           ['hls', 'http://operator-console:8888/boats/index.m3u8'],
                                           ['rtmp', 'rtmp://operator-console:1935/boats']])
        self.assertEqual(self.stream('boats', headers={'Host': '[::1]:9080'})['register'][0][1], 'rtsp://[::1]:8554/boats')
        self.assertEqual(self.request('POST', '/api/settings?advertised_host=publisher-host&destination_host=receiver')[0], 200)
        self.assertEqual(self.stream('boats', headers={'Host': 'operator-console:9080'})['register'][0][1], 'rtsp://publisher-host:8554/boats')
        self.assertEqual(self.request('GET', '/api/settings')[1]['advertised_host'], 'publisher-host')

    def test_stream_settings_can_change_name_file_transport_and_encoding_together(self):
        status, _ = self.request('POST', '/api/streams/boats-udp/settings?transport=tcp_listen&loop=1')
        self.assertEqual(status, 200)
        row = self.stream('boats-udp')
        self.assertEqual((row['transport'], row['port'], row['loop_count']), ('tcp_listen', 42000, 1))
        self.assertEqual(self.request('POST', '/api/streams/boats-udp/settings?name=boats-tcp')[0], 200)
        self.assertEqual(self.stream('boats-tcp')['port'], 42000)
        status, body = self.request('POST', '/api/streams/boats-tcp/settings?name=boats')
        self.assertEqual(status, 400)
        self.assertIn('exists', body['error'])
        self.assertEqual(self.request('POST', '/api/streams/absent/settings?loop=1')[0], 404)

    def test_invalid_requests_are_rejected_before_queueing(self):
        for path in (f'/api/streams?name=&file={self.source.name}&transport=udp',
                     '/api/streams?name=x&file=&transport=udp',
                     f'/api/streams?name=x&file={self.source.name}&transport=pigeon',
                     f'/api/streams?name=x&file={self.source.name}&transport=udp&fps=nan',
                     '/api/streams/boats/settings?transport=pigeon',
                     '/api/streams/boats/settings?file=../etc/passwd',
                     '/api/streams/boats/settings?name=bad%20name',
                     '/api/settings?udp_first_port=abc',
                     '/api/settings?multicast_group=10.0.0.1'):
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path)[0], 400)
        self.assertEqual(self.execution_threads, [])
        status, body = self.request('POST', f'/api/streams?name=boats&file={self.source.name}&transport=udp')
        self.assertEqual(status, 400)
        self.assertIn('exists', body['error'])

    def test_videos_transports_and_ui_are_served(self):
        self.assertEqual(self.request('GET', '/api/videos')[1], [self.source.name])
        transports = self.request('GET', '/api/transports')[1]
        self.assertIn('srt_listen', [kind['id'] for kind in transports])
        self.assertTrue(all({'id', 'label', 'direction', 'carries_klv'} <= set(kind) for kind in transports))
        self.assertEqual(self.request('GET', '/')[0], 200)
        status, content_type, body = self.fetch('GET', '/jost.woff2')
        self.assertEqual((status, content_type, body[:4]), (200, 'font/woff2', b'wOF2'))
        self.assertEqual(self.request('POST', '/api/streams/stop-all')[1], {'success': True})
        self.assertFalse(any(row['enabled'] for row in self.request('GET', '/api/streams')[1]))
        self.assertEqual(self.request('POST', '/api/streams/start-all')[1], {'success': True})
        self.assertTrue(all(row['running'] for row in self.request('GET', '/api/streams')[1]))

    def test_hls_files_are_served_from_the_hls_directory_only(self):
        playlist = self.config.hls_dir / 'boats-hls' / 'index.m3u8'
        playlist.parent.mkdir(parents=True)
        playlist.write_text('#EXTM3U\n')
        (playlist.parent / 'seg00001.ts').write_bytes(b'\x47' * 188)
        (playlist.parent / 'notes.txt').write_text('private')
        status, content_type, body = self.fetch('GET', '/hls/boats-hls/index.m3u8')
        self.assertEqual((status, content_type, body), (200, 'application/vnd.apple.mpegurl', b'#EXTM3U\n'))
        self.assertEqual(self.fetch('GET', '/hls/boats-hls/seg00001.ts')[1], 'video/mp2t')
        self.assertEqual(self.fetch('GET', '/hls/boats-hls/notes.txt')[0], 404)
        self.assertEqual(self.fetch('GET', '/hls/../streams.json')[0], 404)
        self.assertEqual(self.fetch('GET', '/hls/missing/index.m3u8')[0], 404)

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
        with patch.object(self.store, 'save_stream', side_effect=OSError('disk full')):
            status, body = self.request('POST', '/api/streams/boats/stop')
        self.assertEqual(status, 500)
        self.assertIn('disk full', body['error'])
        row = self.stream('boats')
        self.assertFalse(row['enabled'])
        self.assertFalse(row['running'])
