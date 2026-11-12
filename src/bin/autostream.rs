//! Compose the application; execute all domain work on one supervisor thread.

use std::net::TcpListener;
use std::process::ExitCode;
use std::sync::mpsc::sync_channel;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use autostream::appconfig::Config;
use autostream::hostaddresses::watch_lan_ip;
use autostream::mediamtxprocess::{MediamtxOutcome, spawn_mediamtx, supervise_mediamtx};
use autostream::mediastream::{FfmpegTools, StreamServices};
use autostream::statefile::StreamStateStore;
use autostream::stopsignal::StopSignal;
use autostream::streamapi::{ApiState, HostAddresses, play_router, router};
use autostream::streamlog::{StreamLogger, configure_logging};
use autostream::streamregistry::StreamRegistry;
use autostream::streamresponses::UrlFacts;
use autostream::streamsupervisor::{
    QUEUE_CAPACITY, SnapshotCell, publish, run_loop, snapshot_of, wait_for_mediamtx, watch_files,
};
use autostream::udphost::UdpHostResolver;
use axum::Router;
use tokio::signal::unix::{SignalKind, signal};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("autostream: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let config = Config::from_process_env()?;
    configure_logging(&config.log_level);
    let services = StreamServices {
        store: StreamStateStore::new(&config.state_path),
        resolver: Box::new(UdpHostResolver::new()),
        ffmpeg: Box::new(FfmpegTools),
    };
    let log_dir = config.ffmpeg_log_dir.clone();
    let echo_ffmpeg = config.show_ffmpeg_output();
    let mut registry = StreamRegistry::new(
        config.clone(),
        services,
        Box::new(move |name| StreamLogger::open(name, &log_dir, echo_ffmpeg).map(Arc::new)),
    );
    registry.load_persisted_state()?;
    let (requests, queue) = sync_channel(QUEUE_CAPACITY);
    let stop = StopSignal::new();
    let snapshot: SnapshotCell = Arc::new(Mutex::new(snapshot_of(&mut registry)));

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let api_listener = bind_listener(config.api_port)?;
    let play_listener = bind_listener(config.play_port)?;
    install_signal_handlers(&runtime, &stop)?;
    // MediaMTX is a vital child: the thread raises `stop` if it exits first.
    let mediamtx =
        spawn_mediamtx(&config).map_err(|error| format!("Cannot start MediaMTX: {error}"))?;
    let mediamtx_stop = stop.clone();
    let mediamtx_watch = thread::spawn(move || supervise_mediamtx(mediamtx, &mediamtx_stop));
    let api_state = Arc::new(ApiState {
        requests: requests.clone(),
        snapshot: snapshot.clone(),
        url_facts: UrlFacts {
            rtsp_port: config.rtsp_port,
            hls_port: config.hls_port,
            rtmp_port: config.rtmp_port,
            api_port: config.api_port,
        },
        hls_dir: config.hls_dir.clone(),
        host_addresses: Arc::new(Mutex::new(HostAddresses {
            hostname: config.host_name.clone(),
            lan_ip: config.host_lan_ip.clone(),
        })),
        default_host: config.default_host.clone(),
    });

    let mut watcher = None;
    let mut host_watcher = None;
    if wait_for_mediamtx(config.rtsp_port, &stop) {
        if config.refresh_lan_ip {
            let addresses = api_state.host_addresses.clone();
            let watcher_stop = stop.clone();
            host_watcher = Some(thread::spawn(move || watch_lan_ip(addresses, watcher_stop)));
        }
        runtime.spawn(serve(api_listener, router(api_state.clone()), stop.clone()));
        runtime.spawn(serve(play_listener, play_router(api_state), stop.clone()));
        let videos_dir = config.videos_dir.clone();
        let watcher_stop = stop.clone();
        watcher = Some(thread::spawn(move || {
            watch_files(&videos_dir, requests, watcher_stop)
        }));
        log::info!(
            "Supervisor starting, play page on port {}, developer UI on port {}",
            config.play_port,
            config.api_port
        );
        run_loop(
            &mut registry,
            &queue,
            &mut |registry| publish(registry, &snapshot),
            &stop,
        );
    }

    stop.set();
    runtime.shutdown_timeout(Duration::from_secs(3));
    if let Some(watcher) = host_watcher {
        let _ = watcher.join();
    }
    if let Some(watcher) = watcher
        && !wait_for_thread(&watcher, Duration::from_secs(3))
    {
        log::warn!("File watcher is still blocked in filesystem IO");
    }
    match mediamtx_watch.join() {
        Ok(MediamtxOutcome::Stopped) => Ok(()),
        Ok(MediamtxOutcome::Exited(status)) => Err(format!("MediaMTX exited ({status})").into()),
        Err(_) => Err("MediaMTX watcher panicked".into()),
    }
}

fn bind_listener(port: u16) -> std::io::Result<TcpListener> {
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

/// Serve one port until `stop` is set; a server that cannot run stops the service.
async fn serve(listener: TcpListener, app: Router, stop: StopSignal) {
    let listener = match tokio::net::TcpListener::from_std(listener) {
        Ok(listener) => listener,
        Err(error) => {
            log::error!("Cannot serve the API: {error}");
            stop.set();
            return;
        }
    };
    let shutdown = stop.clone();
    let served = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = tokio::task::spawn_blocking(move || shutdown.wait_forever()).await;
        })
        .await;
    if let Err(error) = served {
        log::error!("API server failed: {error}");
        stop.set();
    }
}

fn install_signal_handlers(
    runtime: &tokio::runtime::Runtime,
    stop: &StopSignal,
) -> std::io::Result<()> {
    let _guard = runtime.enter();
    for kind in [SignalKind::terminate(), SignalKind::interrupt()] {
        let mut signals = signal(kind)?;
        let stop = stop.clone();
        runtime.spawn(async move {
            signals.recv().await;
            stop.set();
        });
    }
    Ok(())
}

fn wait_for_thread(handle: &thread::JoinHandle<()>, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while !handle.is_finished() {
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(50));
    }
    true
}
