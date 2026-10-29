"""HTTP transport: validate requests, queue mutations, serve snapshots and HLS segments."""
from __future__ import annotations

import json
import logging
from functools import partial
from concurrent.futures import Future
from queue import Full
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from runtimesettings import RuntimeSettings
from statefile import STREAM_NAME
from streamresponses import describe_streams, describe_transports
from transports import KINDS_BY_ID

log = logging.getLogger(__name__)

INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"
FONT_PATH = Path(__file__).resolve().parent / "jost.woff2"   # the UI's typeface, served so the page works offline
HLS_CONTENT_TYPES = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t'}


class StreamHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, requests, read_snapshot, execute, url_facts, hls_dir, host_addresses=None, **kwargs):
        self.requests = requests
        self.read_snapshot = read_snapshot
        self.execute = execute
        self.url_facts = url_facts
        self.hls_dir = Path(hls_dir)
        self.host_addresses = host_addresses or {'hostname': '', 'lan_ip': ''}
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
            snapshot = self.read_snapshot()
            host = self.url_facts.host_for(snapshot.settings.advertised_host, self.headers.get('Host'))
            self.send_json(describe_streams(snapshot.statuses, snapshot.settings, host, self.url_facts))
        elif parsed.path == '/api/videos':
            self.send_json(list(self.read_snapshot().videos))
        elif parsed.path == '/api/transports':
            self.send_json(describe_transports())
        elif parsed.path == '/api/settings':
            self.send_json(self.read_snapshot().settings.to_document())
        elif parsed.path == '/api/host':
            self.send_json(self.host_addresses)
        elif parsed.path == '/jost.woff2':
            self._send_font()
        elif parsed.path.startswith('/hls/'):
            self._send_hls_file(parsed.path[len('/hls/'):])
        else:
            self.send_json({"error": "Not found"}, 404)

    def _send_font(self):
        try:
            data = FONT_PATH.read_bytes()
        except OSError:
            self.send_json({'error': 'Not found'}, 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'font/woff2')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def _send_hls_file(self, relative):
        """A playlist or segment written by an HLS stream; nothing outside the HLS directory."""
        root = self.hls_dir.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target.suffix not in HLS_CONTENT_TYPES or not target.is_file():
            self.send_json({'error': 'Not found'}, 404)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.send_json({'error': 'Not found'}, 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', HLS_CONTENT_TYPES[target.suffix])
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip('/').split('/')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parts == ['api', 'settings']:
            settings, error = RuntimeSettings.from_query(query)
            if error:
                self.send_json({'error': error}, 400)
                return
            self._submit('runtime-settings', changes=settings)
            return
        if parts[:2] != ['api', 'streams']:
            self.send_json({'error': 'Not found'}, 404)
            return
        if len(parts) == 2:
            creation, error = parse_creation(query)
            if error:
                self.send_json({'error': error}, 400)
                return
            self._submit('create', creation['name'], creation)
            return
        if len(parts) == 3 and parts[2] in ('start-all', 'stop-all'):
            self._submit(parts[2])
            return
        if len(parts) != 4:
            self.send_json({'error': 'Not found'}, 404)
            return
        name, action = parts[2], parts[3]
        changes = {}
        if action == 'settings':
            changes, error = parse_settings(query)
            if error:
                self.send_json({'error': error}, 400)
                return
        elif action not in ('start', 'stop', 'delete'):
            self.send_json({'error': 'Unknown action'}, 400)
            return
        self._submit(action, name, changes)

    def _submit(self, action, name=None, changes=None):
        future = Future()
        operation = partial(self.execute, action, name, changes)
        try:
            self.requests.put_nowait((operation, future))
        except Full:
            self.send_json({'error': 'Supervisor queue is full; request was not accepted'}, 503)
            return
        try:
            data, status = future.result()
        except Exception as error:
            log.exception('Stream request failed')
            data, status = {'success': False, 'error': str(error)}, 500
        self.send_json(data, status)


def _execute_request(registry, action, name, changes):
    """Runs on the supervisor thread, never on the submitting HTTP thread."""
    if action == 'runtime-settings':
        success = registry.apply_settings(changes)
    elif action == 'create':
        success, error = registry.create_stream(**changes)
        if not success:
            return {'error': error}, 400
    elif action == 'start-all':
        success = registry.start_all_streams()
    elif action == 'stop-all':
        success = registry.stop_all_streams()
    else:
        stream = registry.get_stream(name)
        if stream is None:
            return {'error': 'Stream not found'}, 404
        if action == 'start':
            success = stream.start_streaming()
        elif action == 'stop':
            success = stream.stop_streaming()
        elif action == 'delete':
            success = registry.delete_stream(name)
        else:
            new_name = changes.get('name', name)
            if new_name != name and registry.get_stream(new_name) is not None:
                return {'error': f'A stream named {new_name} exists'}, 400
            success = registry.update_stream(name, changes)
    if not success:
        return {'success': False, 'error': 'Operation could not finish; check the stream status'}, 409
    return {'success': True}, 200


def parse_creation(query):
    """A new stream from a ?name=&file=&transport= query plus its settings; start=0 creates it stopped."""
    name = (query.get('name') or [''])[0].strip()
    filename = (query.get('file') or [''])[0].strip()
    transport = (query.get('transport') or [''])[0].strip()
    if not name:
        return None, 'Name the stream'
    if not filename:
        return None, 'Choose a video file'
    if transport not in KINDS_BY_ID:
        return None, 'Choose a transport'
    choices, error = parse_settings(query)
    if error:
        return None, error
    # The name, file and transport are the identity and binding, not choices; parse_settings read them for the edit route.
    choices = {key: value for key, value in choices.items() if key not in ('name', 'filename', 'transport')}
    start = (query.get('start') or ['1'])[0] != '0'
    return {'name': name, 'filename': filename, 'transport': transport, 'choices': choices, 'start': start}, None


def parse_settings(query):
    """The settings named in a ?name=&file=&transport=&loop=&mode=&gop=&fps= query
    as a changes mapping, or an error message. An absent key leaves that setting
    alone; fps=auto returns to the file's own rate and gop=auto to one second
    at the output rate."""
    settings = {}
    if 'name' in query:
        name = query['name'][0].strip()
        if not STREAM_NAME.match(name):
            return None, 'Stream name: letters, digits, - and _ only, 64 at most'
        settings['name'] = name
    if 'file' in query:
        filename = query['file'][0].strip()
        if not filename or '/' in filename or filename.startswith('.'):
            return None, "Choose a video file"
        settings['filename'] = filename
    if 'transport' in query:
        if query['transport'][0] not in KINDS_BY_ID:
            return None, "Unknown transport"
        settings['transport'] = query['transport'][0]
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
