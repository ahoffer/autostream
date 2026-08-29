"""HTTP control API and the web UI it serves.

The transport for the stream operations: routing, JSON encoding, status codes,
and the index.html page live here, so streams.py never has to know it is
reachable over HTTP.

Routes:
  GET    /                                        the UI
  GET    /api/streams                             status of every stream
  POST   /api/streams/start-all | stop-all
  POST   /api/streams/{name}/settings?loop=&mode=&gop=&fps=  store settings, apply if running
  POST   /api/streams/{name}/start                start (or restart) with stored settings
  POST   /api/streams/{name}/stop                 stop every output
  POST   /api/streams/{name}/outputs/{out}/start  restart one output
  POST   /api/streams/{name}/outputs/{out}/stop   stop one output
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
        if not streams.stream_exists(stream_name):
            self.send_json({"error": "Stream not found"}, 404)
            return
        if action == 'start':
            self.send_json({"success": streams.start_stream(stream_name)})
        elif action == 'stop':
            self.send_json({"success": streams.stop_stream(stream_name)})
        elif action == 'settings':
            settings, error = parse_settings(parse_qs(parsed.query))
            if error:
                self.send_json({"error": error}, 400)
                return
            self.send_json({"success": streams.update_settings(stream_name, **settings)})
        else:
            self.send_json({"error": "Unknown action"}, 400)


def parse_settings(query):
    """The settings named in a ?loop=&mode=&gop=&fps= query as update_settings
    keyword arguments, or an error message. An absent key leaves that setting
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
            # The hardware encoders refuse rates above roughly 120.
            if not 1 <= settings['frame_rate'] <= 120:
                return None, "FPS must be a number from 1 to 120, or auto"
    return settings, None

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
