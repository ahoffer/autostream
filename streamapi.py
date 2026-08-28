"""HTTP control API and the web UI it serves.

The transport for the stream operations: routing, JSON encoding, status codes,
and the index.html page live here, so streams.py never has to know it is
reachable over HTTP.

Routes:
  GET    /                                        the UI
  GET    /api/streams                             status of every stream
  POST   /api/streams/start-all | stop-all
  POST   /api/streams/{name}/start?loop=&mode=    restart every output
  POST   /api/streams/{name}/stop                 stop every output
  POST   /api/streams/{name}/outputs/{out}/start  restart one output
  POST   /api/streams/{name}/outputs/{out}/stop   stop one output
  PUT    /api/streams/{name}/outputs/{out}/command  {"command": "ffmpeg ..."}
  DELETE /api/streams/{name}/outputs/{out}/command  back to the default
"""
from __future__ import annotations

import json
import logging
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import ffmpegcmd
import streams

log = logging.getLogger(__name__)

INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"


class StreamHandler(BaseHTTPRequestHandler):
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

    def read_json_body(self):
        """The request body as JSON, or None when absent or malformed."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(length)) if length else None
        except (ValueError, TypeError):
            return None

    def output_route(self):
        """(stream_name, output_name, action) for /api/streams/{n}/outputs/{o}/{action},
        or None when the path is not that shape or names an unknown stream/output."""
        parts = urlparse(self.path).path.strip('/').split('/')
        if len(parts) != 6 or parts[:2] != ['api', 'streams'] or parts[3] != 'outputs':
            return None
        stream_name, output_name, action = parts[2], parts[4], parts[5]
        if output_name not in ffmpegcmd.OUTPUT_NAMES or not streams.stream_exists(stream_name):
            return None
        return stream_name, output_name, action

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
            self.send_json(streams.get_stream_status())
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip('/').split('/')

        if path_parts[:2] != ['api', 'streams'] or len(path_parts) not in (3, 4, 6):
            self.send_json({"error": "Not found"}, 404)
            return

        if len(path_parts) == 3:
            # No action segment means a collective operation. Individual streams
            # always carry an action segment, so a stream that happens to be
            # named stop-all keeps its own start/stop routes.
            if path_parts[2] == 'stop-all':
                streams.stop_all_streams()
                self.send_json({"success": True})
            elif path_parts[2] == 'start-all':
                streams.start_all_streams()
                self.send_json({"success": True})
            else:
                self.send_json({"error": "Unknown action"}, 400)
            return

        if len(path_parts) == 6:
            route = self.output_route()
            if route is None:
                self.send_json({"error": "Not found"}, 404)
                return
            stream_name, output_name, action = route
            if action == 'start':
                self.send_json({"success": streams.start_output(stream_name, output_name)})
            elif action == 'stop':
                self.send_json({"success": streams.disable_output(stream_name, output_name)})
            else:
                self.send_json({"error": "Unknown action"}, 400)
            return

        stream_name, action = path_parts[2], path_parts[3]
        if action == 'start':
            if not streams.stream_exists(stream_name):
                self.send_json({"error": "Stream not found"}, 404)
                return
            query = parse_qs(parsed.query)
            try:
                loop_count = int(query.get('loop', ['-1'])[0])
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid loop count"}, 400)
                return
            if loop_count < -1:
                self.send_json({"error": "Invalid loop count"}, 400)
                return
            # Absent loop defaults to -1, but absent mode keeps the stored value:
            # mode has no universal default (it depends on the file's codec), so
            # a bare start must not reset a stream's choice.
            mode = query.get('mode', [None])[0]
            if mode is not None and mode not in ('passthrough', 'transcode'):
                self.send_json({"error": "Invalid mode"}, 400)
                return
            success = streams.start_stream(stream_name, loop_count, mode)
            self.send_json({"success": success})
        elif action == 'stop':
            success = streams.stop_stream(stream_name)
            self.send_json({"success": success})
        else:
            self.send_json({"error": "Unknown action"}, 400)

    def do_PUT(self):
        route = self.output_route()
        if route is None or route[2] != 'command':
            self.send_json({"error": "Not found"}, 404)
            return
        stream_name, output_name, _ = route
        body = self.read_json_body()
        if not isinstance(body, dict) or not isinstance(body.get('command'), str):
            self.send_json({"error": "Body must be JSON with a string \"command\""}, 400)
            return
        self.send_command_result(streams.set_command(stream_name, output_name, body['command']))

    def do_DELETE(self):
        route = self.output_route()
        if route is None or route[2] != 'command':
            self.send_json({"error": "Not found"}, 404)
            return
        stream_name, output_name, _ = route
        self.send_command_result(streams.set_command(stream_name, output_name, None))

    def send_command_result(self, error):
        if error:
            self.send_json({"error": error}, 400)
        else:
            self.send_json({"success": True})


def serve(port):
    """Serve the control UI and API on port, until the process dies."""
    try:
        server = ThreadingHTTPServer(('0.0.0.0', port), StreamHandler)  # type: ignore[arg-type]
        log.info("Stream Control UI: http://localhost:%d", port)
        server.serve_forever()
    except Exception as error:
        # The control UI is a primary feature; silent half-death (supervisor alive,
        # UI gone) is worse than a hard exit that systemd/compose can restart.
        log.critical("API server failed: %s", error)
        os._exit(1)
