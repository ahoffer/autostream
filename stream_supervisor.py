#!/usr/bin/env python3
"""Compose the application; execute all domain work on one supervisor thread."""
import json
import logging
from functools import partial
from http.server import ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from queue import Queue, Empty, Full
import signal
import socket
import threading
import weakref

import filewatch
import streamapi
from appconfig import Config
from outputregistry import OutputRegistry, is_ignored
from statefile import StreamStateStore
from streamresponses import group_outputs
from udphost import UdpHostResolver

log = logging.getLogger(__name__)


class OneLineFormatter(logging.Formatter):
    def format(self, record):
        return super().format(record).replace('\n', '; ')


def configure_logging(log_level):
    logging.addLevelName(logging.WARNING, 'WARN')
    handler = logging.StreamHandler()
    handler.setFormatter(OneLineFormatter('%(asctime)s %(levelname)s %(message)s', '%H:%M:%S'))
    logging.basicConfig(level=log_level.upper(), handlers=[handler])


def wait_for_mediamtx(rtsp_port, stop_event):
    while not stop_event.is_set():
        try:
            with socket.create_connection(('localhost', rtsp_port), timeout=1):
                return True
        except OSError:
            stop_event.wait(1)
    return False


def output_logger(output_id, config):
    logger = logging.Logger('ffmpeg.' + output_id.replace('/', '.'))
    logger.parent = logging.getLogger()
    config.ffmpeg_log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.ffmpeg_log_dir / (output_id.replace('/', '.') + '.log'),
                                  maxBytes=2_000_000, backupCount=1)
    logger.addHandler(handler)
    # The output owns this logger; do not retain one open file per retired name.
    weakref.finalize(logger, handler.close)
    logger.setLevel(logging.INFO)
    logger.propagate = config.show_ffmpeg_output
    return logger


def _watch_files(directory, requests, stop_event):
    try:
        for inventory in filewatch.watch(directory, is_ignored, stop_event=stop_event):
            while not stop_event.is_set():
                try:
                    requests.put((inventory, None), timeout=.2)
                    break
                except Full:
                    continue
    except Exception:
        log.exception('Directory watcher failed; shutting down supervisor')
        stop_event.set()


def _execute_item(item, registry, publish):
    operation, future = item
    if future is None:
        registry.supervise(operation)
        return
    try:
        try:
            result = operation()
        finally:
            publish()  # Include a Stop that succeeded despite a failed save.
    except Exception as error:
        future.set_exception(error)
    else:
        future.set_result(result)


def run_loop(registry, requests, publish, stop_event):
    """Bounded batches preserve supervision ticks under continuous HTTP traffic."""
    try:
        while not stop_event.is_set():
            try:
                item = requests.get(timeout=.5)
            except Empty:
                pass
            else:
                _execute_item(item, registry, publish)
                for _ in range(31):
                    if stop_event.is_set():
                        break
                    try:
                        item = requests.get_nowait()
                    except Empty:
                        break
                    _execute_item(item, registry, publish)
            registry.supervise()
            publish()
    finally:
        stop_event.set()
        while True:
            try:
                _, future = requests.get_nowait()
            except Empty:
                break
            if future is not None:
                future.set_exception(RuntimeError('Supervisor is shutting down'))
        # Process shutdown must not persist a user Stop across application restarts.
        if not registry.stop_all_outputs(persist=False):
            log.error('Some processes did not exit during supervisor shutdown')


def main():
    config = Config.from_env()
    configure_logging(config.log_level)
    store = StreamStateStore(config.state_path)
    outputs = OutputRegistry(config, store, UdpHostResolver(), partial(output_logger, config=config))
    outputs.load_persisted_state()
    requests = Queue(maxsize=128)
    stopping = threading.Event()
    public_urls = {'rtsp': f'rtsp://{config.hostname}:{config.rtsp_port}',
                   'hls': f'http://{config.hostname}:{config.hls_port}'}
    snapshot = [b'[]']
    snapshot_lock = threading.Lock()

    def publish():
        encoded = json.dumps(group_outputs(outputs.status(), public_urls)).encode()
        with snapshot_lock:
            snapshot[0] = encoded

    def read_status():
        with snapshot_lock:
            return snapshot[0]

    handler = partial(streamapi.StreamHandler, requests=requests, read_status=read_status,
                      execute=partial(streamapi._execute_request, outputs))
    server = ThreadingHTTPServer(('0.0.0.0', config.api_port), handler)
    api_thread = threading.Thread(target=server.serve_forever, name='APIServer', daemon=True)
    watcher = threading.Thread(target=_watch_files, args=(config.videos_dir, requests, stopping),
                               name='FileWatcher', daemon=True)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    try:
        if not wait_for_mediamtx(config.rtsp_port, stopping):
            return
        api_thread.start()
        watcher.start()
        log.info('Supervisor starting, UI http://localhost:%d', config.api_port)
        run_loop(outputs, requests, publish, stopping)
    finally:
        stopping.set()
        if api_thread.is_alive():
            server.shutdown()
        server.server_close()
        if watcher.ident is not None:
            watcher.join(timeout=3)
            if watcher.is_alive():
                log.warning('File watcher is still blocked in filesystem IO')


if __name__ == '__main__':
    main()
