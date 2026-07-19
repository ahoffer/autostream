"""HTTP control API and the web UI it serves.

The transport for the stream operations: routing, JSON encoding, status codes,
and the index.html page live here, so streams.py never has to know it is
reachable over HTTP.
"""
from __future__ import annotations

import json
import logging
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/' or parsed.path == '/index.html':
            try:
                html = INDEX_HTML_PATH.read_text(encoding='utf-8')
            except OSError as e:
                self.send_json({"error": f"index.html unavailable: {e}"}, 500)
                return
            self.send_html(html)
        elif parsed.path == '/api/streams':
            self.send_json(streams.get_stream_status())
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip('/').split('/')

        if path_parts[:2] != ['api', 'streams'] or len(path_parts) not in (3, 4):
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
            success = streams.restart_stream(stream_name, loop_count)
            self.send_json({"success": success})
        elif action == 'stop':
            success = streams.stop_stream(stream_name)
            self.send_json({"success": success})
        else:
            self.send_json({"error": "Unknown action"}, 400)


def serve(port):
    """Serve the control UI and API on port, until the process dies."""
    try:
        server = ThreadingHTTPServer(('0.0.0.0', port), StreamHandler)  # type: ignore[arg-type]
        log.info("Stream Control UI: http://localhost:%d", port)
        server.serve_forever()
    except Exception as e:
        # The control UI is a primary feature; silent half-death (supervisor alive,
        # UI gone) is worse than a hard exit that systemd/compose can restart.
        log.critical("API server failed: %s", e)
        os._exit(1)
