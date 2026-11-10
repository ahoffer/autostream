//! Atomic, scoped persistence of streams and settings.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::runtimesettings::RuntimeSettings;
use crate::streamrecord::StreamRecord;
use crate::transports::kind_by_id;

/// The saved document: a `streams` object in creation order, optional `settings`,
/// and any other keys a future version may add, kept as they were read.
pub type StateDocument = Map<String, Value>;

/// Stream names double as MediaMTX paths and file names, so they stay plain:
/// a letter or digit, then up to 63 letters, digits, dashes or underscores.
pub fn is_valid_stream_name(name: &str) -> bool {
    let mut characters = name.chars();
    let first_is_plain = characters
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric());
    first_is_plain
        && name.len() <= 64
        && characters.all(|character| {
            character.is_ascii_alphanumeric() || character == '_' || character == '-'
        })
}

fn invalid(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// One writer: the supervisor loop. Failed writes leave the prior data intact.
pub struct StreamStateStore {
    path: PathBuf,
    document: Option<StateDocument>,
}

impl StreamStateStore {
    pub fn new(path: &Path) -> Self {
        Self {
            path: path.to_path_buf(),
            document: None,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// No state file yet: nothing was ever saved, so this is a first start.
    pub fn is_fresh(&self) -> bool {
        !self.path.exists()
    }

    pub fn load(&mut self) -> io::Result<StateDocument> {
        if self.document.is_none() {
            self.document = Some(self.read()?);
        }
        Ok(self.document.clone().unwrap_or_default())
    }

    /// The saved streams as typed records, in creation order.
    pub fn load_streams(&mut self) -> io::Result<Vec<(String, StreamRecord)>> {
        let document = self.load()?;
        let mut streams = Vec::new();
        if let Some(Value::Object(records)) = document.get("streams") {
            for (name, record) in records {
                let record: StreamRecord = serde_json::from_value(record.clone())
                    .map_err(|error| invalid(format!("Invalid saved stream '{name}': {error}")))?;
                streams.push((name.clone(), record));
            }
        }
        Ok(streams)
    }

    /// The saved settings, or defaults when the document has none.
    pub fn load_settings(&mut self) -> io::Result<RuntimeSettings> {
        let document = self.load()?;
        let settings = match document.get("settings") {
            Some(Value::Object(settings)) => settings.clone(),
            _ => Map::new(),
        };
        RuntimeSettings::from_document(&settings).map_err(invalid)
    }

    fn read(&self) -> io::Result<StateDocument> {
        let raw = match fs::read_to_string(&self.path) {
            Ok(raw) => raw,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let mut document = StateDocument::new();
                document.insert("streams".to_string(), Value::Object(Map::new()));
                return Ok(document);
            }
            Err(error) => return Err(error),
        };
        let data: Value = serde_json::from_str(&raw).map_err(|error| invalid(error.to_string()))?;
        let document = match data {
            Value::Object(document) => document,
            _ => return Err(self.invalid_document()),
        };
        self.validate(&document)?;
        Ok(document)
    }

    fn invalid_document(&self) -> io::Error {
        invalid(format!(
            "Invalid state document {}; delete the file to start fresh",
            self.path.display()
        ))
    }

    fn validate(&self, data: &StateDocument) -> io::Result<()> {
        let streams = match data.get("streams") {
            Some(Value::Object(streams)) => streams,
            _ => return Err(self.invalid_document()),
        };
        for (name, record) in streams {
            let record = match record {
                Value::Object(record) if is_valid_stream_name(name) => record,
                _ => return Err(invalid(format!("Invalid saved stream '{name}'"))),
            };
            validate_record(name, record)?;
        }
        if let Some(settings) = data.get("settings") {
            let settings = match settings {
                Value::Object(settings) => settings,
                _ => return Err(invalid("Invalid saved settings".to_string())),
            };
            RuntimeSettings::from_document(settings).map_err(invalid)?;
        }
        Ok(())
    }

    fn write(&self, document: &StateDocument) -> io::Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let temp = self.path.with_extension("tmp");
        let mut text = serde_json::to_string_pretty(document)?;
        text.push('\n');
        fs::write(&temp, text)?;
        fs::rename(&temp, &self.path)
    }

    fn commit(&mut self, document: StateDocument) -> io::Result<()> {
        self.write(&document)?;
        self.document = Some(document);
        Ok(())
    }

    fn streams_mut(document: &mut StateDocument) -> &mut Map<String, Value> {
        let streams = document
            .entry("streams")
            .or_insert_with(|| Value::Object(Map::new()));
        if !streams.is_object() {
            *streams = Value::Object(Map::new());
        }
        streams
            .as_object_mut()
            .expect("streams was just made an object")
    }

    pub fn save_stream(&mut self, name: &str, record: &StreamRecord) -> io::Result<()> {
        let mut document = self.load()?;
        Self::streams_mut(&mut document).insert(name.to_string(), serde_json::to_value(record)?);
        self.commit(document)
    }

    pub fn delete_stream(&mut self, name: &str) -> io::Result<()> {
        let mut document = self.load()?;
        Self::streams_mut(&mut document).shift_remove(name);
        self.commit(document)
    }

    /// Move a record to a new name, keeping its place in the order the streams were created.
    pub fn rename_stream(&mut self, previous: &str, name: &str) -> io::Result<()> {
        let mut document = self.load()?;
        let streams = Self::streams_mut(&mut document);
        let renamed: Map<String, Value> = streams
            .iter()
            .map(|(key, record)| {
                let key = if key == previous {
                    name.to_string()
                } else {
                    key.clone()
                };
                (key, record.clone())
            })
            .collect();
        *streams = renamed;
        self.commit(document)
    }

    pub fn save_settings(&mut self, settings_document: &Map<String, Value>) -> io::Result<()> {
        let mut document = self.load()?;
        document.insert(
            "settings".to_string(),
            Value::Object(settings_document.clone()),
        );
        self.commit(document)
    }
}

fn validate_record(name: &str, record: &Map<String, Value>) -> io::Result<()> {
    let complaint = |what: &str| invalid(format!("Stream {name} has {what}"));
    match record.get("filename") {
        Some(Value::String(filename)) if !filename.is_empty() => {}
        _ => return Err(invalid(format!("Stream {name} must name a file"))),
    }
    match record.get("transport") {
        Some(Value::String(transport)) if kind_by_id(transport).is_some() => {}
        _ => return Err(complaint("an unknown transport")),
    }
    match record.get("port") {
        None | Some(Value::Null) => {}
        Some(port) if whole_number(port).is_some_and(|port| (1..=65535).contains(&port)) => {}
        _ => return Err(complaint("an invalid port")),
    }
    if !matches!(record.get("enabled"), Some(Value::Bool(_))) {
        return Err(complaint("no enabled flag"));
    }
    match record.get("mode") {
        None | Some(Value::Null) => {}
        Some(Value::String(mode)) if mode == "passthrough" || mode == "transcode" => {}
        _ => return Err(complaint("an invalid mode")),
    }
    match record.get("loop_count").and_then(whole_number) {
        Some(count) if count >= -1 => {}
        _ => return Err(complaint("an invalid loop count")),
    }
    match record.get("frame_rate") {
        None | Some(Value::Null) => {}
        Some(rate)
            if rate
                .as_f64()
                .is_some_and(|rate| (1.0..=120.0).contains(&rate)) => {}
        _ => return Err(complaint("an invalid frame_rate")),
    }
    // The Python version also accepted a fractional GOP from a hand-edited file;
    // the API only ever saves whole frames, so only those load here.
    match record.get("gop_frames") {
        None | Some(Value::Null) => {}
        Some(gop) if whole_number(gop).is_some_and(|gop| gop >= 1) => {}
        _ => return Err(complaint("an invalid gop_frames")),
    }
    Ok(())
}

/// A JSON integer; floats and booleans are not whole numbers here, as in Python's `type(x) is int`.
fn whole_number(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::streamrecord::VideoMode;
    use crate::testsupport::{fixture, state_path};

    fn record() -> StreamRecord {
        StreamRecord {
            filename: "boats.mp4".into(),
            transport: "udp".into(),
            port: Some(40000),
            enabled: true,
            mode: None,
            loop_count: -1,
            frame_rate: None,
            gop_frames: None,
        }
    }

    #[test]
    fn sequential_scoped_saves_preserve_other_streams_and_settings() {
        let directory = tempfile::tempdir().unwrap();
        let path = state_path(&directory);
        let mut store = StreamStateStore::new(&path);
        assert!(store.is_fresh());
        store.save_stream("boats", &record()).unwrap();
        let settings = RuntimeSettings {
            destination_host: Some("receiver".into()),
            ..Default::default()
        };
        store.save_settings(&settings.to_document()).unwrap();
        store
            .save_stream(
                "boats-srt",
                &StreamRecord {
                    transport: "srt_listen".into(),
                    port: Some(41000),
                    ..record()
                },
            )
            .unwrap();
        store
            .save_stream(
                "boats",
                &StreamRecord {
                    enabled: false,
                    ..record()
                },
            )
            .unwrap();
        store.delete_stream("boats-srt").unwrap();
        assert!(!store.is_fresh());
        let mut reloaded = StreamStateStore::new(&path);
        let streams = reloaded.load_streams().unwrap();
        assert_eq!(streams.len(), 1);
        assert_eq!(streams[0].0, "boats");
        assert!(!streams[0].1.enabled);
        assert_eq!(
            reloaded
                .load_settings()
                .unwrap()
                .destination_host
                .as_deref(),
            Some("receiver")
        );
    }

    #[test]
    fn rename_moves_the_record_and_keeps_its_place() {
        let directory = tempfile::tempdir().unwrap();
        let path = state_path(&directory);
        let mut store = StreamStateStore::new(&path);
        store.save_stream("boats", &record()).unwrap();
        store
            .save_stream(
                "boats-srt",
                &StreamRecord {
                    transport: "srt_listen".into(),
                    port: Some(41000),
                    ..record()
                },
            )
            .unwrap();
        store.rename_stream("boats", "ships").unwrap();
        let streams = StreamStateStore::new(&path).load_streams().unwrap();
        let names: Vec<_> = streams.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(names, ["ships", "boats-srt"]);
        assert_eq!(streams[0].1.port, Some(40000));
    }

    #[test]
    fn failed_replace_leaves_previous_document_retryable() {
        let directory = tempfile::tempdir().unwrap();
        let mut path = state_path(&directory);
        let mut store = StreamStateStore::new(&path);
        store.save_stream("boats", &record()).unwrap();
        let before = fs::read_to_string(&path).unwrap();
        let cached = store.load().unwrap();
        // Block the replacement by making the directory unwritable, then retry.
        crate::testsupport::block_writes(&directory, true);
        let failed = store.save_stream(
            "boats",
            &StreamRecord {
                enabled: false,
                ..record()
            },
        );
        crate::testsupport::block_writes(&directory, false);
        assert!(failed.is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), before);
        assert_eq!(store.load().unwrap(), cached);
        store
            .save_stream(
                "boats",
                &StreamRecord {
                    enabled: false,
                    ..record()
                },
            )
            .unwrap();
        assert_eq!(
            store.load().unwrap(),
            StreamStateStore::new(&path).load().unwrap()
        );
        path.set_extension("tmp");
        assert!(!path.exists());
    }

    #[test]
    fn document_without_settings_loads_with_defaults() {
        let directory = tempfile::tempdir().unwrap();
        let path = state_path(&directory);
        fs::write(&path, r#"{"streams": {"boats": {"filename": "boats.mp4", "transport": "udp", "port": 40000, "enabled": true, "loop_count": -1}}}"#).unwrap();
        let mut store = StreamStateStore::new(&path);
        assert!(!store.load().unwrap().contains_key("settings"));
        assert_eq!(store.load_settings().unwrap(), RuntimeSettings::default());
        let streams = store.load_streams().unwrap();
        assert_eq!(streams[0].1.choices().mode, None);
        assert_eq!(streams[0].1.choices().gop_frames, None);
    }

    #[test]
    fn documents_match_the_python_fixture() {
        let fixture = fixture("state.json");
        let directory = tempfile::tempdir().unwrap();
        for case in fixture["load_cases"].as_array().unwrap() {
            if case["document"]["streams"]["boats"]["gop_frames"].is_f64() {
                continue; // Deliberate deviation: fractional GOP values are refused here.
            }
            let path = state_path(&directory);
            let raw = serde_json::to_string(&case["document"]).unwrap();
            fs::write(&path, &raw).unwrap();
            let mut store = StreamStateStore::new(&path);
            let outcome = store.load();
            match (&outcome, case["error"].as_str()) {
                (Ok(document), None) => {
                    assert_eq!(Value::Object(document.clone()), case["loaded"], "{case}")
                }
                (Err(error), Some(expected)) => {
                    let expected = expected.replace("{path}", &path.display().to_string());
                    if let Some((prefix, _)) = expected.split_once(" (Python") {
                        assert!(error.to_string().starts_with(prefix), "{case}: {error}");
                    } else {
                        assert_eq!(error.to_string(), expected, "{case}");
                    }
                }
                _ => panic!("{case} gave {outcome:?}"),
            }
            assert_eq!(fs::read_to_string(&path).unwrap(), raw);
        }
        let path = state_path(&directory);
        let mut store = StreamStateStore::new(&path);
        store.save_stream("boats", &record()).unwrap();
        let settings = RuntimeSettings {
            destination_host: Some("receiver".into()),
            ..Default::default()
        };
        store.save_settings(&settings.to_document()).unwrap();
        store
            .save_stream(
                "boats-srt",
                &StreamRecord {
                    transport: "srt_listen".into(),
                    port: Some(41000),
                    ..record()
                },
            )
            .unwrap();
        store
            .save_stream(
                "boats",
                &StreamRecord {
                    enabled: false,
                    frame_rate: Some(12.0),
                    gop_frames: Some(7),
                    mode: Some(VideoMode::Transcode),
                    ..record()
                },
            )
            .unwrap();
        store
            .save_stream(
                "third",
                &StreamRecord {
                    transport: "rtsp".into(),
                    port: None,
                    ..record()
                },
            )
            .unwrap();
        store.rename_stream("boats", "ships").unwrap();
        store.delete_stream("boats-srt").unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            fixture["sequence_text"].as_str().unwrap()
        );

        let extra_path = state_path(&directory);
        fs::write(&extra_path, serde_json::to_string(&serde_json::json!({
            "extra": 1,
            "streams": {"b": {"filename": "boats.mp4", "transport": "udp", "port": 40000, "enabled": true, "mode": null, "loop_count": -1, "frame_rate": null, "gop_frames": null, "note": "kept"},
                        "a": {"filename": "boats.mp4", "transport": "udp", "port": 40000, "enabled": true, "mode": null, "loop_count": -1, "frame_rate": null, "gop_frames": null}},
            "settings": {"udp_first_port": 40001, "unknown": 2}})).unwrap()).unwrap();
        let mut extra_store = StreamStateStore::new(&extra_path);
        extra_store.save_stream("c", &record()).unwrap();
        assert_eq!(
            fs::read_to_string(&extra_path).unwrap(),
            fixture["extra_keys_text"].as_str().unwrap()
        );
        let mut absent = StreamStateStore::new(&directory.path().join("absent.json"));
        assert_eq!(
            Value::Object(absent.load().unwrap()),
            fixture["missing_file_document"]
        );
    }

    #[test]
    fn stream_names_stay_plain() {
        assert!(is_valid_stream_name("boats"));
        assert!(is_valid_stream_name("a-b_c9"));
        assert!(is_valid_stream_name(&"a".repeat(64)));
        assert!(!is_valid_stream_name(&"a".repeat(65)));
        assert!(!is_valid_stream_name(""));
        assert!(!is_valid_stream_name("-abc"));
        assert!(!is_valid_stream_name("bad name"));
        assert!(!is_valid_stream_name("über"));
    }
}
