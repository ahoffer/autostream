#!/usr/bin/env python3
"""Compose the application; execute all domain work on one supervisor thread."""
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
from statefile import StreamStateStore
from streamregistry import StreamRegistry, is_ignored
from streamresponses import StatusSnapshot, UrlFacts
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


def stream_logger(name, config):
    logger = logging.Logger('ffmpeg.' + name)
    logger.parent = logging.getLogger()
    config.ffmpeg_log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.ffmpeg_log_dir / (name + '.log'),
                                  maxBytes=2_000_000, backupCount=1)
    logger.addHandler(handler)
    # The stream owns this logger; do not retain one open file per deleted name.
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
        if not registry.stop_all_streams(persist=False):
            log.error('Some processes did not exit during supervisor shutdown')


def main():
    config = Config.from_env()
    configure_logging(config.log_level)
    store = StreamStateStore(config.state_path)
    registry = StreamRegistry(config, store, UdpHostResolver(), partial(stream_logger, config=config))
    registry.load_persisted_state()
    requests = Queue(maxsize=128)
    stopping = threading.Event()
    snapshot = [None]
    snapshot_lock = threading.Lock()

    def publish():
        published = StatusSnapshot(tuple(registry.status()), registry.settings(), registry.videos())
        with snapshot_lock:
            snapshot[0] = published

    def read_snapshot():
        with snapshot_lock:
            return snapshot[0]

    publish()
    handler = partial(streamapi.StreamHandler, requests=requests, read_snapshot=read_snapshot,
                      execute=partial(streamapi._execute_request, registry),
                      url_facts=UrlFacts(config.rtsp_port, config.hls_port, config.rtmp_port, config.api_port),
                      hls_dir=config.hls_dir,
                      host_addresses={'hostname': config.host_name, 'lan_ip': config.host_lan_ip})
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
        log.info('Supervisor starting, UI on port %d', config.api_port)
        run_loop(registry, requests, publish, stopping)
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
