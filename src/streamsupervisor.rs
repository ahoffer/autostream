//! Execute all domain work on one supervisor thread, fed by a bounded queue.

use std::io;
use std::net::{TcpStream, ToSocketAddrs};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;
use tokio::sync::oneshot;

use crate::filewatch::{self, DirectoryScan, Inventory, POLL_INTERVAL, QUIET_PERIOD};
use crate::stopsignal::StopSignal;
use crate::streamregistry::{StreamRegistry, is_ignored};
use crate::streamresponses::StatusSnapshot;

/// Requests the supervisor accepts without blocking the submitters.
pub const QUEUE_CAPACITY: usize = 128;
/// Requests drained per wake-up before a supervision tick, so traffic cannot starve recovery.
const BATCH_SIZE: usize = 32;

/// What an HTTP mutation answers with: a JSON body and its status code.
#[derive(Clone, Debug, PartialEq)]
pub struct ApiReply {
    pub body: Value,
    pub status: u16,
}

/// Work that runs on the supervisor thread with exclusive access to the registry.
pub type Operation = Box<dyn FnOnce(&mut StreamRegistry) -> Result<ApiReply, String> + Send>;

pub enum SupervisorRequest {
    Operation {
        operation: Operation,
        reply: oneshot::Sender<Result<ApiReply, String>>,
    },
    Observation(Inventory),
}

/// The last published snapshot, replaced whole under a short-held lock.
pub type SnapshotCell = Arc<Mutex<Arc<StatusSnapshot>>>;

pub fn snapshot_of(registry: &mut StreamRegistry) -> Arc<StatusSnapshot> {
    Arc::new(StatusSnapshot {
        statuses: registry.status(),
        settings: registry.settings().clone(),
        videos: registry.videos().to_vec(),
    })
}

pub fn publish(registry: &mut StreamRegistry, cell: &SnapshotCell) {
    let published = snapshot_of(registry);
    *cell.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = published;
}

pub fn read_snapshot(cell: &SnapshotCell) -> Arc<StatusSnapshot> {
    cell.lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone()
}

pub fn wait_for_mediamtx(rtsp_port: u16, stop: &StopSignal) -> bool {
    while !stop.is_set() {
        let reachable = ("localhost", rtsp_port)
            .to_socket_addrs()
            .ok()
            .into_iter()
            .flatten()
            .any(|address| TcpStream::connect_timeout(&address, Duration::from_secs(1)).is_ok());
        if reachable {
            return true;
        }
        stop.wait(Duration::from_secs(1));
    }
    false
}

/// Poll the videos directory and hand every observation to the supervisor.
pub fn watch_files(directory: &Path, requests: SyncSender<SupervisorRequest>, stop: StopSignal) {
    watch_files_with(
        || {
            filewatch::scan_directory(directory, &is_ignored).map_err(|error| {
                log::error!("Cannot scan {}: {}", directory.display(), error);
                error
            })
        },
        requests,
        stop,
    );
}

/// The watcher loop over an injected scanner; a panic in it shuts the supervisor down.
pub fn watch_files_with(
    scan: impl FnMut() -> io::Result<DirectoryScan>,
    requests: SyncSender<SupervisorRequest>,
    stop: StopSignal,
) {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        filewatch::watch_with(scan, POLL_INTERVAL, QUIET_PERIOD, &stop, |inventory| {
            submit_observation(&requests, inventory, &stop);
        });
    }));
    if outcome.is_err() {
        log::error!("Directory watcher failed; shutting down supervisor");
        stop.set();
    }
}

/// Keep offering the observation until the queue takes it or shutdown begins.
pub fn submit_observation(
    requests: &SyncSender<SupervisorRequest>,
    inventory: Inventory,
    stop: &StopSignal,
) {
    let mut item = SupervisorRequest::Observation(inventory);
    while !stop.is_set() {
        match requests.try_send(item) {
            Ok(()) => return,
            Err(TrySendError::Full(returned)) => {
                item = returned;
                stop.wait(Duration::from_millis(200));
            }
            Err(TrySendError::Disconnected(_)) => return,
        }
    }
}

fn execute_item(
    item: SupervisorRequest,
    registry: &mut StreamRegistry,
    publish: &mut dyn FnMut(&mut StreamRegistry),
) {
    match item {
        SupervisorRequest::Observation(inventory) => registry.supervise(Some(&inventory)),
        SupervisorRequest::Operation { operation, reply } => {
            let result = operation(registry);
            publish(registry); // Include a Stop that succeeded despite a failed save.
            let _ = reply.send(result);
        }
    }
}

/// Bounded batches preserve supervision ticks under continuous HTTP traffic.
pub fn run_loop(
    registry: &mut StreamRegistry,
    requests: &Receiver<SupervisorRequest>,
    publish: &mut dyn FnMut(&mut StreamRegistry),
    stop: &StopSignal,
) {
    while !stop.is_set() {
        match requests.recv_timeout(Duration::from_millis(500)) {
            Ok(item) => {
                execute_item(item, registry, publish);
                for _ in 1..BATCH_SIZE {
                    if stop.is_set() {
                        break;
                    }
                    match requests.try_recv() {
                        Ok(item) => execute_item(item, registry, publish),
                        Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
                    }
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        registry.supervise(None);
        publish(registry);
    }
    stop.set();
    while let Ok(item) = requests.try_recv() {
        if let SupervisorRequest::Operation { reply, .. } = item {
            let _ = reply.send(Err("Supervisor is shutting down".to_string()));
        }
    }
    // Process shutdown must not persist a user Stop across application restarts.
    match registry.stop_all_streams(false) {
        Ok(true) => {}
        Ok(false) => log::error!("Some processes did not exit during supervisor shutdown"),
        Err(error) => log::error!("Supervisor shutdown failed to stop every process: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::sync_channel;
    use std::thread;

    use crate::streamresponses::StreamState;
    use crate::testsupport::{Harness, inventory};

    type Reply = oneshot::Receiver<Result<ApiReply, String>>;

    fn setup() -> Harness {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        harness.create("boats-udp", &source, "udp");
        harness
    }

    fn operation(
        call: impl FnOnce(&mut StreamRegistry) -> Result<ApiReply, String> + Send + 'static,
    ) -> (SupervisorRequest, Reply) {
        let (reply, receiver) = oneshot::channel();
        let request = SupervisorRequest::Operation {
            operation: Box::new(call),
            reply,
        };
        (request, receiver)
    }

    fn success() -> ApiReply {
        ApiReply {
            body: serde_json::json!({"success": true}),
            status: 200,
        }
    }

    #[test]
    fn stop_queued_during_slow_start_executes_after_start_and_prevents_recovery() {
        let Harness {
            mut registry,
            ffmpeg,
            config,
            state,
            videos,
            hls,
            ..
        } = setup();
        {
            let (stream, services) = registry.stream_mut("boats").unwrap();
            stream.stop_streaming(services).unwrap();
        }
        let (sender, receiver) = sync_channel(8);
        let stop = StopSignal::new();
        let entered = StopSignal::new();
        let release = StopSignal::new();
        let (start_request, started) = operation({
            let entered = entered.clone();
            let release = release.clone();
            move |registry| {
                entered.set();
                assert!(
                    release.wait(Duration::from_secs(3)),
                    "test did not release work"
                );
                let (stream, services) = registry.stream_mut("boats").unwrap();
                stream
                    .start_streaming(services)
                    .map(|_| success())
                    .map_err(|error| error.to_string())
            }
        });
        let (stop_request, stopped) = operation(|registry| {
            let (stream, services) = registry.stream_mut("boats").unwrap();
            stream
                .stop_streaming(services)
                .map(|_| success())
                .map_err(|error| error.to_string())
        });
        sender.send(start_request).unwrap();
        let snapshot: SnapshotCell = Arc::new(Mutex::new(snapshot_of(&mut registry)));
        let loop_stop = stop.clone();
        let cell = snapshot.clone();
        let supervisor = thread::spawn(move || {
            run_loop(
                &mut registry,
                &receiver,
                &mut |registry| publish(registry, &cell),
                &loop_stop,
            );
        });
        assert!(entered.wait(Duration::from_secs(2)));
        sender.send(stop_request).unwrap();
        thread::sleep(Duration::from_millis(50));
        assert!(stopped.is_empty());
        release.set();
        assert!(started.blocking_recv().unwrap().is_ok());
        assert!(stopped.blocking_recv().unwrap().is_ok());
        let published = read_snapshot(&snapshot);
        let row = published
            .statuses
            .iter()
            .find(|row| row.name == "boats")
            .unwrap();
        assert!(!row.enabled);
        stop.set();
        supervisor.join().unwrap();
        assert_eq!(ffmpeg.run_count(), 3); // A brief start before Stop is intentional.
        assert!(
            ffmpeg
                .runs()
                .iter()
                .all(|run| run.lock().unwrap().code.is_some())
        );
        let saved = crate::statefile::StreamStateStore::new(&config.state_path)
            .load_streams()
            .unwrap();
        let udp = saved.iter().find(|(name, _)| name == "boats-udp").unwrap();
        assert!(udp.1.enabled);
        drop((state, videos, hls));
    }

    #[test]
    fn file_observation_on_shared_queue_runs_on_owner_and_parks_its_streams() {
        let Harness {
            mut registry,
            source,
            videos,
            state,
            hls,
            ..
        } = setup();
        let (sender, receiver) = sync_channel(8);
        let stop = StopSignal::new();
        let applied = StopSignal::new();
        type Published = Vec<(thread::ThreadId, Vec<(StreamState, Option<String>)>)>;
        let seen: Arc<Mutex<Published>> = Arc::new(Mutex::new(Vec::new()));
        std::fs::remove_file(&source).unwrap();
        let name = source.file_name().unwrap().to_string_lossy().into_owned();
        sender
            .send(SupervisorRequest::Observation(Inventory {
                deletes: vec![name],
                ..Inventory::default()
            }))
            .unwrap();
        let loop_stop = stop.clone();
        let recorder = seen.clone();
        let applied_signal = applied.clone();
        let supervisor = thread::spawn(move || {
            run_loop(
                &mut registry,
                &receiver,
                &mut |registry| {
                    let rows = registry
                        .status()
                        .into_iter()
                        .map(|row| (row.state, row.reason))
                        .collect();
                    recorder
                        .lock()
                        .unwrap()
                        .push((thread::current().id(), rows));
                    applied_signal.set();
                },
                &loop_stop,
            );
        });
        assert!(applied.wait(Duration::from_secs(2)));
        let owner = supervisor.thread().id();
        stop.set();
        supervisor.join().unwrap();
        let seen = seen.lock().unwrap();
        let (publisher, rows) = &seen[0];
        assert_eq!(*publisher, owner);
        assert!(rows.iter().all(|(state, reason)| {
            *state == StreamState::Waiting && reason.as_deref() == Some("source file missing")
        }));
        drop((videos, state, hls));
    }

    #[test]
    fn shutdown_fails_pending_requests_and_stops_children_without_saving() {
        let Harness {
            mut registry,
            ffmpeg,
            config,
            ..
        } = setup();
        let (sender, receiver) = sync_channel(8);
        let stop = StopSignal::new();
        stop.set();
        let (request, reply) = operation(|_| Ok(success()));
        sender.send(request).unwrap();
        run_loop(&mut registry, &receiver, &mut |_| {}, &stop);
        assert_eq!(
            reply.blocking_recv().unwrap(),
            Err("Supervisor is shutting down".to_string())
        );
        assert!(
            ffmpeg
                .runs()
                .iter()
                .all(|run| run.lock().unwrap().code.is_some())
        );
        let saved = crate::statefile::StreamStateStore::new(&config.state_path)
            .load_streams()
            .unwrap();
        assert!(saved.iter().all(|(_, record)| record.enabled));
    }

    #[test]
    fn full_queue_preserves_event_and_shutdown_interrupts_wait() {
        let (sender, receiver) = sync_channel(1);
        sender
            .send(SupervisorRequest::Observation(inventory(&[])))
            .unwrap();
        let stop = StopSignal::new();
        let observed = StopSignal::new();
        let watcher = {
            let sender = sender.clone();
            let stop = stop.clone();
            let observed = observed.clone();
            thread::spawn(move || {
                let mut delivered = false;
                let scan_stop = stop.clone();
                watch_files_with(
                    move || {
                        if delivered {
                            scan_stop.set();
                            return Err(io::Error::other("done"));
                        }
                        delivered = true;
                        observed.set();
                        Ok(DirectoryScan::new())
                    },
                    sender,
                    stop,
                );
            })
        };
        assert!(observed.wait(Duration::from_secs(2)));
        thread::sleep(Duration::from_millis(50));
        let _occupied = receiver.recv().unwrap();
        let event = receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(
            event,
            SupervisorRequest::Observation(inventory) if inventory.present.is_empty()
        ));
        watcher.join().unwrap();

        sender
            .send(SupervisorRequest::Observation(inventory(&[])))
            .unwrap();
        let stop = StopSignal::new();
        let blocked = {
            let sender = sender.clone();
            let stop = stop.clone();
            thread::spawn(move || submit_observation(&sender, inventory(&["late.mp4"]), &stop))
        };
        thread::sleep(Duration::from_millis(50));
        stop.set();
        blocked.join().unwrap();
        assert!(receiver.try_recv().is_ok());
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn unexpected_failure_signals_shutdown() {
        let (sender, _receiver) = sync_channel(8);
        let stop = StopSignal::new();
        let previous_hook = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        watch_files_with(|| panic!("broken watcher"), sender, stop.clone());
        std::panic::set_hook(previous_hook);
        assert!(stop.is_set());
    }
}
