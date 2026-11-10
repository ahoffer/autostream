//! Shared fixtures and helpers for the behavioral tests.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;

use crate::ffmpegcmd::{Delivery, MediaProbeResult};
use crate::transports::Muxer;

/// A parity fixture recorded from the Python implementation.
pub fn fixture(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    let text =
        fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

static STATE_FILES: AtomicUsize = AtomicUsize::new(0);

/// A unique state file name inside a test's directory; the file does not exist yet.
pub fn state_path(directory: &tempfile::TempDir) -> PathBuf {
    let ordinal = STATE_FILES.fetch_add(1, Ordering::Relaxed);
    directory.path().join(format!("state-{ordinal}.json"))
}

/// Make a directory read-only so every write under it fails, or writable again.
pub fn block_writes(directory: &tempfile::TempDir, blocked: bool) {
    use std::os::unix::fs::PermissionsExt;
    let mode = if blocked { 0o500 } else { 0o700 };
    fs::set_permissions(directory.path(), fs::Permissions::from_mode(mode)).unwrap();
}

pub fn profile_from_fixture(document: &Value) -> MediaProbeResult {
    let names = |value: &Value| {
        value.as_array().map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
    };
    MediaProbeResult {
        video_codecs: names(&document["video_codecs"]),
        audio_codecs: names(&document["audio_codecs"]),
        audio_copyable: document["audio_copyable"].as_bool().unwrap_or(false),
        pix_fmt: document["pix_fmt"].as_str().map(str::to_string),
        bitrate_bps: document["bitrate_bps"].as_i64(),
        frame_rate: document["frame_rate"].as_f64(),
        data_codecs: names(&document["data_codecs"]),
    }
}

pub fn delivery_from_fixture(document: &Value) -> Delivery {
    let muxer = match document["muxer"].as_str().unwrap_or("mpegts") {
        "rtsp" => Muxer::Rtsp,
        "rtp_mpegts" => Muxer::RtpMpegts,
        "hls" => Muxer::Hls,
        _ => Muxer::Mpegts,
    };
    Delivery {
        url: document["url"].as_str().map(str::to_string),
        muxer,
        carries_klv: document["carries_klv"].as_bool().unwrap_or(true),
        extra_args: document["extra_args"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default(),
        resolve_host: document["resolve_host"].as_str().map(str::to_string),
        client_bound: document["client_bound"].as_bool().unwrap_or(false),
        blocker: document["blocker"].as_str().map(str::to_string),
        clamp_data_dts: document["clamp_data_dts"].as_bool().unwrap_or(true),
    }
}

use std::collections::BTreeSet;
use std::io;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::appconfig::Config;
use crate::ffmpegprocess::{ProcessObservation, StreamProcess};
use crate::filewatch::Inventory;
use crate::mediastream::{FfmpegBoundary, StreamChanges, StreamServices};
use crate::runtimesettings::RuntimeSettings;
use crate::statefile::StreamStateStore;
use crate::streamlog::StreamLogger;
use crate::streamregistry::StreamRegistry;
use crate::streamresponses::StreamStatus;
use crate::udphost::HostResolver;

pub fn test_settings() -> RuntimeSettings {
    RuntimeSettings {
        destination_host: Some("receiver".into()),
        multicast_group: "239.1.1.2".into(),
        srtp_key: "c2VjcmV0a2V5".into(),
        max_video_bitrate: "2M".into(),
        ..RuntimeSettings::default()
    }
}

pub fn test_profile() -> MediaProbeResult {
    MediaProbeResult {
        video_codecs: Some(vec!["h264".into()]),
        audio_codecs: Some(vec!["aac".into()]),
        audio_copyable: true,
        pix_fmt: Some("yuv420p".into()),
        bitrate_bps: Some(4_000_000),
        frame_rate: Some(30.0),
        data_codecs: Some(vec!["klv".into()]),
    }
}

/// One fake child: what the stream told it, and what the test makes it report.
#[derive(Debug, Default)]
pub struct FakeRun {
    pub argv: Vec<String>,
    pub code: Option<i32>,
    pub age: Duration,
    pub stuck: bool,
    pub tail: String,
}

impl FakeRun {
    pub fn finish(&mut self, code: i32) {
        self.code = Some(code);
    }
}

pub type RunHandle = Arc<Mutex<FakeRun>>;

struct FakeProcess(RunHandle);

impl StreamProcess for FakeProcess {
    fn start(&mut self, argv: &[String]) -> io::Result<()> {
        self.0.lock().unwrap().argv = argv.to_vec();
        Ok(())
    }

    fn observe(&mut self) -> ProcessObservation {
        let run = self.0.lock().unwrap();
        ProcessObservation {
            returncode: run.code,
            argv: run.argv.clone(),
            running_for: run.age,
            diagnostics: run.tail.clone(),
        }
    }

    fn stop(&mut self) -> bool {
        let mut run = self.0.lock().unwrap();
        if run.stuck {
            return false;
        }
        run.code = Some(-15);
        true
    }
}

/// Fake processes and a fixed probe profile, shared with the test through handles.
#[derive(Clone)]
pub struct FakeFfmpeg {
    pub runs: Arc<Mutex<Vec<RunHandle>>>,
    pub profile: Arc<Mutex<MediaProbeResult>>,
    pub probes: Arc<AtomicUsize>,
    pub probing_forbidden: Arc<AtomicBool>,
}

impl FakeFfmpeg {
    pub fn new(profile: MediaProbeResult) -> Self {
        Self {
            runs: Arc::new(Mutex::new(Vec::new())),
            profile: Arc::new(Mutex::new(profile)),
            probes: Arc::new(AtomicUsize::new(0)),
            probing_forbidden: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn runs(&self) -> Vec<RunHandle> {
        self.runs.lock().unwrap().clone()
    }

    pub fn run(&self, index: usize) -> RunHandle {
        self.runs()[index].clone()
    }

    pub fn last_run(&self) -> RunHandle {
        self.runs().last().expect("no run yet").clone()
    }

    pub fn run_count(&self) -> usize {
        self.runs.lock().unwrap().len()
    }

    pub fn set_profile(&self, profile: MediaProbeResult) {
        *self.profile.lock().unwrap() = profile;
    }
}

impl FfmpegBoundary for FakeFfmpeg {
    fn probe(&self, _path: &Path) -> MediaProbeResult {
        assert!(
            !self.probing_forbidden.load(Ordering::Relaxed),
            "status probed"
        );
        self.probes.fetch_add(1, Ordering::Relaxed);
        self.profile.lock().unwrap().clone()
    }

    fn spawn(&self, _logger: Arc<StreamLogger>) -> Box<dyn StreamProcess> {
        let handle = Arc::new(Mutex::new(FakeRun::default()));
        self.runs.lock().unwrap().push(handle.clone());
        Box::new(FakeProcess(handle))
    }
}

#[derive(Clone, Default)]
pub struct FakeResolver {
    pub blocked: Arc<AtomicBool>,
    pub refreshed: Arc<Mutex<BTreeSet<String>>>,
    pub refreshing_forbidden: Arc<AtomicBool>,
}

impl FakeResolver {
    pub fn set_blocked(&self, blocked: bool) {
        self.blocked.store(blocked, Ordering::Relaxed);
    }

    pub fn refreshed(&self) -> BTreeSet<String> {
        self.refreshed.lock().unwrap().clone()
    }
}

impl HostResolver for FakeResolver {
    fn refresh(&mut self, hosts: &BTreeSet<String>) {
        assert!(
            !self.refreshing_forbidden.load(Ordering::Relaxed),
            "status resolved"
        );
        *self.refreshed.lock().unwrap() = hosts.clone();
    }

    fn start_blocker(&self, _host: &str) -> Option<String> {
        self.blocked
            .load(Ordering::Relaxed)
            .then(|| "DNS unresolved".to_string())
    }
}

pub fn make_config(videos_dir: &Path, state_path: &Path, hls_dir: &Path) -> Config {
    let mut config = Config::new(8554, 8888, 1935, 8080, "debug").unwrap();
    config.videos_dir = videos_dir.to_path_buf();
    config.state_path = state_path.to_path_buf();
    config.hls_dir = hls_dir.to_path_buf();
    config
}

/// An inventory from file names: `stable` files are present and settled.
pub fn inventory(stable: &[&str]) -> Inventory {
    let names: Vec<String> = stable.iter().map(|name| name.to_string()).collect();
    Inventory {
        present: names.clone(),
        stable: names,
        ..Inventory::default()
    }
}

pub fn build_registry(
    config: &Config,
    settings: Option<&RuntimeSettings>,
    ffmpeg: &FakeFfmpeg,
    resolver: &FakeResolver,
) -> StreamRegistry {
    let mut store = StreamStateStore::new(&config.state_path);
    if let Some(settings) = settings {
        store.save_settings(&settings.to_document()).unwrap();
    }
    let services = StreamServices {
        store,
        resolver: Box::new(resolver.clone()),
        ffmpeg: Box::new(ffmpeg.clone()),
    };
    let mut registry = StreamRegistry::new(
        config.clone(),
        services,
        Box::new(|name| Ok(Arc::new(StreamLogger::discard(name)))),
    );
    registry.load_persisted_state().unwrap();
    registry
}

/// A registry over one stable video file, fake processes and DNS, and saved settings.
pub struct Harness {
    pub registry: StreamRegistry,
    pub ffmpeg: FakeFfmpeg,
    pub resolver: FakeResolver,
    pub source: PathBuf,
    pub config: Config,
    pub videos: tempfile::TempDir,
    pub state: tempfile::TempDir,
    pub hls: tempfile::TempDir,
}

impl Harness {
    pub fn with_settings(settings: &RuntimeSettings) -> Self {
        let mut harness = Self::fresh(&["source.mp4"]);
        harness.registry = build_registry(
            &harness.config,
            Some(settings),
            &harness.ffmpeg,
            &harness.resolver,
        );
        harness
            .registry
            .supervise(Some(&inventory(&["source.mp4"])));
        harness
    }

    pub fn new() -> Self {
        Self::with_settings(&test_settings())
    }
}

impl Harness {
    /// A first start: the named videos are present and no state file was ever written.
    pub fn fresh(filenames: &[&str]) -> Self {
        let videos = tempfile::tempdir().unwrap();
        let state = tempfile::tempdir().unwrap();
        let hls = tempfile::tempdir().unwrap();
        for filename in filenames {
            fs::write(videos.path().join(filename), b"").unwrap();
        }
        let config = make_config(
            videos.path(),
            &state.path().join("streams_config.json"),
            hls.path(),
        );
        let ffmpeg = FakeFfmpeg::new(test_profile());
        let resolver = FakeResolver::default();
        let registry = build_registry(&config, None, &ffmpeg, &resolver);
        Self {
            registry,
            ffmpeg,
            resolver,
            source: videos
                .path()
                .join(filenames.first().copied().unwrap_or("source.mp4")),
            config,
            videos,
            state,
            hls,
        }
    }

    pub fn create(&mut self, name: &str, filename: &str, transport: &str) {
        self.create_with(name, filename, transport, true, &StreamChanges::default());
    }

    pub fn create_with(
        &mut self,
        name: &str,
        filename: &str,
        transport: &str,
        start: bool,
        changes: &StreamChanges,
    ) {
        self.registry
            .create_stream(name, filename, transport, changes, start)
            .unwrap_or_else(|error| panic!("creating {name}: {error}"));
    }

    pub fn status(&mut self, name: &str) -> StreamStatus {
        self.registry
            .stream_status(name)
            .unwrap_or_else(|| panic!("no stream {name}"))
    }

    pub fn source_name(&self) -> String {
        self.source
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned()
    }

    pub fn saved_streams(&self) -> Vec<(String, crate::streamrecord::StreamRecord)> {
        StreamStateStore::new(&self.config.state_path)
            .load_streams()
            .unwrap()
    }

    pub fn saved_stream(&self, name: &str) -> crate::streamrecord::StreamRecord {
        self.saved_streams()
            .into_iter()
            .find(|(saved, _)| saved == name)
            .unwrap_or_else(|| panic!("{name} not saved"))
            .1
    }

    pub fn another_file(&self, name: &str) -> PathBuf {
        let path = self.videos.path().join(name);
        fs::write(&path, b"").unwrap();
        path
    }
}

impl Default for Harness {
    fn default() -> Self {
        Self::new()
    }
}
