//! One stream's behavior: a video delivered over one transport, called exclusively by the supervisor thread.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use crate::ffmpegcmd::{self, MediaProbeResult, PreparedCommand};
use crate::ffmpegprocess::StreamProcess;
use crate::statefile::StreamStateStore;
use crate::streamlog::StreamLogger;
use crate::streamrecord::{StreamBinding, StreamChoices, StreamRecord, VideoMode};
use crate::streamresponses::{PreparedFacts, StreamState, StreamStatus};
use crate::transports::Muxer;
use crate::udphost::HostResolver;

pub use crate::ffmpegcmd::Delivery;

/// A listener that has run this long was serving a client; its exit is the client leaving, not a crash.
pub const CLIENT_SERVED_SECONDS: Duration = Duration::from_secs(5);
/// A run that lasted this long is healthy again; earlier crashes are forgotten.
const STABLE_RUN: Duration = Duration::from_secs(60);

/// The ffmpeg boundary: probing a file and spawning a run.
pub trait FfmpegBoundary: Send {
    fn probe(&self, path: &Path) -> MediaProbeResult;
    fn spawn(&self, logger: Arc<StreamLogger>) -> Box<dyn StreamProcess>;
}

/// The collaborators every stream operation needs, owned by the registry and lent per call.
pub struct StreamServices {
    pub store: StreamStateStore,
    pub resolver: Box<dyn HostResolver>,
    pub ffmpeg: Box<dyn FfmpegBoundary>,
}

/// A setting that can be left alone, returned to automatic, or set.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub enum Adjustment<T> {
    #[default]
    Keep,
    Auto,
    Set(T),
}

impl<T: Copy> Adjustment<T> {
    fn applied_to(self, current: Option<T>) -> Option<T> {
        match self {
            Adjustment::Keep => current,
            Adjustment::Auto => None,
            Adjustment::Set(value) => Some(value),
        }
    }
}

/// What a settings request asks to change; absent fields leave the setting alone.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct StreamChanges {
    pub name: Option<String>,
    pub filename: Option<String>,
    pub transport: Option<String>,
    pub loop_count: Option<i64>,
    pub mode: Option<VideoMode>,
    pub gop_frames: Adjustment<i64>,
    pub frame_rate: Adjustment<f64>,
}

impl StreamChanges {
    /// Whether anything besides the name is requested.
    pub fn changes_more_than_the_name(&self) -> bool {
        self.filename.is_some()
            || self.transport.is_some()
            || self.loop_count.is_some()
            || self.mode.is_some()
            || self.gop_frames != Adjustment::Keep
            || self.frame_rate != Adjustment::Keep
    }

    pub fn applied_to(&self, choices: &StreamChoices) -> StreamChoices {
        StreamChoices {
            enabled: choices.enabled,
            mode: self.mode.or(choices.mode),
            loop_count: self.loop_count.unwrap_or(choices.loop_count),
            frame_rate: self.frame_rate.applied_to(choices.frame_rate),
            gop_frames: self.gop_frames.applied_to(choices.gop_frames),
        }
    }
}

pub struct MediaStream {
    name: String,
    source: PathBuf,
    binding: StreamBinding,
    choices: StreamChoices,
    delivery: Delivery,
    max_video_bps: Option<i64>,
    logger: Arc<StreamLogger>,
    run: Option<Box<dyn StreamProcess>>,
    completed: bool,
    crashes: u32,
    error: Option<String>,
    prepared: Option<PreparedCommand>,
}

impl MediaStream {
    pub fn new(
        name: &str,
        source_path: &Path,
        binding: StreamBinding,
        choices: StreamChoices,
        delivery: Delivery,
        max_video_bps: Option<i64>,
        logger: Arc<StreamLogger>,
    ) -> Self {
        Self {
            name: name.to_string(),
            source: source_path.to_path_buf(),
            binding,
            choices,
            delivery,
            max_video_bps,
            logger,
            run: None,
            completed: false,
            crashes: 0,
            error: None,
            prepared: None,
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn binding(&self) -> &StreamBinding {
        &self.binding
    }

    pub fn delivery(&self) -> &Delivery {
        &self.delivery
    }

    pub fn enabled(&self) -> bool {
        self.choices.enabled
    }

    fn save(&self, choices: &StreamChoices, store: &mut StreamStateStore) -> io::Result<()> {
        store.save_stream(&self.name, &StreamRecord::new(&self.binding, choices))
    }

    pub fn start_streaming(&mut self, services: &mut StreamServices) -> io::Result<bool> {
        let choices = StreamChoices {
            enabled: true,
            ..self.choices.clone()
        };
        self.save(&choices, &mut services.store)?;
        self.choices = choices;
        self.reset_health();
        Ok(self.restart(services, None))
    }

    pub fn ensure_streaming(&mut self, services: &mut StreamServices) -> io::Result<bool> {
        if self.choices.enabled
            && let Some(run) = self.run.as_mut()
            && run.observe().returncode.is_none()
        {
            return Ok(true);
        }
        self.start_streaming(services)
    }

    pub fn stop_streaming(&mut self, services: &mut StreamServices) -> io::Result<bool> {
        self.choices.enabled = false;
        // Even when saving fails, stop now; propagate the failure to the caller.
        let saved = self.save(&self.choices.clone(), &mut services.store);
        let stopped = self.stop_run();
        saved?;
        Ok(stopped)
    }

    pub fn update_settings(
        &mut self,
        changes: &StreamChanges,
        services: &mut StreamServices,
    ) -> io::Result<bool> {
        let source = self.source.clone();
        let binding = self.binding.clone();
        let delivery = self.delivery.clone();
        self.rebind(&source, binding, delivery, changes, services)
    }

    /// Play another file or use another transport, with any setting changes, in one restart.
    pub fn rebind(
        &mut self,
        source_path: &Path,
        binding: StreamBinding,
        delivery: Delivery,
        changes: &StreamChanges,
        services: &mut StreamServices,
    ) -> io::Result<bool> {
        self.source = source_path.to_path_buf();
        self.binding = binding;
        self.delivery = delivery;
        let choices = changes.applied_to(&self.choices);
        self.save(&choices, &mut services.store)?;
        self.choices = choices;
        self.reset_health();
        if self.choices.enabled {
            return Ok(self.restart(services, None));
        }
        Ok(self.refresh_preview(services))
    }

    /// Carry a new name and the delivery derived from it; the saved record moves with it.
    /// The caller decides whether the process restarts.
    pub fn rename(
        &mut self,
        name: &str,
        delivery: Delivery,
        logger: Arc<StreamLogger>,
        store: &mut StreamStateStore,
    ) -> io::Result<()> {
        store.rename_stream(&self.name, name)?;
        self.name = name.to_string();
        self.delivery = delivery;
        self.logger = logger;
        Ok(())
    }

    /// Registry-owned facts changed: restart only when the command they produce differs.
    pub fn settings_changed(
        &mut self,
        delivery: Delivery,
        max_video_bps: Option<i64>,
        services: &mut StreamServices,
    ) -> bool {
        self.delivery = delivery;
        self.max_video_bps = max_video_bps;
        if self.blocker(services).is_some() {
            self.prepared = None;
            return if self.choices.enabled {
                self.stop_run()
            } else {
                true
            };
        }
        let prepared = self.prepare(services);
        let running_same_command = self.run.as_mut().is_some_and(|run| {
            let observation = run.observe();
            observation.returncode.is_none() && observation.argv == prepared.argv
        });
        if !self.choices.enabled || running_same_command {
            self.prepared = Some(prepared);
            return true;
        }
        self.reset_health();
        self.restart(services, Some(prepared))
    }

    /// Show what would run without starting a stopped stream.
    fn refresh_preview(&mut self, services: &mut StreamServices) -> bool {
        if self.blocker(services).is_some() {
            self.prepared = None;
            return true;
        }
        self.prepared = Some(self.prepare(services));
        true
    }

    fn prepare(&self, services: &StreamServices) -> PreparedCommand {
        let profile = services.ffmpeg.probe(&self.source);
        ffmpegcmd::command_for_profile(
            &self.source,
            &self.choices,
            &self.delivery,
            &profile,
            self.max_video_bps,
        )
    }

    fn reset_health(&mut self) {
        self.completed = false;
        self.crashes = 0;
        self.error = None;
    }

    fn stop_run(&mut self) -> bool {
        let Some(run) = self.run.as_mut() else {
            return true;
        };
        if !run.stop() {
            self.error =
                Some("Previous process has not exited; destination remains occupied".to_string());
            return false;
        }
        self.run = None;
        true
    }

    pub fn source_changed(&mut self, services: &mut StreamServices) -> bool {
        self.reset_health();
        if self.choices.enabled {
            return self.restart(services, None);
        }
        self.refresh_preview(services)
    }

    /// Stop without changing saved intent; the registry owns membership.
    pub fn retire(&mut self) -> bool {
        self.stop_run()
    }

    fn blocker(&self, services: &StreamServices) -> Option<String> {
        if !self.source.is_file() {
            return Some("source file missing".to_string());
        }
        if self.delivery.url.is_none() {
            return self.delivery.blocker.clone();
        }
        if let Some(host) = &self.delivery.resolve_host {
            return services.resolver.start_blocker(host);
        }
        None
    }

    fn restart(
        &mut self,
        services: &mut StreamServices,
        prepared: Option<PreparedCommand>,
    ) -> bool {
        if self.blocker(services).is_some() {
            // A missing file or destination is accepted intent: the next
            // supervision tick starts the stream once it clears.
            self.prepared = None;
            return self.stop_run();
        }
        let prepared = prepared.unwrap_or_else(|| self.prepare(services));
        if let Some(error) = &prepared.error {
            self.error = Some(error.clone());
            if self.run.is_none() {
                self.prepared = Some(prepared);
            }
            return false;
        }
        if !self.stop_run() {
            return false;
        }
        let argv = prepared.argv.clone();
        self.prepared = Some(prepared);
        let launched = (|| -> io::Result<()> {
            if self.delivery.muxer == Muxer::Hls
                && let Some(parent) = self
                    .delivery
                    .url
                    .as_deref()
                    .and_then(|url| Path::new(url).parent())
            {
                std::fs::create_dir_all(parent)?;
            }
            let mut run = services.ffmpeg.spawn(self.logger.clone());
            run.start(&argv)?;
            self.run = Some(run);
            Ok(())
        })();
        match launched {
            Ok(()) => true,
            Err(error) => {
                self.error = Some(error.to_string());
                log::error!("Cannot start {}: {}", self.name, error);
                false
            }
        }
    }

    pub fn supervise(&mut self, services: &mut StreamServices) {
        if !self.choices.enabled {
            self.stop_run();
            return;
        }
        if let Some(run) = self.run.as_mut() {
            let observation = run.observe();
            if observation.returncode.is_none() {
                if observation.running_for >= STABLE_RUN {
                    if self.crashes > 0 {
                        self.error = None;
                    }
                    self.crashes = 0;
                }
                return;
            }
            self.run = None;
            let loops: i64 = observation
                .argv
                .iter()
                .position(|argument| argument == "-stream_loop")
                .and_then(|index| observation.argv.get(index + 1))
                .and_then(|count| count.parse().ok())
                .unwrap_or(-1);
            if observation.returncode == Some(0) && loops >= 0 {
                self.completed = true;
            } else if self.delivery.client_bound && observation.running_for >= CLIENT_SERVED_SECONDS
            {
                self.error = None; // The client left; listen again.
            } else {
                self.crashes += 1;
                self.error = Some(format!(
                    "FFmpeg exited {}: {}",
                    observation.returncode.unwrap_or(-1),
                    observation.diagnostics
                ));
            }
        }
        let prepared_error = self
            .prepared
            .as_ref()
            .is_some_and(|prepared| prepared.error.is_some());
        if !self.completed && self.blocker(services).is_none() && !prepared_error {
            self.restart(services, None);
        }
    }

    pub fn status(&mut self, services: &StreamServices) -> StreamStatus {
        let observation = self.run.as_mut().map(|run| run.observe());
        let running = observation
            .as_ref()
            .is_some_and(|observation| observation.returncode.is_none());
        let (state, reason) = self.state(running, services);
        let argv = observation
            .map(|observation| observation.argv)
            .or_else(|| self.prepared.as_ref().map(|prepared| prepared.argv.clone()))
            .unwrap_or_default();
        // Controls describe requested values, even if an old command still runs.
        let source_rate = self
            .prepared
            .as_ref()
            .and_then(|prepared| prepared.source_frame_rate);
        let frame_rate = self.choices.frame_rate.or(source_rate);
        StreamStatus {
            facts: self.prepared.as_ref().map(|prepared| PreparedFacts {
                video_codecs: prepared.video_codecs.clone(),
                audio_codecs: prepared.audio_codecs.clone(),
                data_codecs: prepared.data_codecs.clone(),
                effective_mode: prepared.effective_mode.as_str(),
                encoder: prepared.encoder,
                passthrough_eligible: prepared.passthrough_eligible,
                ineligible_codecs: prepared.ineligible_codecs.clone(),
                source_frame_rate: prepared.source_frame_rate,
            }),
            mode: self
                .choices
                .mode
                .map(VideoMode::as_str)
                .or_else(|| {
                    self.prepared
                        .as_ref()
                        .map(|prepared| prepared.mode.as_str())
                })
                .unwrap_or("transcode"),
            frame_rate,
            frame_rate_explicit: self.choices.frame_rate.is_some(),
            gop_frames: self
                .choices
                .gop_frames
                .unwrap_or_else(|| ffmpegcmd::natural_gop_frames(frame_rate)),
            gop_explicit: self.choices.gop_frames.is_some(),
            enabled: self.choices.enabled,
            loop_count: self.choices.loop_count,
            filename: self.binding.filename.clone(),
            transport: self.binding.transport.clone(),
            port: self.binding.port,
            name: self.name.clone(),
            state,
            reason,
            running,
            stopping: running && !self.choices.enabled,
            command: (!argv.is_empty()).then(|| ffmpegcmd::shell_join(&argv)),
            error: self.error.clone(),
            delivery: self.delivery.url.clone(),
        }
    }

    fn state(&self, running: bool, services: &StreamServices) -> (StreamState, Option<String>) {
        if !self.choices.enabled {
            return if running {
                (
                    StreamState::Stopping,
                    Some("waiting for process exit".to_string()),
                )
            } else {
                (StreamState::Stopped, Some("stopped".to_string()))
            };
        }
        if running {
            return if self.crashes > 0 {
                (
                    StreamState::Crashing,
                    Some(format!("died {}x; relaunched, watching", self.crashes)),
                )
            } else {
                (StreamState::Running, self.error.clone())
            };
        }
        if self.completed {
            return (
                StreamState::Completed,
                Some("played to the end".to_string()),
            );
        }
        if let Some(blocker) = self.blocker(services) {
            return (StreamState::Waiting, Some(blocker));
        }
        if self.crashes > 0 {
            return (
                StreamState::Restarting,
                Some(format!("died {}x; relaunching", self.crashes)),
            );
        }
        if let Some(error) = &self.error {
            return (StreamState::Failed, Some(error.clone()));
        }
        (StreamState::Starting, Some("starting".to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::streamresponses::StreamState;
    use crate::testsupport::{Harness, block_writes};

    fn setup() -> Harness {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        harness.create("boats-udp", &source, "udp");
        harness
    }

    fn update(harness: &mut Harness, name: &str, changes: StreamChanges) -> io::Result<bool> {
        let (stream, services) = harness.registry.stream_mut(name).unwrap();
        stream.update_settings(&changes, services)
    }

    fn stop(harness: &mut Harness, name: &str) -> io::Result<bool> {
        let (stream, services) = harness.registry.stream_mut(name).unwrap();
        stream.stop_streaming(services)
    }

    fn start(harness: &mut Harness, name: &str) -> io::Result<bool> {
        let (stream, services) = harness.registry.stream_mut(name).unwrap();
        stream.start_streaming(services)
    }

    fn supervise(harness: &mut Harness, name: &str) {
        let (stream, services) = harness.registry.stream_mut(name).unwrap();
        stream.supervise(services);
    }

    fn source_changed(harness: &mut Harness, name: &str) -> bool {
        let (stream, services) = harness.registry.stream_mut(name).unwrap();
        stream.source_changed(services)
    }

    #[test]
    fn settings_and_stop_affect_only_one_stream_and_are_saved() {
        let mut harness = setup();
        let udp_run = harness.ffmpeg.run(1);
        let changes = StreamChanges {
            mode: Some(VideoMode::Transcode),
            frame_rate: Adjustment::Set(12.0),
            loop_count: Some(0),
            ..StreamChanges::default()
        };
        assert!(update(&mut harness, "boats", changes).unwrap());
        assert_eq!(harness.status("boats").frame_rate, Some(12.0));
        assert_eq!(harness.status("boats-udp").frame_rate, Some(30.0));
        assert_eq!(udp_run.lock().unwrap().code, None);
        assert!(stop(&mut harness, "boats").unwrap());
        assert_eq!(harness.status("boats").state, StreamState::Stopped);
        assert!(harness.status("boats-udp").running);
        let saved = harness.saved_stream("boats");
        assert!(!saved.enabled);
        assert_eq!(saved.loop_count, 0);
    }

    #[test]
    fn stuck_shutdown_prevents_replacement_and_reports_actual_command() {
        let mut harness = setup();
        let old = harness.ffmpeg.run(0);
        old.lock().unwrap().stuck = true;
        let command = harness.status("boats").command;
        let changes = StreamChanges {
            frame_rate: Adjustment::Set(12.0),
            ..StreamChanges::default()
        };
        assert!(!update(&mut harness, "boats", changes).unwrap());
        assert_eq!(harness.ffmpeg.run_count(), 2);
        assert_eq!(harness.status("boats").command, command);
        assert_eq!(harness.status("boats").frame_rate, Some(12.0));
        assert!(!harness.registry.stream_mut("boats").unwrap().0.retire());
        old.lock().unwrap().stuck = false;
        assert!(harness.registry.stream_mut("boats").unwrap().0.retire());
    }

    #[test]
    fn finite_completion_stays_complete_until_file_changes() {
        let mut harness = setup();
        let changes = StreamChanges {
            loop_count: Some(0),
            ..StreamChanges::default()
        };
        update(&mut harness, "boats", changes).unwrap();
        harness.ffmpeg.last_run().lock().unwrap().finish(0);
        let count = harness.ffmpeg.run_count();
        supervise(&mut harness, "boats");
        supervise(&mut harness, "boats");
        assert_eq!(harness.ffmpeg.run_count(), count);
        assert_eq!(harness.status("boats").state, StreamState::Completed);
        source_changed(&mut harness, "boats");
        assert!(harness.status("boats").running);
    }

    #[test]
    fn crash_recovery_then_stable_run_clears_history() {
        let mut harness = setup();
        {
            let handle = harness.ffmpeg.run(0);
            let mut run = handle.lock().unwrap();
            run.tail = "failure detail".into();
            run.finish(1);
        }
        supervise(&mut harness, "boats");
        assert_eq!(harness.status("boats").state, StreamState::Crashing);
        assert!(
            harness
                .status("boats")
                .error
                .unwrap()
                .contains("failure detail")
        );
        harness.ffmpeg.last_run().lock().unwrap().age = Duration::from_secs(61);
        supervise(&mut harness, "boats");
        assert_eq!(harness.status("boats").state, StreamState::Running);
        assert_eq!(harness.status("boats").error, None);
    }

    #[test]
    fn push_stream_waits_for_cached_dns_then_recovers() {
        let mut harness = setup();
        stop(&mut harness, "boats-udp").unwrap();
        harness.resolver.set_blocked(true);
        assert!(start(&mut harness, "boats-udp").unwrap());
        assert_eq!(harness.status("boats-udp").state, StreamState::Waiting);
        harness.resolver.set_blocked(false);
        supervise(&mut harness, "boats-udp");
        assert!(harness.status("boats-udp").running);
    }

    #[test]
    fn start_all_keeps_running_processes() {
        let mut harness = setup();
        assert!(harness.registry.start_all_streams().unwrap());
        assert_eq!(harness.ffmpeg.run_count(), 2);
    }

    #[test]
    fn status_never_probes_or_resolves() {
        let mut harness = setup();
        harness
            .ffmpeg
            .probing_forbidden
            .store(true, std::sync::atomic::Ordering::Relaxed);
        harness
            .resolver
            .refreshing_forbidden
            .store(true, std::sync::atomic::Ordering::Relaxed);
        assert!(harness.status("boats").running);
    }

    #[test]
    fn save_failure_is_reported_but_stop_is_honored() {
        let mut harness = setup();
        block_writes(&harness.state, true);
        let outcome = stop(&mut harness, "boats");
        block_writes(&harness.state, false);
        assert!(outcome.is_err());
        assert!(!harness.status("boats").running);
    }

    #[test]
    fn shutdown_does_not_save_disabled_choices() {
        let mut harness = setup();
        assert!(harness.registry.stop_all_streams(false).unwrap());
        assert!(
            harness
                .saved_streams()
                .iter()
                .all(|(_, record)| record.enabled)
        );
        assert!(
            harness
                .ffmpeg
                .runs()
                .iter()
                .all(|run| run.lock().unwrap().code.is_some())
        );
    }

    #[test]
    fn stopped_stream_settings_refresh_preview_without_start() {
        let mut harness = setup();
        stop(&mut harness, "boats").unwrap();
        let count = harness.ffmpeg.run_count();
        let changes = StreamChanges {
            mode: Some(VideoMode::Transcode),
            frame_rate: Adjustment::Set(12.0),
            ..StreamChanges::default()
        };
        update(&mut harness, "boats", changes).unwrap();
        source_changed(&mut harness, "boats");
        assert_eq!(harness.ffmpeg.run_count(), count);
        assert!(harness.status("boats").command.unwrap().contains("-r 12"));
    }

    #[test]
    fn invalid_media_waits_for_file_change_without_launch_loop() {
        let mut harness = setup();
        stop(&mut harness, "boats").unwrap();
        let count = harness.ffmpeg.run_count();
        harness.ffmpeg.set_profile(MediaProbeResult::UNKNOWN);
        let probes_before = harness
            .ffmpeg
            .probes
            .load(std::sync::atomic::Ordering::Relaxed);
        assert!(!start(&mut harness, "boats").unwrap());
        supervise(&mut harness, "boats");
        supervise(&mut harness, "boats");
        let probes_after = harness
            .ffmpeg
            .probes
            .load(std::sync::atomic::Ordering::Relaxed);
        assert_eq!(probes_after - probes_before, 1);
        assert_eq!(harness.status("boats").state, StreamState::Failed);
        assert_eq!(harness.ffmpeg.run_count(), count);
        harness
            .ffmpeg
            .set_profile(crate::testsupport::test_profile());
        assert!(source_changed(&mut harness, "boats"));
        assert!(harness.status("boats").running);
    }

    #[test]
    fn a_listener_whose_client_left_listens_again_without_a_crash() {
        let mut harness = setup();
        let source = harness.source_name();
        harness.create("boats-srt", &source, "srt_listen");
        {
            let handle = harness.ffmpeg.last_run();
            let mut run = handle.lock().unwrap();
            run.age = Duration::from_secs(10);
            run.finish(152);
        }
        supervise(&mut harness, "boats-srt");
        let status = harness.status("boats-srt");
        assert_eq!((status.state, status.error), (StreamState::Running, None));
        assert_eq!(harness.ffmpeg.run_count(), 4);
        harness.ffmpeg.last_run().lock().unwrap().finish(1);
        supervise(&mut harness, "boats-srt");
        assert_eq!(harness.status("boats-srt").state, StreamState::Crashing);
    }

    #[test]
    fn status_reports_the_source_tracks_and_the_binding() {
        let mut harness = setup();
        let status = harness.status("boats-udp");
        let facts = status.facts.unwrap();
        assert_eq!(facts.video_codecs, ["h264"]);
        assert_eq!(facts.audio_codecs, ["aac"]);
        assert_eq!(facts.data_codecs, ["klv"]);
        assert_eq!(
            (
                status.name.as_str(),
                status.filename,
                status.transport.as_str(),
                status.port
            ),
            ("boats-udp", harness.source_name(), "udp", Some(40000))
        );
    }
}
