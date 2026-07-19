"""
Polling directory watcher.

inotify does not see changes made by other clients on network filesystems
(CIFS/NFS), so this watcher polls: it periodically snapshots the directory's
(mtime, size) state and diffs successive snapshots into events.

The single entry point is watch(), a generator that yields the debounced
(creates, modifies, deletions) after every poll — empty lists when nothing
changed. Files present when the watch starts are reported as creates once
they pass the same stability check as any other file — to the caller,
startup looks exactly like someone dropping files into an already-watched
directory.
"""
import logging
import math
import time

log = logging.getLogger("autostream")

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
        """Return (creates, modifies, deletions) for this snapshot."""
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

        return creates, modifies, deletions


def watch(directory, ignore, poll_interval=POLL_INTERVAL_SEC, quiet_period=QUIET_PERIOD):
    """Yield (creates, modifies, deletions) filename lists after every poll.

    quiet_period is in seconds, rounded up to whole polls, so a change is
    reported at most one poll after the file has held steady that long.

    Non-files and entries rejected by the ignore predicate are skipped, as are
    entries that vanish mid-scan — the next poll picks those up as deletions.
    Scans in name order so events for simultaneous files arrive deterministically.
    """
    log.info("Watching %s for changes (polling mode)...", directory)
    debouncer = FileChangeDebouncer(math.ceil(quiet_period / poll_interval))
    while True:
        # filename -> (mtime, size): keyed by name for O(1) diffing between polls.
        files = {}
        try:
            for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
                try:
                    if not path.is_file() or ignore(path):
                        continue
                    stat = path.stat()
                except OSError:
                    continue  # vanished between the scan and the stat; next poll sees the delete
                files[path.name] = (stat.st_mtime, stat.st_size)
        except OSError as error:
            log.error("Cannot read %s: %s", directory, error)
            yield [], [], []
        else:
            yield debouncer.poll(files)
        time.sleep(poll_interval)
