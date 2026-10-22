"""One Popen run with file-backed diagnostics and bounded termination.

Called only by the supervisor thread. The injected logger is borrowed.
"""
import os
import subprocess
import tempfile
import time


class FFmpegProcess:
    STOP_TIMEOUT = 5
    TAIL_BYTES = 4096
    READ_BYTES = 65536

    def __init__(self, logger):
        self._logger = logger
        self._process = None
        self._argv = ()
        self._started_at = None
        self._capture = None
        self._offset = 0
        self._tail = b''

    def start(self, argv):
        if self._argv:
            raise RuntimeError('A process wrapper owns only one run')
        self._argv = tuple(argv)
        self._capture = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(argv, stdout=self._capture,
                                             stderr=subprocess.STDOUT)
        except OSError:
            self._capture.close()
            self._capture = None
            raise
        self._started_at = time.monotonic()

    def _read_diagnostics(self, finished):
        if self._capture is None:
            return
        try:
            size = os.fstat(self._capture.fileno()).st_size
            # Bound work per tick, including the final drain after exit. Keep the
            # newest diagnostics if the child produced more than we can forward.
            offset = max(self._offset, size - self.READ_BYTES)
            if offset > self._offset:
                self._logger.warning('Skipped %d bytes of FFmpeg diagnostic backlog', offset - self._offset)
            chunk = os.pread(self._capture.fileno(), self.READ_BYTES, offset)
            self._offset = offset + len(chunk)
            if chunk:
                self._tail = (self._tail + chunk)[-self.TAIL_BYTES:]
                self._logger.info('%s', chunk.decode('utf-8', errors='replace').rstrip())
        except Exception as error:
            # Capture/logging failure must not prevent process cleanup.
            self._tail = f'Diagnostic capture failed: {error}'.encode()[-self.TAIL_BYTES:]
        finally:
            if finished:
                self._capture.close()
                self._capture = None

    def observe(self):
        code = self._process.poll() if self._process is not None else None
        self._read_diagnostics(code is not None)
        return {'returncode': code, 'argv': self._argv, 'started_at': self._started_at,
                'diagnostics': self._tail.decode('utf-8', errors='replace')}

    def stop(self):
        if self._process is None:
            return True
        try:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=self.STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=self.STOP_TIMEOUT)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.TimeoutExpired):
            return False
        return self.observe()['returncode'] is not None
