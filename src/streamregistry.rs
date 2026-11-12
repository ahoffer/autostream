//! Stream membership, ports and bindings, owned by the supervisor loop.

use std::collections::{BTreeSet, HashSet};
use std::fmt;
use std::io;
use std::path::Path;
use std::sync::Arc;

use indexmap::IndexMap;
use rand::seq::IndexedRandom;

use crate::appconfig::Config;
use crate::filewatch::Inventory;
use crate::mediastream::{Delivery, MediaStream, StreamChanges, StreamServices};
use crate::runtimesettings::RuntimeSettings;
use crate::statefile::is_valid_stream_name;
use crate::streamlog::StreamLogger;
use crate::streamrecord::{StreamBinding, StreamChoices, StreamRecord};
use crate::streamresponses::StreamStatus;
use crate::transports::{Direction, TemplateFields, TransportKind, kind_by_id};

/// Random ports sit above every first port a transport can be given and below the ephemeral range.
pub const RANDOM_PORT_LOW: u16 = 50000;
pub const RANDOM_PORT_HIGH: u16 = 59999;
/// A first start seeds the videos already present as streams of this kind.
pub const SEED_TRANSPORT: &str = "rtsp";
pub const STREAM_NAME_LENGTH: usize = 64;

/// Why a mutation did not happen: refused on its merits, or failed in storage or a process.
#[derive(Debug)]
pub enum OperationError {
    Refused(String),
    Io(io::Error),
}

impl fmt::Display for OperationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OperationError::Refused(reason) => formatter.write_str(reason),
            OperationError::Io(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for OperationError {}

impl From<io::Error> for OperationError {
    fn from(error: io::Error) -> Self {
        OperationError::Io(error)
    }
}

pub fn is_ignored(path: &Path) -> bool {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    name.starts_with('.') || name.to_lowercase().starts_with("readme")
}

/// The play page plays a file through the first stream, in creation order,
/// that serves that file through MediaMTX.
pub fn is_play_stream(stream_filename: &str, stream_transport: &str, filename: &str) -> bool {
    stream_filename == filename && stream_transport == SEED_TRANSPORT
}

/// The file's stem as a valid stream name: runs of other characters become one dash,
/// the name starts with a letter or digit, and a name already taken gets a number.
pub fn seeded_stream_name(filename: &str, taken: &dyn Fn(&str) -> bool) -> String {
    let stem = Path::new(filename)
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy();
    let mut base = String::new();
    let mut in_run = false;
    for character in stem.chars() {
        if character.is_ascii_alphanumeric() || character == '_' || character == '-' {
            base.push(character);
            in_run = false;
        } else if !in_run {
            base.push('-');
            in_run = true;
        }
    }
    let base = base
        .trim_start_matches(|character: char| !character.is_ascii_alphanumeric())
        .trim_end_matches('-');
    let base = if base.is_empty() { "video" } else { base };
    let mut name = base[..base.len().min(STREAM_NAME_LENGTH)].to_string();
    let mut ordinal = 2;
    while taken(&name) {
        let suffix = format!("-{ordinal}");
        name = format!(
            "{}{suffix}",
            &base[..base.len().min(STREAM_NAME_LENGTH - suffix.len())]
        );
        ordinal += 1;
    }
    name
}

/// Opens the ffmpeg log for a stream name.
pub type LoggerFactory = Box<dyn Fn(&str) -> io::Result<Arc<StreamLogger>> + Send>;

/// Chooses one of the free random-range ports; tests pin the choice.
pub type RandomPortPicker = Box<dyn Fn(&[u16]) -> Option<u16> + Send>;

fn random_port_picker() -> RandomPortPicker {
    Box::new(|candidates| candidates.choose(&mut rand::rng()).copied())
}

pub struct StreamRegistry {
    config: Config,
    services: StreamServices,
    logger_factory: LoggerFactory,
    pick_random_port: RandomPortPicker,
    // Name to stream, in creation order: the UI lists them in that order.
    streams: IndexMap<String, MediaStream>,
    settings: RuntimeSettings,
    videos: Vec<String>,
    /// First start: videos become streams until the first stream exists.
    seeding: bool,
}

impl StreamRegistry {
    pub fn new(config: Config, services: StreamServices, logger_factory: LoggerFactory) -> Self {
        Self {
            config,
            services,
            logger_factory,
            pick_random_port: random_port_picker(),
            streams: IndexMap::new(),
            settings: RuntimeSettings::default(),
            videos: Vec::new(),
            seeding: false,
        }
    }

    /// Replace the random port choice, so tests can make multicast ports deterministic.
    pub fn set_random_port_picker(&mut self, picker: RandomPortPicker) {
        self.pick_random_port = picker;
    }

    pub fn load_persisted_state(&mut self) -> io::Result<()> {
        self.seeding = self.services.store.is_fresh();
        self.settings = self.services.store.load_settings()?;
        for (name, record) in self.services.store.load_streams()? {
            let stream = self.build(&name, record.binding(), record.choices())?;
            self.streams.insert(name, stream);
        }
        Ok(())
    }

    fn build(
        &self,
        name: &str,
        binding: StreamBinding,
        choices: StreamChoices,
    ) -> io::Result<MediaStream> {
        let delivery = self.delivery_for(name, &binding);
        Ok(MediaStream::new(
            name,
            &self.config.videos_dir.join(&binding.filename),
            binding,
            choices,
            delivery,
            self.settings.max_video_bps(),
            (self.logger_factory)(name)?,
        ))
    }

    pub fn settings(&self) -> &RuntimeSettings {
        &self.settings
    }

    /// Stable files from the last inventory, offered as sources.
    pub fn videos(&self) -> &[String] {
        &self.videos
    }

    pub fn has_stream(&self, name: &str) -> bool {
        self.streams.contains_key(name)
    }

    pub fn stream_names(&self) -> Vec<String> {
        self.streams.keys().cloned().collect()
    }

    /// One stream and the services its operations need; None when no such stream exists.
    pub fn stream_mut(&mut self, name: &str) -> Option<(&mut MediaStream, &mut StreamServices)> {
        let stream = self.streams.get_mut(name)?;
        Some((stream, &mut self.services))
    }

    pub fn stream_status(&mut self, name: &str) -> Option<StreamStatus> {
        let (stream, services) = self.stream_mut(name)?;
        Some(stream.status(services))
    }

    /// A new stream on a free port of its transport; refused with a reason when it cannot be made.
    pub fn create_stream(
        &mut self,
        name: &str,
        filename: &str,
        transport: &str,
        changes: &StreamChanges,
        start: bool,
    ) -> Result<(), OperationError> {
        let refuse = |reason: String| Err(OperationError::Refused(reason));
        if !is_valid_stream_name(name) {
            return refuse("Stream name: letters, digits, - and _ only, 64 at most".to_string());
        }
        if self.streams.contains_key(name) {
            return refuse(format!("A stream named {name} exists"));
        }
        let Some(kind) = kind_by_id(transport) else {
            return refuse("Unknown transport".to_string());
        };
        if filename.is_empty() || filename.contains('/') || filename.starts_with('.') {
            return refuse("Choose a video file".to_string());
        }
        let port = self.free_port(kind, None);
        if kind.takes_port() && port.is_none() {
            return refuse(format!("No free port left for {}", kind.label));
        }
        let binding = StreamBinding {
            filename: filename.to_string(),
            transport: transport.to_string(),
            port,
        };
        let choices = StreamChoices {
            enabled: start,
            ..changes.applied_to(&StreamChoices::default())
        };
        let mut stream = self.build(name, binding.clone(), choices.clone())?;
        self.services
            .store
            .save_stream(name, &StreamRecord::new(&binding, &choices))?;
        stream.source_changed(&mut self.services);
        self.streams.insert(name.to_string(), stream);
        Ok(())
    }

    pub fn delete_stream(&mut self, name: &str) -> Result<bool, OperationError> {
        let Some(stream) = self.streams.get_mut(name) else {
            return Err(OperationError::Refused("Stream not found".to_string()));
        };
        if !stream.retire() {
            return Ok(false);
        }
        self.services.store.delete_stream(name)?;
        self.streams.shift_remove(name);
        let _ = std::fs::remove_dir_all(self.config.hls_dir.join(name));
        Ok(true)
    }

    /// Apply setting changes; a new name moves the record, and a new file or
    /// transport rebinds the stream, all in the same restart.
    pub fn update_stream(
        &mut self,
        name: &str,
        changes: &StreamChanges,
    ) -> Result<bool, OperationError> {
        if !self.streams.contains_key(name) {
            return Err(OperationError::Refused("Stream not found".to_string()));
        }
        let new_name = changes.name.clone().unwrap_or_else(|| name.to_string());
        if new_name != name {
            self.rename_stream(name, &new_name)?;
            if !changes.changes_more_than_the_name() {
                // Only the name changed: restart only the kinds whose command names the stream.
                let stream = &self.streams[new_name.as_str()];
                let delivery = stream.delivery().clone();
                let max_video_bps = self.settings.max_video_bps();
                let stream = &mut self.streams[new_name.as_str()];
                return Ok(stream.settings_changed(delivery, max_video_bps, &mut self.services));
            }
        }
        let binding = self.streams[new_name.as_str()].binding().clone();
        let filename = changes
            .filename
            .clone()
            .unwrap_or_else(|| binding.filename.clone());
        let transport = changes
            .transport
            .clone()
            .unwrap_or_else(|| binding.transport.clone());
        let Some(kind) = kind_by_id(&transport) else {
            return Err(OperationError::Refused("Unknown transport".to_string()));
        };
        if filename == binding.filename && transport == binding.transport {
            let stream = &mut self.streams[new_name.as_str()];
            return Ok(stream.update_settings(changes, &mut self.services)?);
        }
        let port = if transport == binding.transport {
            binding.port
        } else {
            self.free_port(kind, Some(&new_name))
        };
        let new_binding = StreamBinding {
            filename: filename.clone(),
            transport,
            port,
        };
        let delivery = self.delivery_for(&new_name, &new_binding);
        let source = self.config.videos_dir.join(&filename);
        let stream = &mut self.streams[new_name.as_str()];
        Ok(stream.rebind(&source, new_binding, delivery, changes, &mut self.services)?)
    }

    /// Move a stream to a new name in place; its port, choices and position in the list stay.
    fn rename_stream(&mut self, name: &str, new_name: &str) -> Result<(), OperationError> {
        if !is_valid_stream_name(new_name) {
            return Err(OperationError::Refused(
                "Stream name: letters, digits, - and _ only, 64 at most".to_string(),
            ));
        }
        if self.streams.contains_key(new_name) {
            return Err(OperationError::Refused(format!(
                "A stream named {new_name} exists"
            )));
        }
        let binding = self.streams[name].binding().clone();
        let delivery = self.delivery_for(new_name, &binding);
        let logger = (self.logger_factory)(new_name)?;
        self.streams[name].rename(new_name, delivery, logger, &mut self.services.store)?;
        let renamed: IndexMap<String, MediaStream> = std::mem::take(&mut self.streams)
            .into_iter()
            .map(|(key, stream)| {
                let key = if key == name {
                    new_name.to_string()
                } else {
                    key
                };
                (key, stream)
            })
            .collect();
        self.streams = renamed;
        let _ = std::fs::remove_dir_all(self.config.hls_dir.join(name));
        Ok(())
    }

    fn free_port(&self, kind: &TransportKind, except_name: Option<&str>) -> Option<u16> {
        if !kind.takes_port() {
            return None;
        }
        let used: HashSet<u16> = self
            .streams
            .iter()
            .filter(|(name, _)| Some(name.as_str()) != except_name)
            .filter_map(|(_, stream)| stream.binding().port)
            .collect();
        if kind.random_port {
            let candidates: Vec<u16> = (RANDOM_PORT_LOW..=RANDOM_PORT_HIGH)
                .filter(|port| !used.contains(port))
                .collect();
            return (self.pick_random_port)(&candidates);
        }
        let mut port = u32::from(self.settings.first_port(kind.first_port_setting?));
        while port <= 65535 && used.contains(&(port as u16)) {
            port += 1;
        }
        u16::try_from(port).ok()
    }

    fn delivery_for(&self, name: &str, binding: &StreamBinding) -> Delivery {
        let Some(kind) = kind_by_id(&binding.transport) else {
            return Delivery::blocked("unknown transport");
        };
        let blocked = |blocker: &str| Delivery {
            muxer: kind.muxer,
            carries_klv: kind.carries_klv,
            ..Delivery::blocked(blocker)
        };
        if kind.takes_port() && binding.port.is_none() {
            return blocked("no free port");
        }
        for need in kind.needs {
            if !self.settings.satisfies(*need) {
                return blocked(&format!("no {} configured", need.name().replace('_', " ")));
            }
        }
        let hls_dir = self.config.hls_dir.to_string_lossy();
        let fields = TemplateFields {
            destination_host: self.settings.destination_host.as_deref(),
            multicast_group: &self.settings.multicast_group,
            srtp_key: &self.settings.srtp_key,
            port: binding.port,
            name,
            rtsp_port: self.config.rtsp_port,
            srt_port: self.config.srt_port,
            hls_dir: &hls_dir,
            ..TemplateFields::default()
        };
        let needs_destination = kind
            .needs
            .contains(&crate::runtimesettings::RequiredSetting::DestinationHost);
        Delivery {
            url: Some(fields.fill(kind.delivery)),
            muxer: kind.muxer,
            carries_klv: kind.carries_klv,
            extra_args: kind
                .extra_output_args
                .iter()
                .map(|argument| fields.fill(argument))
                .collect(),
            resolve_host: if needs_destination {
                self.settings.destination_host.clone()
            } else {
                None
            },
            client_bound: kind.direction == Direction::Listen,
            blocker: None,
            clamp_data_dts: kind.clamp_data_dts,
        }
    }

    pub fn start_all_streams(&mut self) -> io::Result<bool> {
        let mut all_started = true;
        for stream in self.streams.values_mut() {
            all_started &= stream.ensure_streaming(&mut self.services)?;
        }
        Ok(all_started)
    }

    /// Stop every stream; with `persist` false the saved enabled choices are left alone.
    pub fn stop_all_streams(&mut self, persist: bool) -> io::Result<bool> {
        let mut all_stopped = true;
        let mut first_error = None;
        for stream in self.streams.values_mut() {
            let outcome = if persist {
                stream.stop_streaming(&mut self.services)
            } else {
                Ok(stream.retire())
            };
            match outcome {
                Ok(stopped) => all_stopped &= stopped,
                Err(error) => {
                    if first_error.is_none() {
                        first_error = Some(error);
                    }
                }
            }
        }
        match first_error {
            Some(error) => Err(error),
            None => Ok(all_stopped),
        }
    }

    /// One operation: persist, re-derive every delivery, restart what changed.
    pub fn apply_settings(&mut self, settings: RuntimeSettings) -> io::Result<bool> {
        self.services.store.save_settings(&settings.to_document())?;
        self.settings = settings;
        let max_video_bps = self.settings.max_video_bps();
        let deliveries: Vec<(String, Delivery)> = self
            .streams
            .iter()
            .map(|(name, stream)| (name.clone(), self.delivery_for(name, stream.binding())))
            .collect();
        let mut all_applied = true;
        for (name, delivery) in deliveries {
            if let Some(stream) = self.streams.get_mut(&name) {
                all_applied &= stream.settings_changed(delivery, max_video_bps, &mut self.services);
            }
        }
        Ok(all_applied)
    }

    /// Apply a watcher observation, or perform an ordinary supervision tick.
    pub fn supervise(&mut self, inventory: Option<&Inventory>) {
        if let Some(inventory) = inventory {
            let mut videos = inventory.stable.clone();
            videos.sort();
            self.videos = videos;
            let changed: HashSet<&str> = inventory.changed().collect();
            for stream in self.streams.values_mut() {
                if changed.contains(stream.binding().filename.as_str()) {
                    stream.source_changed(&mut self.services);
                }
            }
            if self.seeding {
                self.seed_streams(&inventory.creates);
            }
        }
        let hosts: BTreeSet<String> = self
            .streams
            .values()
            .filter_map(|stream| stream.delivery().resolve_host.clone())
            .collect();
        self.services.resolver.refresh(&hosts);
        for stream in self.streams.values_mut() {
            stream.supervise(&mut self.services);
        }
    }

    /// Start one MediaMTX stream per video that settled, named after its file; the first
    /// stream to exist, seeded or made by hand, ends seeding.
    fn seed_streams(&mut self, filenames: &[String]) {
        if !self.streams.is_empty() {
            self.seeding = false;
            return;
        }
        for filename in filenames {
            let name =
                seeded_stream_name(filename, &|candidate| self.streams.contains_key(candidate));
            match self.create_stream(
                &name,
                filename,
                SEED_TRANSPORT,
                &StreamChanges::default(),
                true,
            ) {
                Ok(()) => log::info!("First start: streaming {filename} as {name}"),
                Err(reason) => log::warn!("First start: cannot stream {filename}: {reason}"),
            }
        }
        self.seeding = self.streams.is_empty();
    }

    /// Start the file's play stream, made under its seeded name when the file has none yet.
    /// A loop count replaces the stream's saved one; none keeps it.
    pub fn play_file(
        &mut self,
        filename: &str,
        loop_count: Option<i64>,
    ) -> Result<bool, OperationError> {
        if !self.videos.iter().any(|video| video == filename) {
            return Err(OperationError::Refused(format!(
                "No video named {filename}"
            )));
        }
        let changes = StreamChanges {
            loop_count,
            ..StreamChanges::default()
        };
        match self.play_stream_name(filename) {
            Some(name) => {
                let (stream, services) = self.stream_mut(&name).expect("found above");
                Ok(stream.start_with(&changes, services)?)
            }
            None => {
                let name =
                    seeded_stream_name(filename, &|candidate| self.streams.contains_key(candidate));
                self.create_stream(&name, filename, SEED_TRANSPORT, &changes, true)?;
                Ok(true)
            }
        }
    }

    /// Stop the file's play stream; a file without one has nothing to stop.
    pub fn stop_file(&mut self, filename: &str) -> Result<bool, OperationError> {
        let Some(name) = self.play_stream_name(filename) else {
            return Ok(true);
        };
        let (stream, services) = self.stream_mut(&name).expect("found above");
        Ok(stream.stop_streaming(services)?)
    }

    fn play_stream_name(&self, filename: &str) -> Option<String> {
        self.streams
            .iter()
            .find(|(_, stream)| {
                let binding = stream.binding();
                is_play_stream(&binding.filename, &binding.transport, filename)
            })
            .map(|(name, _)| name.clone())
    }

    pub fn status(&mut self) -> Vec<StreamStatus> {
        let services = &self.services;
        self.streams
            .values_mut()
            .map(|stream| stream.status(services))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mediastream::Adjustment;
    use crate::statefile::StreamStateStore;
    use crate::streamrecord::VideoMode;
    use crate::streamresponses::{
        PlayChoice, StatusSnapshot, StreamState, UrlFacts, describe_play_rows, describe_streams,
    };
    use crate::testsupport::{
        Harness, block_writes, build_registry, fixture, inventory, test_settings,
    };
    use serde_json::Value;

    fn with_creates(stable: &[&str], creates: &[&str]) -> Inventory {
        Inventory {
            creates: creates.iter().map(|name| name.to_string()).collect(),
            ..inventory(stable)
        }
    }

    fn transcode() -> StreamChanges {
        StreamChanges {
            mode: Some(VideoMode::Transcode),
            ..StreamChanges::default()
        }
    }

    fn argv_of(run: &crate::testsupport::RunHandle) -> Vec<String> {
        run.lock().unwrap().argv.clone()
    }

    fn code_of(run: &crate::testsupport::RunHandle) -> Option<i32> {
        run.lock().unwrap().code
    }

    #[test]
    fn create_assigns_the_lowest_free_port_of_the_transport_and_starts() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats-udp", &source, "udp");
        harness.create("boats-srt", &source, "srt_listen");
        harness.create("boats-udp2", &source, "udp");
        assert_eq!(harness.status("boats-udp").port, Some(40000));
        assert_eq!(harness.status("boats-srt").port, Some(41000));
        assert_eq!(harness.status("boats-udp2").port, Some(40001));
        assert_eq!(
            harness.status("boats-udp").delivery.as_deref(),
            Some("udp://receiver:40000?pkt_size=1316")
        );
        assert_eq!(
            harness.status("boats-srt").delivery.as_deref(),
            Some("srt://0.0.0.0:41000?mode=listener")
        );
        assert!(harness.registry.status().iter().all(|row| row.running));
        assert_eq!(harness.ffmpeg.run_count(), 3);
        let saved = harness.saved_stream("boats-udp");
        assert_eq!(
            (
                saved.filename,
                saved.transport.as_str(),
                saved.port,
                saved.enabled
            ),
            (source, "udp", Some(40000), true)
        );
    }

    #[test]
    fn rtsp_and_hls_take_no_port_and_hls_writes_under_the_hls_directory() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        harness.create("boats-hls", &source, "hls");
        assert_eq!(harness.status("boats").port, None);
        assert_eq!(harness.status("boats-hls").port, None);
        assert_eq!(
            harness.status("boats").delivery.as_deref(),
            Some("srt://127.0.0.1:8890?streamid=publish:boats")
        );
        assert!(!argv_of(&harness.ffmpeg.run(0)).contains(&"-bsf:d".to_string()));
        let playlist = harness.config.hls_dir.join("boats-hls").join("index.m3u8");
        assert_eq!(
            harness.status("boats-hls").delivery.as_deref(),
            Some(playlist.to_str().unwrap())
        );
        assert!(harness.config.hls_dir.join("boats-hls").is_dir());
        assert!(argv_of(&harness.ffmpeg.run(1)).contains(&"-hls_segment_type".to_string()));
    }

    #[test]
    fn invalid_or_duplicate_creation_is_refused_with_a_reason() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        for (name, transport, filename, message) in [
            ("boats", "udp", source.as_str(), "exists"),
            ("bad name", "udp", source.as_str(), "letters"),
            ("x", "carrier-pigeon", source.as_str(), "Unknown transport"),
            ("y", "udp", "", "Choose a video file"),
        ] {
            let outcome = harness.registry.create_stream(
                name,
                filename,
                transport,
                &StreamChanges::default(),
                true,
            );
            match outcome {
                Err(OperationError::Refused(reason)) => {
                    assert!(reason.contains(message), "{reason}")
                }
                other => panic!("{name}: {other:?}"),
            }
        }
        assert_eq!(harness.registry.status().len(), 1);
    }

    #[test]
    fn create_stopped_prepares_a_preview_without_a_process() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create_with("boats-udp", &source, "udp", false, &transcode());
        let status = harness.status("boats-udp");
        assert_eq!(
            (status.state, status.running),
            (StreamState::Stopped, false)
        );
        assert!(status.command.unwrap().contains("libx264"));
        assert_eq!(harness.ffmpeg.run_count(), 0);
        let saved = harness.saved_stream("boats-udp");
        assert!(!saved.enabled);
        assert_eq!(saved.mode, Some(VideoMode::Transcode));
    }

    #[test]
    fn restoring_streams_does_not_write_state() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        let looped = StreamChanges {
            loop_count: Some(2),
            ..StreamChanges::default()
        };
        harness.create_with("running", &source, "udp", true, &looped);
        harness.create_with("stopped", &source, "rtsp", false, &transcode());
        let before = std::fs::read(&harness.config.state_path).unwrap();
        block_writes(&harness.state, true);
        let mut restored =
            build_registry(&harness.config, None, &harness.ffmpeg, &harness.resolver);
        block_writes(&harness.state, false);
        assert_eq!(std::fs::read(&harness.config.state_path).unwrap(), before);
        restored.supervise(None);
        assert!(restored.stream_status("running").unwrap().running);
        assert_eq!(restored.stream_status("running").unwrap().loop_count, 2);
        assert_eq!(
            restored.stream_status("stopped").unwrap().state,
            StreamState::Stopped
        );
    }

    #[test]
    fn failed_creation_save_does_not_register_or_start_a_stream() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        block_writes(&harness.state, true);
        let outcome = harness.registry.create_stream(
            "boats",
            &source,
            "udp",
            &StreamChanges::default(),
            true,
        );
        block_writes(&harness.state, false);
        assert!(matches!(outcome, Err(OperationError::Io(_))));
        assert!(!harness.registry.has_stream("boats"));
        assert_eq!(harness.ffmpeg.run_count(), 0);
        assert!(harness.saved_streams().is_empty());
    }

    #[test]
    fn delete_stops_the_process_frees_the_port_and_removes_the_record() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats-udp", &source, "udp");
        let run = harness.ffmpeg.run(0);
        assert!(harness.registry.delete_stream("boats-udp").unwrap());
        assert_eq!(code_of(&run), Some(-15));
        assert!(!harness.registry.has_stream("boats-udp"));
        assert!(harness.saved_streams().is_empty());
        harness.create("again", &source, "udp");
        assert_eq!(harness.status("again").port, Some(40000));
    }

    #[test]
    fn delete_refuses_while_the_process_cannot_stop() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats-udp", &source, "udp");
        harness.ffmpeg.run(0).lock().unwrap().stuck = true;
        assert!(!harness.registry.delete_stream("boats-udp").unwrap());
        assert!(harness.registry.has_stream("boats-udp"));
        harness.ffmpeg.run(0).lock().unwrap().stuck = false;
    }

    fn rebind_setup() -> Harness {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "udp");
        harness
    }

    #[test]
    fn changing_transport_takes_a_port_of_the_new_kind_and_restarts_once() {
        let mut harness = rebind_setup();
        let changes = StreamChanges {
            transport: Some("srt_listen".into()),
            loop_count: Some(2),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("boats", &changes).unwrap());
        let status = harness.status("boats");
        assert_eq!(
            (status.transport.as_str(), status.port, status.loop_count),
            ("srt_listen", Some(41000), 2)
        );
        assert_eq!(code_of(&harness.ffmpeg.run(0)), Some(-15));
        assert_eq!(harness.ffmpeg.run_count(), 2);
        assert!(
            argv_of(&harness.ffmpeg.run(1))
                .contains(&"srt://0.0.0.0:41000?mode=listener".to_string())
        );
        assert_eq!(harness.saved_stream("boats").transport, "srt_listen");
    }

    #[test]
    fn multicast_takes_a_random_high_port_outside_every_first_port_range() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        let changes = StreamChanges {
            transport: Some("multicast".into()),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("boats", &changes).unwrap());
        let status = harness.status("boats");
        let port = status.port.unwrap();
        assert!((50000..=59999).contains(&port), "{port}");
        assert_eq!(
            status.delivery.unwrap(),
            format!("udp://239.1.1.2:{port}?pkt_size=1316&ttl=1")
        );
        harness.create("boats-mcast-2", &source, "multicast");
        let second = harness.status("boats-mcast-2").port.unwrap();
        assert!((50000..=59999).contains(&second));
        assert_ne!(second, port);
        assert_eq!(harness.saved_stream("boats-mcast-2").port, Some(second));
    }

    #[test]
    fn renaming_keeps_the_port_the_record_and_the_place_in_the_list() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        harness.create("other", &source, "srt_listen");
        let changes = StreamChanges {
            name: Some("ships".into()),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("boats", &changes).unwrap());
        assert!(!harness.registry.has_stream("boats"));
        assert_eq!(harness.registry.stream_names(), ["ships", "other"]);
        assert_eq!(harness.status("ships").port, Some(40000));
        let saved: Vec<String> = harness
            .saved_streams()
            .into_iter()
            .map(|(name, _)| name)
            .collect();
        assert_eq!(saved, ["ships", "other"]);
        // A UDP command does not name the stream, so nothing restarts.
        assert_eq!(harness.ffmpeg.run_count(), 2);
        assert_eq!(code_of(&harness.ffmpeg.run(0)), None);
    }

    #[test]
    fn renaming_restarts_the_kinds_whose_command_names_the_stream() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        harness.create("r", &source, "rtsp");
        let changes = StreamChanges {
            name: Some("r2".into()),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("r", &changes).unwrap());
        assert_eq!(code_of(&harness.ffmpeg.run(1)), Some(-15));
        assert!(
            argv_of(&harness.ffmpeg.run(2))
                .contains(&"srt://127.0.0.1:8890?streamid=publish:r2".to_string())
        );
        assert_eq!(
            harness.status("r2").delivery.as_deref(),
            Some("srt://127.0.0.1:8890?streamid=publish:r2")
        );
    }

    #[test]
    fn renaming_with_other_changes_applies_both_in_one_restart() {
        let mut harness = rebind_setup();
        let changes = StreamChanges {
            name: Some("ships".into()),
            loop_count: Some(3),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("boats", &changes).unwrap());
        let status = harness.status("ships");
        assert_eq!((status.name.as_str(), status.loop_count), ("ships", 3));
        assert_eq!(harness.ffmpeg.run_count(), 2);
        assert_eq!(harness.saved_stream("ships").loop_count, 3);
    }

    #[test]
    fn renaming_to_a_taken_or_invalid_name_is_refused() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        harness.create("other", &source, "srt_listen");
        for name in ["other", "bad name"] {
            let changes = StreamChanges {
                name: Some(name.into()),
                ..StreamChanges::default()
            };
            assert!(
                matches!(
                    harness.registry.update_stream("boats", &changes),
                    Err(OperationError::Refused(_))
                ),
                "{name}"
            );
        }
        assert!(harness.registry.has_stream("boats"));
    }

    #[test]
    fn changing_the_file_restarts_on_the_new_source_and_keeps_the_port() {
        let mut harness = rebind_setup();
        let other = harness.another_file("other.mp4");
        let changes = StreamChanges {
            filename: Some("other.mp4".into()),
            ..StreamChanges::default()
        };
        assert!(harness.registry.update_stream("boats", &changes).unwrap());
        let status = harness.status("boats");
        assert_eq!(
            (status.filename.as_str(), status.port),
            ("other.mp4", Some(40000))
        );
        assert!(argv_of(&harness.ffmpeg.run(1)).contains(&other.to_string_lossy().into_owned()));
    }

    #[test]
    fn a_missing_source_waits_and_starts_when_the_file_appears() {
        let mut harness = rebind_setup();
        harness.create("ghost", "later.mp4", "udp");
        let status = harness.status("ghost");
        assert_eq!(
            (status.state, status.reason.as_deref()),
            (StreamState::Waiting, Some("source file missing"))
        );
        assert_eq!(harness.ffmpeg.run_count(), 1);
        harness.another_file("later.mp4");
        let source = harness.source_name();
        harness
            .registry
            .supervise(Some(&with_creates(&[&source, "later.mp4"], &["later.mp4"])));
        assert!(harness.status("ghost").running);
    }

    #[test]
    fn deleting_the_file_stops_the_stream_until_it_returns() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        std::fs::remove_file(&harness.source).unwrap();
        harness.registry.supervise(Some(&Inventory {
            deletes: vec![source.clone()],
            ..Inventory::default()
        }));
        let status = harness.status("boats");
        assert_eq!(
            (status.state, status.reason.as_deref()),
            (StreamState::Waiting, Some("source file missing"))
        );
        assert_eq!(code_of(&harness.ffmpeg.run(0)), Some(-15));
        std::fs::write(&harness.source, b"").unwrap();
        harness
            .registry
            .supervise(Some(&with_creates(&[&source], &[&source])));
        assert!(harness.status("boats").running);
        assert_eq!(harness.ffmpeg.run_count(), 2);
    }

    #[test]
    fn a_modified_file_restarts_only_the_streams_playing_it() {
        let mut harness = rebind_setup();
        let source = harness.source_name();
        harness.another_file("other.mp4");
        harness.create("other", "other.mp4", "udp");
        let other_run = harness.ffmpeg.run(1);
        harness.registry.supervise(Some(&Inventory {
            modifies: vec![source.clone()],
            ..inventory(&[&source, "other.mp4"])
        }));
        assert_eq!(harness.ffmpeg.run_count(), 3);
        assert_eq!(code_of(&harness.ffmpeg.run(0)), Some(-15));
        assert_eq!(code_of(&other_run), None);
    }

    #[test]
    fn videos_present_on_a_first_start_become_running_mediamtx_streams() {
        let files = ["cargoship.mp4", "rolling boats.v2.avi"];
        let mut harness = Harness::fresh(&files);
        harness
            .registry
            .supervise(Some(&with_creates(&files, &files)));
        let rows = harness.registry.status();
        let names: Vec<&str> = rows.iter().map(|row| row.name.as_str()).collect();
        assert_eq!(names, ["cargoship", "rolling-boats-v2"]);
        assert_eq!(rows[0].filename, "cargoship.mp4");
        assert_eq!(rows[1].filename, "rolling boats.v2.avi");
        assert!(
            rows.iter()
                .all(|row| row.transport == "rtsp" && row.running)
        );
        assert_eq!(
            rows[0].delivery.as_deref(),
            Some("srt://127.0.0.1:8890?streamid=publish:cargoship")
        );
        let saved: Vec<String> = harness
            .saved_streams()
            .into_iter()
            .map(|(name, _)| name)
            .collect();
        assert_eq!(saved, ["cargoship", "rolling-boats-v2"]);
    }

    #[test]
    fn seeding_waits_for_the_videos_to_settle_and_ends_once_streams_exist() {
        let mut harness = Harness::fresh(&["cargoship.mp4"]);
        harness.registry.supervise(Some(&Inventory {
            present: vec!["cargoship.mp4".into()],
            ..Inventory::default()
        }));
        assert!(harness.registry.status().is_empty());
        harness
            .registry
            .supervise(Some(&with_creates(&["cargoship.mp4"], &["cargoship.mp4"])));
        assert_eq!(harness.registry.stream_names(), ["cargoship"]);
        harness.another_file("later.mp4");
        let files = ["cargoship.mp4", "later.mp4"];
        harness
            .registry
            .supervise(Some(&with_creates(&files, &files)));
        assert_eq!(harness.registry.stream_names(), ["cargoship"]);
    }

    #[test]
    fn a_stream_made_by_hand_first_ends_seeding() {
        let mut harness = Harness::fresh(&["cargoship.mp4"]);
        harness.create("mine", "cargoship.mp4", "srt_listen");
        harness
            .registry
            .supervise(Some(&with_creates(&["cargoship.mp4"], &["cargoship.mp4"])));
        assert_eq!(harness.registry.stream_names(), ["mine"]);
    }

    #[test]
    fn playing_a_file_starts_its_first_mediamtx_stream_or_makes_one() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("source-udp", &source, "udp");
        assert!(harness.registry.play_file(&source, Some(0)).unwrap());
        assert_eq!(harness.registry.stream_names(), ["source-udp", "source"]);
        assert_eq!(harness.status("source").transport, "rtsp");
        assert!(harness.status("source").enabled);
        assert_eq!(harness.status("source").loop_count, 0);

        assert!(harness.registry.stop_file(&source).unwrap());
        assert!(!harness.status("source").enabled);
        assert!(harness.status("source-udp").enabled);
        assert!(harness.registry.play_file(&source, Some(-1)).unwrap());
        assert!(harness.status("source").enabled);
        assert_eq!(harness.status("source").loop_count, -1);
        assert_eq!(harness.saved_stream("source").loop_count, -1);
        assert_eq!(harness.registry.stream_names().len(), 2);

        assert!(harness.registry.play_file(&source, None).unwrap());
        assert_eq!(harness.status("source").loop_count, -1);
    }

    #[test]
    fn a_file_played_once_can_play_again_after_it_ends() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        let play_row = |registry: &mut StreamRegistry| {
            let snapshot = StatusSnapshot {
                statuses: registry.status(),
                settings: registry.settings().clone(),
                videos: vec![source.clone()],
            };
            let url_facts = UrlFacts {
                rtsp_port: 8554,
                hls_port: 8888,
                rtmp_port: 1935,
                api_port: 8080,
            };
            describe_play_rows(&snapshot, "publisher", &url_facts).remove(0)
        };
        assert!(harness.registry.play_file(&source, Some(0)).unwrap());
        harness.ffmpeg.last_run().lock().unwrap().finish(0);
        harness.registry.supervise(None);
        assert_eq!(harness.status("source").state, StreamState::Completed);
        assert_eq!(play_row(&mut harness.registry).selected, Some(PlayChoice::Stop));

        let runs = harness.ffmpeg.run_count();
        assert!(harness.registry.play_file(&source, Some(0)).unwrap());
        assert_eq!(harness.ffmpeg.run_count(), runs + 1);
        assert_eq!(play_row(&mut harness.registry).selected, Some(PlayChoice::Once));
    }

    #[test]
    fn only_offered_videos_play_and_a_file_without_a_stream_has_nothing_to_stop() {
        let mut harness = Harness::new();
        assert!(matches!(
            harness.registry.play_file("missing.mp4", None),
            Err(OperationError::Refused(_))
        ));
        assert!(harness.registry.stop_file("missing.mp4").unwrap());
        assert!(harness.registry.stream_names().is_empty());
    }

    #[test]
    fn a_saved_state_without_streams_is_not_a_first_start() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness
            .registry
            .supervise(Some(&with_creates(&[&source], &[&source])));
        assert!(harness.registry.status().is_empty());
    }

    #[test]
    fn seeded_names_are_valid_and_distinct() {
        let none = |_: &str| false;
        assert_eq!(
            seeded_stream_name("truck_klv_30s.ts", &none),
            "truck_klv_30s"
        );
        assert_eq!(
            seeded_stream_name("--my clip (final).mp4", &none),
            "my-clip-final"
        );
        assert_eq!(
            seeded_stream_name("a.ts", &|name| name == "a" || name == "a-2"),
            "a-3"
        );
        assert_eq!(seeded_stream_name("-.mp4", &none), "video");
        let long_name = seeded_stream_name(&format!("{}.mp4", "x".repeat(80)), &|name| {
            name == "x".repeat(64)
        });
        assert_eq!((long_name.len(), &long_name[62..]), (64, "-2"));
        for case in fixture("streams.json")["seeded_names"].as_array().unwrap() {
            let taken: Vec<String> = case["taken"]
                .as_array()
                .unwrap()
                .iter()
                .map(|name| name.as_str().unwrap().to_string())
                .collect();
            let name = seeded_stream_name(case["filename"].as_str().unwrap(), &|candidate| {
                taken.iter().any(|taken| taken == candidate)
            });
            assert_eq!(Value::String(name), case["name"], "{case}");
        }
    }

    #[test]
    fn transports_needing_settings_wait_with_the_reason() {
        let mut harness = Harness::with_settings(&RuntimeSettings::default());
        let source = harness.source_name();
        for transport in ["udp", "multicast", "srtp", "srt_listen"] {
            harness.create(transport, &source, transport);
        }
        assert_eq!(
            harness.status("udp").reason.as_deref(),
            Some("no destination host configured")
        );
        assert_eq!(
            harness.status("srtp").reason.as_deref(),
            Some("no destination host configured")
        );
        assert_eq!(harness.status("multicast").reason, None);
        assert_eq!(harness.status("srt_listen").reason, None);
        assert_eq!(harness.ffmpeg.run_count(), 2);
    }

    #[test]
    fn apply_settings_restarts_only_streams_whose_delivery_changed() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("u", &source, "udp");
        harness.create("s", &source, "srt_listen");
        let srt_run = harness.ffmpeg.run(1);
        let settings = RuntimeSettings {
            destination_host: Some("other-receiver".into()),
            ..test_settings()
        };
        assert!(harness.registry.apply_settings(settings.clone()).unwrap());
        assert_eq!(harness.ffmpeg.run_count(), 3);
        assert_eq!(code_of(&harness.ffmpeg.run(0)), Some(-15));
        assert_eq!(code_of(&srt_run), None);
        assert!(
            argv_of(&harness.ffmpeg.run(2))
                .contains(&"udp://other-receiver:40000?pkt_size=1316".to_string())
        );
        let saved = StreamStateStore::new(&harness.config.state_path)
            .load_settings()
            .unwrap();
        assert_eq!(saved, settings);
        harness.registry.supervise(None);
        assert_eq!(
            harness.resolver.refreshed(),
            BTreeSet::from(["other-receiver".to_string()])
        );
    }

    #[test]
    fn first_port_changes_affect_new_streams_only() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("u", &source, "udp");
        let settings = RuntimeSettings {
            udp_first_port: 50000,
            ..test_settings()
        };
        assert!(harness.registry.apply_settings(settings).unwrap());
        assert_eq!(harness.status("u").port, Some(40000));
        assert_eq!(harness.ffmpeg.run_count(), 1);
        harness.create("v", &source, "udp");
        assert_eq!(harness.status("v").port, Some(50000));
    }

    #[test]
    fn a_setting_a_waiting_stream_needs_starts_it_on_save() {
        let mut harness = Harness::with_settings(&RuntimeSettings::default());
        let source = harness.source_name();
        harness.create("u", &source, "udp");
        assert_eq!(harness.status("u").state, StreamState::Waiting);
        let settings = RuntimeSettings {
            destination_host: Some("receiver".into()),
            ..RuntimeSettings::default()
        };
        assert!(harness.registry.apply_settings(settings).unwrap());
        assert!(harness.status("u").running);
    }

    #[test]
    fn cap_from_settings_applies_to_transcoding_streams_only() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create_with("t", &source, "rtsp", true, &transcode());
        harness.create("c", &source, "udp");
        let copied_run = harness.ffmpeg.run(1);
        assert!(
            harness
                .status("t")
                .command
                .unwrap()
                .contains("-maxrate 2000000")
        );
        let settings = RuntimeSettings {
            max_video_bitrate: "3M".into(),
            ..test_settings()
        };
        assert!(harness.registry.apply_settings(settings).unwrap());
        assert!(
            harness
                .status("t")
                .command
                .unwrap()
                .contains("-maxrate 3000000")
        );
        assert_eq!(harness.ffmpeg.run_count(), 3);
        assert_eq!(code_of(&copied_run), None);
    }

    #[test]
    fn restart_restores_streams_with_ports_and_choices_from_state() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        let looped = StreamChanges {
            loop_count: Some(2),
            ..StreamChanges::default()
        };
        harness.create_with("u", &source, "udp", true, &looped);
        {
            let (stream, services) = harness.registry.stream_mut("u").unwrap();
            stream.stop_streaming(services).unwrap();
        }
        harness.create("s", &source, "srt_listen");
        let mut restarted =
            build_registry(&harness.config, None, &harness.ffmpeg, &harness.resolver);
        restarted.supervise(Some(&inventory(&[&source])));
        let u = restarted.stream_status("u").unwrap();
        assert_eq!((u.port, u.loop_count, u.enabled), (Some(40000), 2, false));
        let s = restarted.stream_status("s").unwrap();
        assert_eq!((s.port, s.running), (Some(41000), true));
        restarted
            .create_stream("v", &source, "udp", &StreamChanges::default(), true)
            .unwrap();
        assert_eq!(restarted.stream_status("v").unwrap().port, Some(40001));
    }

    /// The scenario `tests/fixtures/generate.py` recorded, replayed here.
    #[test]
    fn registry_behavior_matches_the_python_fixture() {
        let files = ["boats.mp4", "cargo ship.v2.avi", "truck_klv_30s.ts"];
        let harness = Harness::fresh(&files);
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/streams.json");
        let text = std::fs::read_to_string(path).unwrap();
        let quoted = |path: &Path| {
            let json = serde_json::to_string(&path.to_string_lossy()).unwrap();
            json[1..json.len() - 1].to_string()
        };
        let text = text
            .replace("{videos_dir}", &quoted(harness.videos.path()))
            .replace("{hls_dir}", &quoted(harness.hls.path()));
        let fixture: Value = serde_json::from_str(&text).unwrap();
        let url_facts = UrlFacts {
            rtsp_port: 8554,
            hls_port: 8888,
            rtmp_port: 1935,
            api_port: 8080,
        };

        let mut registry = build_registry(
            &harness.config,
            Some(&test_settings()),
            &harness.ffmpeg,
            &harness.resolver,
        );
        registry.set_random_port_picker(Box::new(|candidates| candidates.first().copied()));
        registry.supervise(Some(&with_creates(&files, &files)));
        let tuned = StreamChanges {
            mode: Some(VideoMode::Transcode),
            frame_rate: Adjustment::Set(15.0),
            loop_count: Some(2),
            gop_frames: Adjustment::Set(20),
            ..StreamChanges::default()
        };
        for kind in &crate::transports::KINDS {
            registry
                .create_stream(
                    kind.id,
                    "boats.mp4",
                    kind.id,
                    &StreamChanges::default(),
                    true,
                )
                .unwrap();
            registry
                .create_stream(
                    &format!("{}-t", kind.id),
                    "truck_klv_30s.ts",
                    kind.id,
                    &tuned,
                    true,
                )
                .unwrap();
        }
        registry
            .create_stream(
                "stopped-udp",
                "cargo ship.v2.avi",
                "udp",
                &transcode(),
                false,
            )
            .unwrap();
        registry
            .create_stream("ghost", "later.mp4", "udp", &StreamChanges::default(), true)
            .unwrap();
        let once = StreamChanges {
            loop_count: Some(0),
            ..StreamChanges::default()
        };
        registry
            .create_stream("once", "boats.mp4", "rtsp", &once, true)
            .unwrap();
        let renamed = StreamChanges {
            name: Some("udp-renamed".into()),
            loop_count: Some(3),
            ..StreamChanges::default()
        };
        assert!(registry.update_stream("udp-t", &renamed).unwrap());
        {
            let handle = harness.ffmpeg.run(0);
            let mut crashed = handle.lock().unwrap();
            crashed.tail = "failure detail".into();
            crashed.finish(1);
        }
        harness.ffmpeg.last_run().lock().unwrap().finish(0);
        registry.supervise(None);
        let statuses = registry.status();
        assert_eq!(
            serde_json::to_value(&statuses).unwrap(),
            fixture["statuses"]
        );
        let described = describe_streams(&statuses, registry.settings(), "publisher", &url_facts);
        assert_eq!(
            serde_json::to_value(&described).unwrap(),
            fixture["described"]
        );
        assert_eq!(
            serde_json::to_value(registry.videos()).unwrap(),
            fixture["videos"]
        );
        let document = StreamStateStore::new(&harness.config.state_path)
            .load()
            .unwrap();
        assert_eq!(Value::Object(document), fixture["state_document"]);
        assert_eq!(
            std::fs::read_to_string(&harness.config.state_path).unwrap(),
            fixture["state_text"].as_str().unwrap()
        );

        let waiting_config = Config {
            state_path: harness.state.path().join("waiting.json"),
            ..harness.config.clone()
        };
        let mut waiting = build_registry(
            &waiting_config,
            Some(&RuntimeSettings::default()),
            &harness.ffmpeg,
            &harness.resolver,
        );
        waiting.set_random_port_picker(Box::new(|candidates| candidates.first().copied()));
        for transport in ["udp", "multicast", "srtp", "srt_listen", "rtp", "srt_call"] {
            waiting
                .create_stream(
                    transport,
                    "boats.mp4",
                    transport,
                    &StreamChanges::default(),
                    true,
                )
                .unwrap();
        }
        let waiting_statuses = waiting.status();
        assert_eq!(
            serde_json::to_value(&waiting_statuses).unwrap(),
            fixture["waiting_statuses"]
        );
        let waiting_described = describe_streams(
            &waiting_statuses,
            waiting.settings(),
            "localhost",
            &url_facts,
        );
        assert_eq!(
            serde_json::to_value(&waiting_described).unwrap(),
            fixture["waiting_described"]
        );

        let port_config = Config {
            state_path: harness.state.path().join("ports.json"),
            ..harness.config.clone()
        };
        let mut ports = build_registry(
            &port_config,
            Some(&test_settings()),
            &harness.ffmpeg,
            &harness.resolver,
        );
        for index in 0..3 {
            for transport in ["udp", "srt_listen", "tcp_listen"] {
                ports
                    .create_stream(
                        &format!("{transport}-{index}"),
                        "boats.mp4",
                        transport,
                        &StreamChanges::default(),
                        true,
                    )
                    .unwrap();
            }
        }
        assert!(ports.delete_stream("udp-1").unwrap());
        ports
            .create_stream(
                "udp-again",
                "boats.mp4",
                "udp",
                &StreamChanges::default(),
                true,
            )
            .unwrap();
        let rebound = StreamChanges {
            transport: Some("udp".into()),
            ..StreamChanges::default()
        };
        assert!(ports.update_stream("srt_listen-2", &rebound).unwrap());
        let rows: Vec<Value> = ports
            .status()
            .iter()
            .map(|row| serde_json::json!([row.name, row.transport, row.port]))
            .collect();
        assert_eq!(Value::Array(rows), fixture["port_allocation"]);
        // Commands keyed by their delivery URL, recorded across all three registries
        // because the generator shared one fake process list.
        let mut commands = serde_json::Map::new();
        for run in harness.ffmpeg.runs() {
            let argv = argv_of(&run);
            if let Some(url) = argv.last() {
                commands.insert(url.clone(), serde_json::to_value(&argv).unwrap());
            }
        }
        assert_eq!(Value::Object(commands), fixture["commands"]);
    }
}
