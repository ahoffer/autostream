"""
Polling directory watcher.

inotify does not see changes made by other clients on network filesystems
(CIFS/NFS), so this watcher polls: every tick it snapshots the directory's
(mtime, size) state and diffs successive snapshots into events.

The single entry point is watch(), a generator that yields one debounced
(creates, modifies, deletions) batch per tick. Files present when the watch
starts are reported as creates once they pass the same stability check as
any other file — to the caller, startup looks exactly like someone dropping
files into an already-watched directory.
"""
import logging
import time

log = logging.getLogger("autostream")

POLL_INTERVAL_SEC = 2
DEBOUNCE_STABLE_POLLS = 2  # ~4s of (mtime, size) stability before committing a change


def snapshot_stats(directory, ignore):
    """Return this tick's directory state, or None if the directory is unreadable.

    Non-files and entries rejected by the ignore predicate are skipped, as are
    entries that vanish mid-scan — the next poll picks those up as deletions.
    Scans in name order so events for simultaneous files arrive deterministically.
    """
    # filename -> (mtime, size): keyed by name for O(1) diffing between ticks.
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
        return None
    return files


class FileChangeDebouncer:
    """Turns successive directory snapshots into create/modify/delete events.

    Copying a file into a watched directory takes far longer than one poll, so
    reporting immediately would hand the caller a truncated file and then a
    modify event on every tick as the copy grows. A create or modify is
    therefore only reported once its (mtime, size) has held steady for
    stable_polls consecutive polls. Deletions are reported immediately; gone
    is gone.
    """

    def __init__(self, stable_polls):
        self._stable_polls = stable_polls
        self._known = {}    # filename -> (mtime, size) as last reported to the caller
        self._pending = {}  # filename -> (snapshot, consecutive stable polls)

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
            snapshot, stable_polls = self._pending.get(name, (None, 0))
            if snapshot != current:
                self._pending[name] = (current, 0)
                continue
            stable_polls += 1
            if stable_polls < self._stable_polls:
                self._pending[name] = (current, stable_polls)
                continue
            if previous is None:
                creates.append(name)
            else:
                modifies.append(name)
            self._known[name] = current
            del self._pending[name]

        return creates, modifies, deletions


def watch(directory, ignore, poll_interval=POLL_INTERVAL_SEC, stable_polls=DEBOUNCE_STABLE_POLLS):
    """Yield (creates, modifies, deletions) filename lists, one batch per tick.

    Never returns. A batch is yielded every tick even when nothing changed, so
    the caller can piggyback periodic work on the poll cadence. An unreadable
    directory yields an empty batch rather than fabricating deletions.
    """
    log.info("Watching %s for changes (polling mode)...", directory)
    debouncer = FileChangeDebouncer(stable_polls)
    while True:
        files = snapshot_stats(directory, ignore)
        if files is None:
            yield [], [], []
        else:
            yield debouncer.poll(files)
        time.sleep(poll_interval)
