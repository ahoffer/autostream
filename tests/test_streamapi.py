from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest

from streamapi import StreamHandler
from streams import StreamManager
from testsupport import make_config, state_path


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = StreamManager(make_config(state_path=state_path(self)))
        with self.manager.registry.lock:
            self.manager.registry.claim_slot("demo", "demo.mp4")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(StreamHandler, streams=self.manager))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.close_server, thread)

    def close_server(self, thread):
        self.server.shutdown()
        thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_routes_read_and_mutate_the_supplied_manager(self):
        status, body = self.request("POST", "/api/streams/demo/settings?mode=transcode&gop=12&fps=24")
        self.assertEqual((status, body), (200, {"success": True}))
        status, body = self.request("POST", "/api/streams/demo/outputs/udp/stop")
        self.assertEqual((status, body), (200, {"success": True}))
        status, body = self.request("GET", "/api/streams")
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["name"], "demo")
        self.assertEqual(body[0]["gop_frames"], 12)
        self.assertEqual(body[0]["frame_rate"], 24)
        self.assertEqual(body[0]["outputs"]["udp"]["state"], "stopped")
        self.assertEqual(body[0]["rtsp_url"], "rtsp://autostream:8554/demo")
        self.assertEqual(self.request("POST", "/api/streams/stop-all"), (200, {"success": True}))
        self.assertFalse(self.manager.registry.streams["demo"].rtsp.enabled)

    def test_invalid_routes_and_settings_do_not_change_state(self):
        self.assertEqual(self.request("POST", "/api/streams/missing/start")[0], 404)
        self.assertEqual(self.request("POST", "/api/streams/demo/outputs/hls/start")[0], 404)
        self.assertEqual(self.request("POST", "/api/streams/demo/settings?fps=0")[0], 400)
        self.assertIsNone(self.manager.registry.streams["demo"].frame_rate)

    def test_managers_do_not_share_registry_configuration_or_reachability(self):
        other = StreamManager(make_config(
            state_path=state_path(self), hostname="other", udp_base_port=45000,
            udp_last_port=45001))
        with other.registry.lock:
            other.registry.claim_slot("demo", "other.mp4")
        other.output_reachable = True
        other.disable_output("demo", "rtsp")
        self.assertEqual(other.get_stream_status()[0]["udp_url"], "udp://video-streaming:45000")
        _, status = self.request("GET", "/api/streams")
        self.assertEqual(status[0]["filename"], "demo.mp4")
        self.assertEqual(status[0]["outputs"]["udp"]["state"], "waiting")
        self.assertTrue(status[0]["outputs"]["rtsp"]["enabled"])
