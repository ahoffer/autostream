//! Polling directory watcher.
//!
//! inotify does not see changes made by other clients on network filesystems
//! (CIFS/NFS), so this watcher polls: it periodically snapshots the directory's
//! (mtime, size) state and diffs successive snapshots into events.
//!
//! The entry point `watch` delivers detached changes and present/stable
//! inventories after successful scans, including scans with no new changes.
//! Files present when the watch starts are reported as creates once they pass
//! the same stability check as any other file: to the caller, startup looks
//! exactly like someone dropping files into an already-watched directory.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::time::Duration;

use indexmap::IndexMap;

use crate::stopsignal::StopSignal;

pub const POLL_INTERVAL: Duration = Duration::from_secs(2);
/// How long a file's (mtime, size) must hold steady before a change is reported.
pub const QUIET_PERIOD: Duration = Duration::from_secs(4);

/// What one poll saw of a file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileSnapshot {
    pub mtime_ns: i64,
    pub size: u64,
}

/// One scan of the directory: regular files by name, in name order.
pub type DirectoryScan = BTreeMap<String, FileSnapshot>;

/// Detached changes and present/stable inventories for one scan.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Inventory {
    pub creates: Vec<String>,
    pub modifies: Vec<String>,
    pub deletes: Vec<String>,
    pub present: Vec<String>,
    pub stable: Vec<String>,
}

impl Inventory {
    pub fn changed(&self) -> impl Iterator<Item = &str> {
        self.creates
            .iter()
            .chain(&self.modifies)
            .chain(&self.deletes)
            .map(String::as_str)
    }
}

/// Turns successive directory snapshots into create/modify/delete events.
///
/// Copying a file into a watched directory takes far longer than one poll, so
/// reporting immediately would hand the caller a truncated file and then a
/// modify event on every poll as the copy grows. The first poll to see a new
/// (mtime, size) registers it as a candidate; each later poll that sees the
/// same snapshot confirms it. A create or modify is reported only after
/// `confirmations_needed` consecutive confirmations. Deletions are reported
/// immediately; gone is gone.
pub struct FileChangeDebouncer {
    confirmations_needed: u32,
    // Filename to the snapshot last reported to the caller, in the order first reported.
    known: IndexMap<String, FileSnapshot>,
    // Filename to the candidate snapshot and its confirmations so far.
    pending: IndexMap<String, (FileSnapshot, u32)>,
}

impl FileChangeDebouncer {
    pub fn new(confirmations_needed: u32) -> Self {
        Self {
            confirmations_needed,
            known: IndexMap::new(),
            pending: IndexMap::new(),
        }
    }

    pub fn poll(&mut self, current_files: &DirectoryScan) -> Inventory {
        let deletes: Vec<String> = self
            .known
            .keys()
            .filter(|name| !current_files.contains_key(*name))
            .cloned()
            .collect();
        for name in &deletes {
            self.known.shift_remove(name);
        }
        // Forget files that vanished mid-debounce.
        self.pending
            .retain(|name, _| current_files.contains_key(name));

        let mut creates = Vec::new();
        let mut modifies = Vec::new();
        for (name, current) in current_files {
            let previous = self.known.get(name).copied();
            if previous == Some(*current) {
                self.pending.shift_remove(name);
                continue;
            }
            let (candidate, confirmations) = self
                .pending
                .get(name)
                .map_or((None, 0), |(candidate, confirmations)| {
                    (Some(*candidate), *confirmations)
                });
            if candidate != Some(*current) {
                self.pending.insert(name.clone(), (*current, 0));
                continue;
            }
            let confirmations = confirmations + 1;
            if confirmations < self.confirmations_needed {
                self.pending.insert(name.clone(), (*current, confirmations));
                continue;
            }
            if previous.is_none() {
                creates.push(name.clone());
            } else {
                modifies.push(name.clone());
            }
            self.known.insert(name.clone(), *current);
            self.pending.shift_remove(name);
        }
        Inventory {
            creates,
            modifies,
            deletes,
            present: current_files.keys().cloned().collect(),
            stable: current_files
                .iter()
                .filter(|(name, value)| self.known.get(*name) == Some(*value))
                .map(|(name, _)| name.clone())
                .collect(),
        }
    }
}

/// Snapshot the regular files in a directory; a concurrent deletion is skipped,
/// any other failure aborts the scan because unreadable is not deleted.
pub fn scan_directory(
    directory: &Path,
    ignore: &dyn Fn(&Path) -> bool,
) -> io::Result<DirectoryScan> {
    let mut entries: Vec<_> = fs::read_dir(directory)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<_>>()?;
    entries.sort_by(|left, right| left.file_name().cmp(&right.file_name()));
    let mut files = DirectoryScan::new();
    for path in entries {
        if ignore(&path) {
            continue;
        }
        let metadata = match fs::metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if metadata.is_file() {
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            files.insert(
                name,
                FileSnapshot {
                    mtime_ns: metadata.mtime() * 1_000_000_000 + metadata.mtime_nsec(),
                    size: metadata.len(),
                },
            );
        }
    }
    Ok(files)
}

/// Deliver detached event/inventory batches until stopped; intended for one watcher thread.
///
/// Startup files pass the same stability check as additions. An unsuccessful
/// scan preserves the last successful observation and yields no deletion events.
pub fn watch(
    directory: &Path,
    ignore: &dyn Fn(&Path) -> bool,
    poll_interval: Duration,
    quiet_period: Duration,
    stop: &StopSignal,
    deliver: impl FnMut(Inventory),
) {
    watch_with(
        || {
            scan_directory(directory, ignore).map_err(|error| {
                log::error!("Cannot scan {}: {}", directory.display(), error);
                error
            })
        },
        poll_interval,
        quiet_period,
        stop,
        deliver,
    );
}

/// The polling loop over an injected scanner, so tests can script scan results.
pub fn watch_with(
    mut scan: impl FnMut() -> io::Result<DirectoryScan>,
    poll_interval: Duration,
    quiet_period: Duration,
    stop: &StopSignal,
    mut deliver: impl FnMut(Inventory),
) {
    let confirmations = (quiet_period.as_secs_f64() / poll_interval.as_secs_f64()).ceil() as u32;
    let mut debouncer = FileChangeDebouncer::new(confirmations);
    while !stop.is_set() {
        if let Ok(files) = scan() {
            deliver(debouncer.poll(&files));
        }
        stop.wait(poll_interval);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seen(files: &[(&str, i64, u64)]) -> DirectoryScan {
        files
            .iter()
            .map(|(name, mtime_ns, size)| {
                (
                    name.to_string(),
                    FileSnapshot {
                        mtime_ns: *mtime_ns,
                        size: *size,
                    },
                )
            })
            .collect()
    }

    #[test]
    fn growth_resets_stability_and_startup_uses_same_check() {
        let mut watcher = FileChangeDebouncer::new(2);
        assert!(watcher.poll(&seen(&[("a.mp4", 1, 10)])).stable.is_empty());
        assert!(watcher.poll(&seen(&[("a.mp4", 1, 10)])).stable.is_empty());
        assert!(watcher.poll(&seen(&[("a.mp4", 2, 20)])).stable.is_empty());
        watcher.poll(&seen(&[("a.mp4", 2, 20)]));
        let batch = watcher.poll(&seen(&[("a.mp4", 2, 20)]));
        assert_eq!(batch.creates, ["a.mp4"]);
        assert_eq!(batch.stable, ["a.mp4"]);
        assert_eq!(batch.present, ["a.mp4"]);
        assert_eq!(watcher.poll(&seen(&[])).deletes, ["a.mp4"]);
    }

    #[test]
    fn modifications_are_reported_after_the_file_settles_again() {
        let mut watcher = FileChangeDebouncer::new(1);
        watcher.poll(&seen(&[("a.mp4", 1, 10)]));
        assert_eq!(watcher.poll(&seen(&[("a.mp4", 1, 10)])).creates, ["a.mp4"]);
        assert!(watcher.poll(&seen(&[("a.mp4", 2, 20)])).modifies.is_empty());
        let batch = watcher.poll(&seen(&[("a.mp4", 2, 20)]));
        assert_eq!(batch.modifies, ["a.mp4"]);
        assert_eq!(batch.stable, ["a.mp4"]);
    }

    #[test]
    fn scan_error_preserves_known_files() {
        let stop = StopSignal::new();
        let mut scans: Vec<io::Result<DirectoryScan>> = vec![
            Ok(seen(&[("a.mp4", 1, 10)])),
            Ok(seen(&[("a.mp4", 1, 10)])),
            Err(io::Error::other("offline")),
            Ok(seen(&[("a.mp4", 1, 10)])),
            Ok(seen(&[])),
        ];
        scans.reverse();
        let mut batches = Vec::new();
        let stopper = stop.clone();
        watch_with(
            || {
                scans.pop().unwrap_or_else(|| {
                    stopper.set();
                    Err(io::Error::other("exhausted"))
                })
            },
            Duration::from_millis(1),
            Duration::from_millis(1),
            &stop,
            |batch| batches.push(batch),
        );
        assert_eq!(batches.len(), 4);
        assert_eq!(batches[1].creates, ["a.mp4"]);
        assert!(batches[2].deletes.is_empty());
        assert_eq!(batches[3].deletes, ["a.mp4"]);
    }

    #[test]
    fn scanning_lists_regular_files_in_name_order_and_skips_ignored_ones() {
        let directory = tempfile::tempdir().unwrap();
        fs::write(directory.path().join("b.mp4"), b"bb").unwrap();
        fs::write(directory.path().join("a.mp4"), b"a").unwrap();
        fs::write(directory.path().join(".hidden"), b"x").unwrap();
        fs::create_dir(directory.path().join("sub")).unwrap();
        let scan = scan_directory(directory.path(), &|path| {
            path.file_name()
                .is_some_and(|name| name.to_string_lossy().starts_with('.'))
        })
        .unwrap();
        assert_eq!(scan.keys().collect::<Vec<_>>(), ["a.mp4", "b.mp4"]);
        assert_eq!(scan["a.mp4"].size, 1);
        assert!(scan_directory(&directory.path().join("missing"), &|_| false).is_err());
    }
}
