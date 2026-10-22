"""
Polling directory watcher.

inotify does not see changes made by other clients on network filesystems
(CIFS/NFS), so this watcher polls: it periodically snapshots the directory's
(mtime, size) state and diffs successive snapshots into events.

The entry point watch() yields detached changes and present/stable inventories
after successful scans, including scans with no new changes. Files present when the watch starts are reported as creates once
they pass the same stability check as any other file — to the caller,
startup looks exactly like someone dropping files into an already-watched
directory.
"""
import logging
import math
import threading
import stat as stat_module

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 2
QUIET_PERIOD = 4  # seconds a file's (mtime, size) must hold steady before a change is reported


class FileChangeDebouncer:
    """Turns successive directory snapshots into create/modify/delete events.

    Copying a file into a watched directory takes far longer than one poll, so
    reporting immediately would hand the caller a truncated file and then a
    modify event on every poll as the copy grows. The first poll to see a new
    (mtime, size) registers it as a candidate; each later poll that sees the
    same snapshot confirms it. A create or modify is reported only after
    confirmations_needed consecutive confirmations. Deletions are reported
    immediately; gone is gone.
    """

    def __init__(self, confirmations_needed):
        self._confirmations_needed = confirmations_needed
        self._known = {}    # filename -> (mtime, size) as last reported to the caller
        self._pending = {}  # filename -> (candidate snapshot, confirmations so far)

    def poll(self, current_files):
        """Return detached changes and present/stable inventories for this scan."""
        deletions = [name for name in self._known if name not in current_files]
        for name in deletions:
            del self._known[name]

        # Forget files that vanished mid-debounce.
        for name in list(self._pending):
            if name not in current_files:
                del self._pending[name]

        creates, modifies = [], []
        for name, current in current_files.items():
            previous = self._known.get(name)
            if previous == current:
                self._pending.pop(name, None)
                continue
            candidate, confirmations = self._pending.get(name, (None, 0))
            if candidate != current:
                self._pending[name] = (current, 0)
                continue
            confirmations += 1
            if confirmations < self._confirmations_needed:
                self._pending[name] = (current, confirmations)
                continue
            if previous is None:
                creates.append(name)
            else:
                modifies.append(name)
            self._known[name] = current
            del self._pending[name]

        return {'creates': tuple(creates), 'modifies': tuple(modifies), 'deletes': tuple(deletions),
                'present': tuple(current_files),
                'stable': tuple(name for name, value in current_files.items() if self._known.get(name) == value)}


def _scan(directory, ignore):
    files = {}
    for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if ignore(path):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue  # A concurrent deletion is reflected in this observation.
        # Other stat errors abort the entire scan; unreadable is not deleted.
        if stat_module.S_ISREG(stat.st_mode):
            files[path.name] = (stat.st_mtime_ns, stat.st_size)
    return files


def watch(directory, ignore, poll_interval=POLL_INTERVAL_SEC, quiet_period=QUIET_PERIOD, *, stop_event=None):
    """Yield detached event/inventory batches; intended for one watcher thread.

    Startup files pass the same stability check as additions. An unsuccessful
    scan preserves the last successful observation and yields no deletion events.
    """
    stop_event = stop_event if stop_event is not None else threading.Event()
    debouncer = FileChangeDebouncer(math.ceil(quiet_period / poll_interval))
    while not stop_event.is_set():
        try:
            files = _scan(directory, ignore)
        except OSError as error:
            log.error('Cannot scan %s: %s', directory, error)
        else:
            yield debouncer.poll(files)
        stop_event.wait(poll_interval)
