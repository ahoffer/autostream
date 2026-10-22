"""HTTP transport: resolve an output and request its behavior directly."""
from __future__ import annotations

import json
import logging
from functools import partial
from concurrent.futures import Future
from queue import Full
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs


log = logging.getLogger(__name__)

INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"


class StreamHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, requests, read_status, execute, **kwargs):
        self.requests = requests
        self.read_status = read_status
        self.execute = execute
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass  # Suppress default access logging; we have our own.

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/' or parsed.path == '/index.html':
            try:
                html = INDEX_HTML_PATH.read_text(encoding='utf-8')
            except OSError as error:
                self.send_json({"error": f"index.html unavailable: {error}"}, 500)
                return
            self.send_html(html)
        elif parsed.path == '/api/streams':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(self.read_status())
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip('/').split('/')
        if parts[:2] != ['api', 'streams']:
            self.send_json({'error': 'Not found'}, 404)
            return
        if len(parts) == 3 and parts[2] in ('start-all', 'stop-all'):
            self._submit(parts[2])
            return
        if len(parts) == 4:
            self.send_json({'error': 'Choose an output: /api/streams/{name}/outputs/{rtsp|udp}/{action}'}, 410)
            return
        if len(parts) != 6 or parts[3] != 'outputs':
            self.send_json({'error': 'Not found'}, 404)
            return
        action = parts[5]
        changes = {}
        if action == 'settings':
            changes, error = parse_settings(parse_qs(parsed.query, keep_blank_values=True))
            if error:
                self.send_json({'error': error}, 400)
                return
        elif action not in ('start', 'stop'):
            self.send_json({'error': 'Unknown action'}, 400)
            return
        self._submit(action, f'{parts[2]}/{parts[4]}', changes)

    def _submit(self, action, output_id=None, changes=None):
        future = Future()
        operation = partial(self.execute, action, output_id, changes)
        try:
            self.requests.put_nowait((operation, future))
        except Full:
            self.send_json({'error': 'Supervisor queue is full; request was not accepted'}, 503)
            return
        try:
            data, status = future.result()
        except Exception as error:
            log.exception('Output request failed')
            data, status = {'success': False, 'error': str(error)}, 500
        self.send_json(data, status)


def _execute_request(registry, action, output_id, changes):
    """Runs on the supervisor thread, never on the submitting HTTP thread."""
    if action == 'start-all':
        success = registry.start_all_outputs()
    elif action == 'stop-all':
        success = registry.stop_all_outputs()
    else:
        output = registry.get_output(output_id)
        if output is None:
            return {'error': 'Output not found'}, 404
        if action == 'start':
            success = output.start_streaming()
        elif action == 'stop':
            success = output.stop_streaming()
        else:
            success = output.update_settings(changes)
    if not success:
        return {'success': False, 'error': 'Operation could not finish; check output status'}, 409
    return {'success': True}, 200


def parse_settings(query):
    """The settings named in a ?loop=&mode=&gop=&fps= query as update_settings
    a mapping, or an error message. An absent key leaves that setting
    alone; fps=auto returns to the file's own rate and gop=auto to one second
    at the output rate."""
    settings = {}
    if 'loop' in query:
        try:
            settings['loop_count'] = int(query['loop'][0])
        except ValueError:
            return None, "Invalid loop count"
        if settings['loop_count'] < -1:
            return None, "Invalid loop count"
    if 'mode' in query:
        if query['mode'][0] not in ('passthrough', 'transcode'):
            return None, "Invalid mode"
        settings['mode'] = query['mode'][0]
    if 'gop' in query:
        if query['gop'][0] == 'auto':
            settings['gop_frames'] = None
        else:
            try:
                settings['gop_frames'] = int(query['gop'][0])
            except ValueError:
                settings['gop_frames'] = 0
            if settings['gop_frames'] < 1:
                return None, "GOP length must be a whole number of frames, 1 or more, or auto"
    if 'fps' in query:
        if query['fps'][0] == 'auto':
            settings['frame_rate'] = None
        else:
            try:
                settings['frame_rate'] = float(query['fps'][0])
            except ValueError:
                settings['frame_rate'] = 0
            # Keep frame-rate validation consistent with the control UI.
            if not 1 <= settings['frame_rate'] <= 120:
                return None, "FPS must be a number from 1 to 120, or auto"
    return settings, None
