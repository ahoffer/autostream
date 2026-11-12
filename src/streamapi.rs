//! HTTP transport: validate requests, queue mutations, serve snapshots and HLS segments.

use std::path::{Path, PathBuf};
use std::sync::mpsc::{SyncSender, TrySendError};
use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::oneshot;

use crate::mediastream::{Adjustment, StreamChanges};
use crate::queryform::QueryForm;
use crate::runtimesettings::RuntimeSettings;
use crate::statefile::is_valid_stream_name;
use crate::streamrecord::VideoMode;
use crate::streamregistry::{OperationError, StreamRegistry};
use crate::streamresponses::{UrlFacts, describe_play_rows, describe_streams, describe_transports};
use crate::streamsupervisor::{ApiReply, SnapshotCell, SupervisorRequest, read_snapshot};
use crate::transports::kind_by_id;

/// The control page, the evaluator's play page, and their typeface, served so the pages work offline.
const INDEX_HTML: &str = include_str!("index.html");
const PLAY_HTML: &str = include_str!("play.html");
const FONT_WOFF2: &[u8] = include_bytes!("jost.woff2");

pub use crate::hostaddresses::HostAddresses;

/// The host choices the settings page offers, with the host a blank advertised
/// host yields for the client that asked.
#[derive(Serialize)]
struct HostReport<'a> {
    hostname: &'a str,
    lan_ip: &'a str,
    client_host: String,
}

/// Everything a request handler needs; none of it is mutable stream state.
pub struct ApiState {
    pub requests: SyncSender<SupervisorRequest>,
    pub snapshot: SnapshotCell,
    pub url_facts: UrlFacts,
    pub hls_dir: PathBuf,
    pub host_addresses: Arc<Mutex<HostAddresses>>,
    /// The container's name on the consumer network, for the play page's addresses.
    pub network_host: String,
}

pub fn router(state: Arc<ApiState>) -> Router {
    Router::new().fallback(dispatch).with_state(state)
}

/// The evaluator's play page and the calls it makes; nothing else answers on its port.
pub fn play_router(state: Arc<ApiState>) -> Router {
    Router::new().fallback(dispatch_play).with_state(state)
}

/// A mutation the supervisor executes on its own thread.
#[derive(Clone, Debug, PartialEq)]
pub enum Command {
    RuntimeSettings(RuntimeSettings),
    Create(Creation),
    StartAll,
    StopAll,
    Start(String),
    Stop(String),
    Delete(String),
    Settings(String, StreamChanges),
    /// Play a video file from the play page: start its play stream, made if needed.
    Play(String),
    /// Stop a video file's play stream.
    StopPlaying(String),
}

/// A new stream as the create request describes it.
#[derive(Clone, Debug, PartialEq)]
pub struct Creation {
    pub name: String,
    pub filename: String,
    pub transport: String,
    pub changes: StreamChanges,
    pub start: bool,
}

fn reply(body: Value, status: u16) -> ApiReply {
    ApiReply { body, status }
}

/// Runs on the supervisor thread, never on the submitting HTTP task.
pub fn execute_request(
    registry: &mut StreamRegistry,
    command: Command,
) -> Result<ApiReply, OperationError> {
    let success = match command {
        Command::RuntimeSettings(settings) => registry.apply_settings(settings)?,
        Command::Create(creation) => {
            match registry.create_stream(
                &creation.name,
                &creation.filename,
                &creation.transport,
                &creation.changes,
                creation.start,
            ) {
                Ok(()) => true,
                Err(OperationError::Refused(reason)) => {
                    return Ok(reply(json!({"error": reason}), 400));
                }
                Err(error) => return Err(error),
            }
        }
        Command::Play(filename) => match registry.play_file(&filename) {
            Ok(finished) => finished,
            Err(OperationError::Refused(reason)) => {
                return Ok(reply(json!({"error": reason}), 400));
            }
            Err(error) => return Err(error),
        },
        Command::StopPlaying(filename) => registry.stop_file(&filename)?,
        Command::StartAll => registry.start_all_streams()?,
        Command::StopAll => registry.stop_all_streams(true)?,
        Command::Start(name)
        | Command::Stop(name)
        | Command::Delete(name)
        | Command::Settings(name, _)
            if !registry.has_stream(&name) =>
        {
            return Ok(reply(json!({"error": "Stream not found"}), 404));
        }
        Command::Start(name) => {
            let (stream, services) = registry.stream_mut(&name).expect("checked above");
            stream.start_streaming(services)?
        }
        Command::Stop(name) => {
            let (stream, services) = registry.stream_mut(&name).expect("checked above");
            stream.stop_streaming(services)?
        }
        Command::Delete(name) => registry.delete_stream(&name)?,
        Command::Settings(name, changes) => {
            if let Some(new_name) = &changes.name
                && *new_name != name
                && registry.has_stream(new_name)
            {
                return Ok(reply(
                    json!({"error": format!("A stream named {new_name} exists")}),
                    400,
                ));
            }
            registry.update_stream(&name, &changes)?
        }
    };
    if !success {
        return Ok(reply(
            json!({"success": false, "error": "Operation could not finish; check the stream status"}),
            409,
        ));
    }
    Ok(reply(json!({"success": true}), 200))
}

/// A new stream from a ?name=&file=&transport= query plus its settings; start=0 creates it stopped.
pub fn parse_creation(query: &QueryForm) -> Result<Creation, String> {
    let name = query.first_or_blank("name").trim().to_string();
    let filename = query.first_or_blank("file").trim().to_string();
    let transport = query.first_or_blank("transport").trim().to_string();
    if name.is_empty() {
        return Err("Name the stream".to_string());
    }
    if filename.is_empty() {
        return Err("Choose a video file".to_string());
    }
    if kind_by_id(&transport).is_none() {
        return Err("Choose a transport".to_string());
    }
    let mut changes = parse_settings(query)?;
    // The name, file and transport are the identity and binding, not choices;
    // parse_settings read them for the edit route.
    changes.name = None;
    changes.filename = None;
    changes.transport = None;
    let start = query.first("start").unwrap_or("1") != "0";
    Ok(Creation {
        name,
        filename,
        transport,
        changes,
        start,
    })
}

/// The settings named in a ?name=&file=&transport=&loop=&mode=&gop=&fps= query
/// as changes, or an error message. An absent key leaves that setting alone;
/// fps=auto returns to the file's own rate and gop=auto to one second at the
/// output rate.
pub fn parse_settings(query: &QueryForm) -> Result<StreamChanges, String> {
    let mut changes = StreamChanges::default();
    if let Some(name) = query.first("name") {
        let name = name.trim();
        if !is_valid_stream_name(name) {
            return Err("Stream name: letters, digits, - and _ only, 64 at most".to_string());
        }
        changes.name = Some(name.to_string());
    }
    if let Some(filename) = query.first("file") {
        let filename = filename.trim();
        if filename.is_empty() || filename.contains('/') || filename.starts_with('.') {
            return Err("Choose a video file".to_string());
        }
        changes.filename = Some(filename.to_string());
    }
    if let Some(transport) = query.first("transport") {
        if kind_by_id(transport).is_none() {
            return Err("Unknown transport".to_string());
        }
        changes.transport = Some(transport.to_string());
    }
    if let Some(loop_text) = query.first("loop") {
        let loop_count: i64 = loop_text
            .trim()
            .parse()
            .map_err(|_| "Invalid loop count".to_string())?;
        if loop_count < -1 {
            return Err("Invalid loop count".to_string());
        }
        changes.loop_count = Some(loop_count);
    }
    if let Some(mode) = query.first("mode") {
        changes.mode = Some(VideoMode::parse(mode).ok_or_else(|| "Invalid mode".to_string())?);
    }
    if let Some(gop) = query.first("gop") {
        if gop == "auto" {
            changes.gop_frames = Adjustment::Auto;
        } else {
            let frames: i64 = gop.trim().parse().unwrap_or(0);
            if frames < 1 {
                return Err(
                    "GOP length must be a whole number of frames, 1 or more, or auto".to_string(),
                );
            }
            changes.gop_frames = Adjustment::Set(frames);
        }
    }
    if let Some(fps) = query.first("fps") {
        if fps == "auto" {
            changes.frame_rate = Adjustment::Auto;
        } else {
            let rate: f64 = fps.trim().parse().unwrap_or(0.0);
            // Keep frame-rate validation consistent with the control UI.
            if !(1.0..=120.0).contains(&rate) {
                return Err("FPS must be a number from 1 to 120, or auto".to_string());
            }
            changes.frame_rate = Adjustment::Set(rate);
        }
    }
    Ok(changes)
}

fn json_response(status: u16, body: &Value) -> Response {
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, axum::Json(body.clone())).into_response()
}

fn not_found() -> Response {
    json_response(404, &json!({"error": "Not found"}))
}

fn bytes_response(content_type: &str, cache_control: &str, data: Vec<u8>) -> Response {
    let mut response = Response::new(Body::from(data));
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(content_type).expect("static header"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_str(cache_control).expect("static header"),
    );
    response
}

async fn dispatch(
    State(state): State<Arc<ApiState>>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
) -> Response {
    let path = uri.path();
    let query = QueryForm::parse(uri.query().unwrap_or(""));
    match method {
        Method::GET => get(&state, path, &headers),
        Method::POST => post(&state, path, &query).await,
        _ => json_response(501, &json!({"error": "Unsupported method"})),
    }
}

fn host_header(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
}

fn html_response(page: &'static str) -> Response {
    let mut response = Response::new(Body::from(page));
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    response
}

fn font_response() -> Response {
    bytes_response("font/woff2", "public, max-age=86400", FONT_WOFF2.to_vec())
}

async fn dispatch_play(State(state): State<Arc<ApiState>>, method: Method, uri: Uri) -> Response {
    let query = QueryForm::parse(uri.query().unwrap_or(""));
    let play_command = |command: fn(String) -> Command| match query
        .first("file")
        .map(str::trim)
        .filter(|file| !file.is_empty())
    {
        Some(file) => Ok(command(file.to_string())),
        None => Err(json_response(400, &json!({"error": "Choose a video file"}))),
    };
    let command = match (&method, uri.path()) {
        (&Method::GET, "/" | "/index.html") => return html_response(PLAY_HTML),
        (&Method::GET, "/jost.woff2") => return font_response(),
        (&Method::GET, "/api/play") => {
            let rows = describe_play_rows(
                &read_snapshot(&state.snapshot),
                &state.network_host,
                &state.url_facts,
            );
            return json_response(200, &serde_json::to_value(rows).unwrap_or(Value::Null));
        }
        (&Method::POST, "/api/play/start") => play_command(Command::Play),
        (&Method::POST, "/api/play/stop") => play_command(Command::StopPlaying),
        (&Method::GET | &Method::POST, _) => return not_found(),
        _ => return json_response(501, &json!({"error": "Unsupported method"})),
    };
    match command {
        Ok(command) => submit(&state, command).await,
        Err(refusal) => refusal,
    }
}

fn get(state: &ApiState, path: &str, headers: &HeaderMap) -> Response {
    match path {
        "/" | "/index.html" => html_response(INDEX_HTML),
        "/api/streams" => {
            let snapshot = read_snapshot(&state.snapshot);
            let host = state.url_facts.host_for(
                snapshot.settings.advertised_host.as_deref(),
                host_header(headers),
            );
            let rows = describe_streams(
                &snapshot.statuses,
                &snapshot.settings,
                &host,
                &state.url_facts,
            );
            json_response(200, &serde_json::to_value(rows).unwrap_or(Value::Null))
        }
        "/api/videos" => json_response(200, &json!(read_snapshot(&state.snapshot).videos)),
        "/api/transports" => json_response(
            200,
            &serde_json::to_value(describe_transports()).unwrap_or(Value::Null),
        ),
        "/api/settings" => json_response(
            200,
            &Value::Object(read_snapshot(&state.snapshot).settings.to_document()),
        ),
        "/api/host" => {
            let addresses = state
                .host_addresses
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let report = HostReport {
                hostname: &addresses.hostname,
                lan_ip: &addresses.lan_ip,
                client_host: state.url_facts.host_for(None, host_header(headers)),
            };
            json_response(200, &serde_json::to_value(report).unwrap_or(Value::Null))
        }
        "/jost.woff2" => font_response(),
        _ => match path.strip_prefix("/hls/") {
            Some(relative) => hls_file(&state.hls_dir, relative),
            None => not_found(),
        },
    }
}

/// A playlist or segment written by an HLS stream; nothing outside the HLS directory.
fn hls_file(hls_dir: &Path, relative: &str) -> Response {
    let Ok(root) = hls_dir.canonicalize() else {
        return not_found();
    };
    let Ok(target) = root.join(relative).canonicalize() else {
        return not_found();
    };
    let content_type = match target.extension().and_then(|extension| extension.to_str()) {
        Some("m3u8") => "application/vnd.apple.mpegurl",
        Some("ts") => "video/mp2t",
        _ => return not_found(),
    };
    if !target.starts_with(&root) || !target.is_file() {
        return not_found();
    }
    match std::fs::read(&target) {
        Ok(data) => bytes_response(content_type, "no-store", data),
        Err(_) => not_found(),
    }
}

async fn post(state: &ApiState, path: &str, query: &QueryForm) -> Response {
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    if parts == ["api", "settings"] {
        return match RuntimeSettings::from_query(query) {
            Ok(settings) => submit(state, Command::RuntimeSettings(settings)).await,
            Err(error) => json_response(400, &json!({"error": error})),
        };
    }
    if parts.len() < 2 || parts[..2] != ["api", "streams"] {
        return not_found();
    }
    if parts.len() == 2 {
        return match parse_creation(query) {
            Ok(creation) => submit(state, Command::Create(creation)).await,
            Err(error) => json_response(400, &json!({"error": error})),
        };
    }
    if parts.len() == 3 {
        return match parts[2] {
            "start-all" => submit(state, Command::StartAll).await,
            "stop-all" => submit(state, Command::StopAll).await,
            _ => not_found(),
        };
    }
    if parts.len() != 4 {
        return not_found();
    }
    let name = parts[2].to_string();
    let command = match parts[3] {
        "settings" => match parse_settings(query) {
            Ok(changes) => Command::Settings(name, changes),
            Err(error) => return json_response(400, &json!({"error": error})),
        },
        "start" => Command::Start(name),
        "stop" => Command::Stop(name),
        "delete" => Command::Delete(name),
        _ => return json_response(400, &json!({"error": "Unknown action"})),
    };
    submit(state, command).await
}

/// Queue the command without blocking and wait for the supervisor's answer.
async fn submit(state: &ApiState, command: Command) -> Response {
    let (reply, answer) = oneshot::channel();
    let request = SupervisorRequest::Operation {
        operation: Box::new(move |registry| {
            execute_request(registry, command).map_err(|error| error.to_string())
        }),
        reply,
    };
    match state.requests.try_send(request) {
        Ok(()) => {}
        Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
            return json_response(
                503,
                &json!({"error": "Supervisor queue is full; request was not accepted"}),
            );
        }
    }
    match answer.await {
        Ok(Ok(reply)) => json_response(reply.status, &reply.body),
        Ok(Err(error)) => {
            log::error!("Stream request failed: {error}");
            json_response(500, &json!({"success": false, "error": error}))
        }
        Err(_) => json_response(
            500,
            &json!({"success": false, "error": "Supervisor is shutting down"}),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};
    use std::sync::Mutex;
    use std::sync::mpsc::sync_channel;
    use std::thread;
    use std::time::Duration;

    use crate::appconfig::Config;
    use crate::stopsignal::StopSignal;
    use crate::streamsupervisor::{run_loop, snapshot_of};
    use crate::testsupport::{
        FakeFfmpeg, Harness, block_writes, build_registry, fixture, inventory, test_settings,
    };

    /// One HTTP exchange as the test client saw it.
    struct Exchange {
        status: u16,
        content_type: Option<String>,
        cache_control: Option<String>,
        body: Vec<u8>,
    }

    impl Exchange {
        fn json(&self) -> Value {
            serde_json::from_slice(&self.body)
                .unwrap_or_else(|error| panic!("{error}: {}", String::from_utf8_lossy(&self.body)))
        }
    }

    /// A running API server over a supervisor thread and fake processes.
    struct ApiHarness {
        address: SocketAddr,
        host_addresses: Arc<Mutex<HostAddresses>>,
        stop: StopSignal,
        supervisor: Option<thread::JoinHandle<()>>,
        runtime: Option<tokio::runtime::Runtime>,
        requests: SyncSender<SupervisorRequest>,
        ffmpeg: FakeFfmpeg,
        config: Config,
        state: tempfile::TempDir,
        _videos: tempfile::TempDir,
        _hls: tempfile::TempDir,
    }

    impl ApiHarness {
        fn start(harness: Harness, queue_capacity: usize) -> Self {
            Self::serve(harness, queue_capacity, router)
        }

        fn serve(
            harness: Harness,
            queue_capacity: usize,
            make_router: fn(Arc<ApiState>) -> Router,
        ) -> Self {
            let Harness {
                mut registry,
                ffmpeg,
                config,
                videos,
                state,
                hls,
                ..
            } = harness;
            let (requests, queue) = sync_channel(queue_capacity);
            let stop = StopSignal::new();
            let snapshot: SnapshotCell = Arc::new(Mutex::new(snapshot_of(&mut registry)));
            let loop_stop = stop.clone();
            let cell = snapshot.clone();
            let supervisor = thread::spawn(move || {
                run_loop(
                    &mut registry,
                    &queue,
                    &mut |registry| crate::streamsupervisor::publish(registry, &cell),
                    &loop_stop,
                );
            });
            let api_state = Arc::new(ApiState {
                requests: requests.clone(),
                snapshot,
                url_facts: UrlFacts {
                    rtsp_port: 8554,
                    hls_port: 8888,
                    rtmp_port: 1935,
                    api_port: 8080,
                },
                hls_dir: config.hls_dir.clone(),
                host_addresses: Arc::new(Mutex::new(HostAddresses {
                    hostname: "publisher".into(),
                    lan_ip: "192.168.1.20".into(),
                })),
                network_host: "autostream".into(),
            });
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap();
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            listener.set_nonblocking(true).unwrap();
            let server_stop = stop.clone();
            let host_addresses = api_state.host_addresses.clone();
            runtime.spawn(async move {
                let listener = tokio::net::TcpListener::from_std(listener).unwrap();
                axum::serve(listener, make_router(api_state))
                    .with_graceful_shutdown(async move {
                        let _ =
                            tokio::task::spawn_blocking(move || server_stop.wait_forever()).await;
                    })
                    .await
                    .unwrap();
            });
            Self {
                address,
                host_addresses,
                stop,
                supervisor: Some(supervisor),
                runtime: Some(runtime),
                requests,
                ffmpeg,
                config,
                state,
                _videos: videos,
                _hls: hls,
            }
        }

        fn fetch(&self, method: &str, path: &str, host: Option<&str>) -> Exchange {
            let mut connection = TcpStream::connect(self.address).unwrap();
            connection
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let host = host.map_or_else(|| self.address.to_string(), str::to_string);
            let request =
                format!("{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n");
            connection.write_all(request.as_bytes()).unwrap();
            let mut raw = Vec::new();
            connection.read_to_end(&mut raw).unwrap();
            let split = raw
                .windows(4)
                .position(|window| window == b"\r\n\r\n")
                .expect("response head");
            let head = String::from_utf8_lossy(&raw[..split]).into_owned();
            let body = raw[split + 4..].to_vec();
            let mut lines = head.lines();
            let status: u16 = lines
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|code| code.parse().ok())
                .expect("status line");
            let header = |name: &str| {
                lines.clone().find_map(|line| {
                    let (key, value) = line.split_once(':')?;
                    key.eq_ignore_ascii_case(name)
                        .then(|| value.trim().to_string())
                })
            };
            Exchange {
                status,
                content_type: header("content-type"),
                cache_control: header("cache-control"),
                body,
            }
        }

        fn request(&self, method: &str, path: &str) -> (u16, Value) {
            let exchange = self.fetch(method, path, None);
            (exchange.status, exchange.json())
        }

        fn stream(&self, name: &str, host: Option<&str>) -> Value {
            let rows = self.fetch("GET", "/api/streams", host).json();
            rows.as_array()
                .unwrap()
                .iter()
                .find(|row| row["name"] == name)
                .unwrap_or_else(|| panic!("no stream {name}"))
                .clone()
        }
    }

    impl Drop for ApiHarness {
        fn drop(&mut self) {
            self.stop.set();
            if let Some(runtime) = self.runtime.take() {
                runtime.shutdown_timeout(Duration::from_secs(3));
            }
            if let Some(supervisor) = self.supervisor.take() {
                supervisor.join().unwrap();
            }
        }
    }

    fn setup() -> ApiHarness {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        harness.create("boats-udp", &source, "udp");
        ApiHarness::start(harness, 8)
    }

    fn source_name(api: &ApiHarness) -> String {
        let row = api.stream("boats", None);
        row["filename"].as_str().unwrap().to_string()
    }

    #[test]
    fn the_play_port_plays_and_stops_videos_and_offers_nothing_else() {
        let api = ApiHarness::serve(Harness::new(), 8, play_router);
        let page = api.fetch("GET", "/", None);
        assert_eq!(page.status, 200);
        assert_eq!(
            page.content_type.as_deref(),
            Some("text/html; charset=utf-8")
        );
        assert_eq!(api.fetch("GET", "/api/streams", None).status, 404);
        assert_eq!(
            api.fetch("POST", "/api/streams/start-all", None).status,
            404
        );
        assert_eq!(
            api.request("POST", "/api/play/start"),
            (400, json!({"error": "Choose a video file"}))
        );

        let rows = api.request("GET", "/api/play").1;
        assert_eq!(
            rows,
            json!([{"file": "source.mp4", "state": null, "reason": null, "playing": false, "url": null}])
        );
        assert_eq!(
            api.request("POST", "/api/play/start?file=source.mp4"),
            (200, json!({"success": true}))
        );
        let row = &api.request("GET", "/api/play").1[0];
        assert_eq!(row["playing"], true);
        // The test settings advertise no host, so the address names the container.
        assert_eq!(row["url"], "rtsp://autostream:8554/source");
        assert_eq!(
            api.request("POST", "/api/play/stop?file=source.mp4"),
            (200, json!({"success": true}))
        );
        assert_eq!(api.request("GET", "/api/play").1[0]["playing"], false);
        assert_eq!(api.request("POST", "/api/play/start?file=other.mp4").0, 400);
    }

    #[test]
    fn create_list_and_delete_a_stream() {
        let api = setup();
        let source = source_name(&api);
        let (status, body) = api.request(
            "POST",
            &format!("/api/streams?name=boats-srt&file={source}&transport=srt_listen&mode=transcode&fps=15"),
        );
        assert_eq!((status, body), (200, json!({"success": true})));
        let row = api.stream("boats-srt", Some("operator-console:9080"));
        assert_eq!(row["transport"], "srt_listen");
        assert_eq!(row["port"], 41000);
        assert_eq!(row["frame_rate"], 15.0);
        assert_eq!(row["running"], true);
        assert_eq!(
            row["register"],
            json!([["register", "srt://operator-console:41000"]])
        );
        assert_eq!(row["delivery"], "srt://0.0.0.0:41000?mode=listener");
        assert_eq!(
            api.request("POST", "/api/streams/boats-srt/delete"),
            (200, json!({"success": true}))
        );
        let names: Vec<Value> = api
            .request("GET", "/api/streams")
            .1
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["name"].clone())
            .collect();
        assert!(!names.contains(&json!("boats-srt")));
    }

    #[test]
    fn register_urls_follow_the_host_header_unless_an_advertised_host_is_set() {
        let api = setup();
        assert_eq!(
            api.fetch("GET", "/api/host", Some("operator-console:9080"))
                .json(),
            json!({
                "hostname": "publisher",
                "lan_ip": "192.168.1.20",
                "client_host": "operator-console"
            })
        );
        let row = api.stream("boats", Some("operator-console:9080"));
        assert_eq!(
            row["register"],
            json!([
                ["rtsp", "rtsp://operator-console:8554/boats"],
                ["hls", "http://operator-console:8888/boats/index.m3u8"],
                ["rtmp", "rtmp://operator-console:1935/boats"]
            ])
        );
        assert_eq!(
            api.stream("boats", Some("[::1]:9080"))["register"][0][1],
            "rtsp://[::1]:8554/boats"
        );
        assert_eq!(
            api.request(
                "POST",
                "/api/settings?advertised_host=publisher-host&destination_host=receiver"
            )
            .0,
            200
        );
        assert_eq!(
            api.stream("boats", Some("operator-console:9080"))["register"][0][1],
            "rtsp://publisher-host:8554/boats"
        );
        assert_eq!(
            api.request("GET", "/api/settings").1["advertised_host"],
            "publisher-host"
        );
        assert_eq!(
            api.request(
                "POST",
                "/api/settings?advertised_host=&destination_host=receiver"
            )
            .0,
            200
        );
        assert_eq!(
            api.stream("boats", Some("operator-console:9080"))["register"][0][1],
            "rtsp://operator-console:8554/boats"
        );
    }

    #[test]
    fn host_api_reports_address_changes_without_changing_saved_settings() {
        let api = setup();
        let settings = api.request("GET", "/api/settings").1;
        for address in ["172.31.50.72", "", "192.168.0.228"] {
            api.host_addresses.lock().unwrap().lan_ip = address.into();
            let report = api.fetch("GET", "/api/host", Some("localhost:9080")).json();
            assert_eq!(report["lan_ip"], address);
            assert_eq!(report["hostname"], "publisher");
            assert_eq!(report["client_host"], "localhost");
            assert_eq!(api.request("GET", "/api/settings").1, settings);
        }
    }

    #[test]
    fn stream_settings_can_change_name_file_transport_and_encoding_together() {
        let api = setup();
        assert_eq!(
            api.request(
                "POST",
                "/api/streams/boats-udp/settings?transport=tcp_listen&loop=1"
            )
            .0,
            200
        );
        let row = api.stream("boats-udp", None);
        assert_eq!(
            (
                row["transport"].as_str(),
                row["port"].as_i64(),
                row["loop_count"].as_i64()
            ),
            (Some("tcp_listen"), Some(42000), Some(1))
        );
        assert_eq!(
            api.request("POST", "/api/streams/boats-udp/settings?name=boats-tcp")
                .0,
            200
        );
        assert_eq!(api.stream("boats-tcp", None)["port"], 42000);
        let (status, body) = api.request("POST", "/api/streams/boats-tcp/settings?name=boats");
        assert_eq!(status, 400);
        assert!(body["error"].as_str().unwrap().contains("exists"));
        assert_eq!(
            api.request("POST", "/api/streams/absent/settings?loop=1").0,
            404
        );
    }

    #[test]
    fn invalid_requests_are_rejected_before_queueing() {
        let api = setup();
        let source = source_name(&api);
        let runs_before = api.ffmpeg.run_count();
        for path in [
            format!("/api/streams?name=&file={source}&transport=udp"),
            "/api/streams?name=x&file=&transport=udp".to_string(),
            format!("/api/streams?name=x&file={source}&transport=pigeon"),
            format!("/api/streams?name=x&file={source}&transport=udp&fps=nan"),
            "/api/streams/boats/settings?transport=pigeon".to_string(),
            "/api/streams/boats/settings?file=../etc/passwd".to_string(),
            "/api/streams/boats/settings?name=bad%20name".to_string(),
            "/api/settings?udp_first_port=abc".to_string(),
            "/api/settings?multicast_group=10.0.0.1".to_string(),
        ] {
            assert_eq!(api.request("POST", &path).0, 400, "{path}");
        }
        assert_eq!(api.ffmpeg.run_count(), runs_before);
        let (status, body) = api.request(
            "POST",
            &format!("/api/streams?name=boats&file={source}&transport=udp"),
        );
        assert_eq!(status, 400);
        assert!(body["error"].as_str().unwrap().contains("exists"));
    }

    #[test]
    fn videos_transports_and_ui_are_served() {
        let api = setup();
        let source = source_name(&api);
        assert_eq!(api.request("GET", "/api/videos").1, json!([source]));
        let transports = api.request("GET", "/api/transports").1;
        let ids: Vec<&str> = transports
            .as_array()
            .unwrap()
            .iter()
            .map(|kind| kind["id"].as_str().unwrap())
            .collect();
        assert!(ids.contains(&"srt_listen"));
        assert!(transports.as_array().unwrap().iter().all(|kind| {
            ["id", "label", "direction", "carries_klv"]
                .iter()
                .all(|key| kind.get(key).is_some())
        }));
        let page = api.fetch("GET", "/", None);
        assert_eq!(
            (page.status, page.content_type.as_deref()),
            (200, Some("text/html; charset=utf-8"))
        );
        let font = api.fetch("GET", "/jost.woff2", None);
        assert_eq!(
            (font.status, font.content_type.as_deref(), &font.body[..4]),
            (200, Some("font/woff2"), &b"wOF2"[..])
        );
        assert_eq!(font.cache_control.as_deref(), Some("public, max-age=86400"));
        assert_eq!(
            api.request("POST", "/api/streams/stop-all").1,
            json!({"success": true})
        );
        assert!(
            api.request("GET", "/api/streams")
                .1
                .as_array()
                .unwrap()
                .iter()
                .all(|row| row["enabled"] == false)
        );
        assert_eq!(
            api.request("POST", "/api/streams/start-all").1,
            json!({"success": true})
        );
        assert!(
            api.request("GET", "/api/streams")
                .1
                .as_array()
                .unwrap()
                .iter()
                .all(|row| row["running"] == true)
        );
    }

    #[test]
    fn hls_files_are_served_from_the_hls_directory_only() {
        let api = setup();
        let playlist = api.config.hls_dir.join("boats-hls").join("index.m3u8");
        std::fs::create_dir_all(playlist.parent().unwrap()).unwrap();
        std::fs::write(&playlist, "#EXTM3U\n").unwrap();
        std::fs::write(playlist.with_file_name("seg00001.ts"), vec![0x47u8; 188]).unwrap();
        std::fs::write(playlist.with_file_name("notes.txt"), "private").unwrap();
        let served = api.fetch("GET", "/hls/boats-hls/index.m3u8", None);
        assert_eq!(
            (
                served.status,
                served.content_type.as_deref(),
                served.body.as_slice()
            ),
            (
                200,
                Some("application/vnd.apple.mpegurl"),
                &b"#EXTM3U\n"[..]
            )
        );
        assert_eq!(served.cache_control.as_deref(), Some("no-store"));
        assert_eq!(
            api.fetch("GET", "/hls/boats-hls/seg00001.ts", None)
                .content_type
                .as_deref(),
            Some("video/mp2t")
        );
        assert_eq!(
            api.fetch("GET", "/hls/boats-hls/notes.txt", None).status,
            404
        );
        assert_eq!(
            api.fetch("GET", "/hls/../streams_config.json", None).status,
            404
        );
        assert_eq!(
            api.fetch("GET", "/hls/missing/index.m3u8", None).status,
            404
        );
    }

    #[test]
    fn full_queue_rejects_mutation_but_cached_get_still_responds() {
        let api = setup();
        let entered = StopSignal::new();
        let release = StopSignal::new();
        let (slow_reply, _answer) = oneshot::channel();
        let slow = SupervisorRequest::Operation {
            operation: Box::new({
                let entered = entered.clone();
                let release = release.clone();
                move |_| {
                    entered.set();
                    assert!(
                        release.wait(Duration::from_secs(3)),
                        "test did not release work"
                    );
                    Ok(reply(json!({"success": true}), 200))
                }
            }),
            reply: slow_reply,
        };
        api.requests.send(slow).unwrap();
        assert!(entered.wait(Duration::from_secs(2)));
        let mut answers = Vec::new();
        for _ in 0..8 {
            let (reply, answer) = oneshot::channel();
            answers.push(answer);
            api.requests
                .try_send(SupervisorRequest::Operation {
                    operation: Box::new(|_| {
                        Ok(ApiReply {
                            body: json!({"success": true}),
                            status: 200,
                        })
                    }),
                    reply,
                })
                .unwrap();
        }
        assert_eq!(api.request("POST", "/api/streams/stop-all").0, 503);
        assert_eq!(api.request("GET", "/api/streams").0, 200);
        release.set();
    }

    #[test]
    fn failed_save_still_publishes_actual_stop() {
        let api = setup();
        block_writes(&api.state, true);
        let (status, body) = api.request("POST", "/api/streams/boats/stop");
        block_writes(&api.state, false);
        assert_eq!(status, 500);
        assert_eq!(body["success"], false);
        assert!(body["error"].as_str().unwrap().contains("denied"), "{body}");
        let row = api.stream("boats", None);
        assert_eq!(row["enabled"], false);
        assert_eq!(row["running"], false);
    }

    #[test]
    fn queued_lookup_of_a_deleted_stream_finds_nothing() {
        let mut harness = Harness::new();
        let source = harness.source_name();
        harness.create("boats", &source, "rtsp");
        assert!(harness.registry.delete_stream("boats").unwrap());
        let reply = execute_request(&mut harness.registry, Command::Start("boats".into())).unwrap();
        assert_eq!(reply.status, 404);
        assert!(reply.body["error"].as_str().unwrap().contains("not found"));
    }

    fn changes_document(changes: &StreamChanges) -> Value {
        let mut document = serde_json::Map::new();
        if let Some(name) = &changes.name {
            document.insert("name".into(), json!(name));
        }
        if let Some(filename) = &changes.filename {
            document.insert("filename".into(), json!(filename));
        }
        if let Some(transport) = &changes.transport {
            document.insert("transport".into(), json!(transport));
        }
        if let Some(loop_count) = changes.loop_count {
            document.insert("loop_count".into(), json!(loop_count));
        }
        if let Some(mode) = changes.mode {
            document.insert("mode".into(), json!(mode.as_str()));
        }
        match changes.gop_frames {
            Adjustment::Keep => {}
            Adjustment::Auto => {
                document.insert("gop_frames".into(), Value::Null);
            }
            Adjustment::Set(frames) => {
                document.insert("gop_frames".into(), json!(frames));
            }
        }
        match changes.frame_rate {
            Adjustment::Keep => {}
            Adjustment::Auto => {
                document.insert("frame_rate".into(), Value::Null);
            }
            Adjustment::Set(rate) => {
                document.insert("frame_rate".into(), json!(rate));
            }
        }
        Value::Object(document)
    }

    #[test]
    fn query_parsing_matches_the_python_fixture() {
        let fixture = fixture("queries.json");
        for case in fixture["parse_settings"].as_array().unwrap() {
            let query = QueryForm::parse(case["query"].as_str().unwrap());
            match (parse_settings(&query), case["error"].as_str()) {
                (Ok(changes), None) => {
                    assert_eq!(changes_document(&changes), case["value"], "{case}")
                }
                (Err(error), Some(expected)) => assert_eq!(error, expected, "{case}"),
                (outcome, _) => panic!("{case} gave {outcome:?}"),
            }
        }
        for case in fixture["parse_creation"].as_array().unwrap() {
            let query = QueryForm::parse(case["query"].as_str().unwrap());
            match (parse_creation(&query), case["error"].as_str()) {
                (Ok(creation), None) => {
                    let document = json!({
                        "name": creation.name, "filename": creation.filename, "transport": creation.transport,
                        "choices": changes_document(&creation.changes), "start": creation.start,
                    });
                    assert_eq!(document, case["value"], "{case}");
                }
                (Err(error), Some(expected)) => assert_eq!(error, expected, "{case}"),
                (outcome, _) => panic!("{case} gave {outcome:?}"),
            }
        }
    }

    /// The request sequence `tests/fixtures/generate.py` recorded, replayed in order.
    #[test]
    fn api_exchanges_match_the_python_fixture() {
        let mut harness = Harness::fresh(&["boats.mp4"]);
        harness.registry = build_registry(
            &harness.config,
            Some(&test_settings()),
            &harness.ffmpeg,
            &harness.resolver,
        );
        harness
            .registry
            .supervise(Some(&crate::filewatch::Inventory {
                creates: vec!["boats.mp4".into()],
                ..inventory(&["boats.mp4"])
            }));
        harness.create("boats", "boats.mp4", "rtsp");
        harness.create("boats-udp", "boats.mp4", "udp");
        let playlist = harness.config.hls_dir.join("boats-hls").join("index.m3u8");
        std::fs::create_dir_all(playlist.parent().unwrap()).unwrap();
        std::fs::write(&playlist, "#EXTM3U\n").unwrap();
        std::fs::write(playlist.with_file_name("seg00001.ts"), vec![0x47u8; 188]).unwrap();
        std::fs::write(playlist.with_file_name("notes.txt"), "private").unwrap();
        let quoted = |path: &Path| {
            let text = serde_json::to_string(&path.to_string_lossy()).unwrap();
            text[1..text.len() - 1].to_string()
        };
        let text = std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/api.json"),
        )
        .unwrap();
        let text = text
            .replace("{videos_dir}", &quoted(harness.videos.path()))
            .replace("{hls_dir}", &quoted(harness.hls.path()));
        let fixture: Value = serde_json::from_str(&text).unwrap();
        let api = ApiHarness::start(harness, 8);
        for exchange in fixture["exchanges"].as_array().unwrap() {
            let method = exchange["method"].as_str().unwrap();
            let path = exchange["path"].as_str().unwrap();
            let host = exchange["headers"]["Host"].as_str();
            let seen = api.fetch(method, path, host);
            let label = format!("{method} {path}");
            assert_eq!(
                seen.status,
                exchange["status"].as_u64().unwrap() as u16,
                "{label}"
            );
            assert_eq!(
                seen.content_type.as_deref(),
                exchange["content_type"].as_str(),
                "{label}"
            );
            assert_eq!(
                seen.cache_control.as_deref(),
                exchange["cache_control"].as_str(),
                "{label}"
            );
            if let Some(expected) = exchange.get("json") {
                assert_eq!(&seen.json(), expected, "{label}");
            } else {
                assert_eq!(
                    seen.body.len() as u64,
                    exchange["body_length"].as_u64().unwrap(),
                    "{label}"
                );
                let prefix =
                    String::from_utf8_lossy(&seen.body[..8.min(seen.body.len())]).into_owned();
                let expected_prefix = exchange["body_prefix"].as_str().unwrap();
                assert_eq!(
                    prefix.as_bytes()[..4],
                    expected_prefix.as_bytes()[..4],
                    "{label}"
                );
            }
        }
        let document = crate::statefile::StreamStateStore::new(&api.config.state_path)
            .load()
            .unwrap();
        assert_eq!(Value::Object(document), fixture["final_state"]);
    }
}
