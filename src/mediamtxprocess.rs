//! MediaMTX as a child of the service: the container lives and dies with it.
//!
//! The service spawns MediaMTX with the listener addresses from `.env`, so
//! `mediamtx.yml` never carries port numbers that can drift. One thread
//! watches the child: if MediaMTX exits on its own the stop signal is raised
//! and the service exits non-zero so Compose restarts the whole stack; when
//! the service stops, the child gets TERM, then KILL after a bounded wait.

use std::io;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::appconfig::Config;
use crate::stopsignal::StopSignal;

const MEDIAMTX_BINARY: &str = "/mediamtx";
const MEDIAMTX_CONFIG: &str = "/app/mediamtx.yml";
const STOP_TIMEOUT: Duration = Duration::from_secs(5);
const POLL_INTERVAL: Duration = Duration::from_millis(500);

/// How MediaMTX ended relative to the service.
#[derive(Debug, PartialEq, Eq)]
pub enum MediamtxOutcome {
    /// MediaMTX exited while the service still wanted it.
    Exited(ExitStatus),
    /// The service stopped first and MediaMTX was terminated.
    Stopped,
}

/// The MTX_* overrides that hand the `.env` listener ports to MediaMTX.
pub fn mediamtx_environment(config: &Config) -> Vec<(String, String)> {
    [
        ("MTX_RTSPADDRESS", format!(":{}", config.rtsp_port)),
        ("MTX_RTPADDRESS", format!(":{}", config.rtp_port)),
        ("MTX_RTCPADDRESS", format!(":{}", config.rtcp_port)),
        ("MTX_HLSADDRESS", format!(":{}", config.hls_port)),
        ("MTX_RTMPADDRESS", format!(":{}", config.rtmp_port)),
        // The SRT listener only receives the local ffmpeg feeds.
        ("MTX_SRTADDRESS", format!("127.0.0.1:{}", config.srt_port)),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_string(), value))
    .collect()
}

/// Start MediaMTX on the image's config with its output on the container log.
pub fn spawn_mediamtx(config: &Config) -> io::Result<Child> {
    Command::new(MEDIAMTX_BINARY)
        .arg(MEDIAMTX_CONFIG)
        .envs(mediamtx_environment(config))
        .stdin(Stdio::null())
        .spawn()
}

/// Watch the child until it exits or the service stops; raises `stop` if it exits first.
pub fn supervise_mediamtx(mut child: Child, stop: &StopSignal) -> MediamtxOutcome {
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            log::error!("MediaMTX exited ({status}); stopping the service");
            stop.set();
            return MediamtxOutcome::Exited(status);
        }
        if stop.wait(POLL_INTERVAL) {
            terminate(&mut child);
            return MediamtxOutcome::Stopped;
        }
    }
}

/// TERM, then KILL after the bounded wait; always reaps the child.
fn terminate(child: &mut Child) {
    // Sound: the pid belongs to a child this module spawned and has not reaped.
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
    if wait_until(child, Instant::now() + STOP_TIMEOUT).is_some() {
        return;
    }
    log::warn!("MediaMTX ignored SIGTERM; killing it");
    let _ = child.kill();
    let _ = child.wait();
}

fn wait_until(child: &mut Child, deadline: Instant) -> Option<ExitStatus> {
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => return None,
            Err(_) => return None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::process::ExitStatusExt;

    fn shell(script: &str) -> Child {
        Command::new("sh")
            .args(["-c", script])
            .stdin(Stdio::null())
            .spawn()
            .unwrap()
    }

    #[test]
    fn listener_addresses_come_from_the_ports() {
        let mut config = Config::new(8554, 8888, 1935, 8080, "info").unwrap();
        config.rtp_port = 8000;
        config.rtcp_port = 8001;
        config.srt_port = 8890;
        assert_eq!(
            mediamtx_environment(&config),
            [
                ("MTX_RTSPADDRESS", ":8554"),
                ("MTX_RTPADDRESS", ":8000"),
                ("MTX_RTCPADDRESS", ":8001"),
                ("MTX_HLSADDRESS", ":8888"),
                ("MTX_RTMPADDRESS", ":1935"),
                ("MTX_SRTADDRESS", "127.0.0.1:8890"),
            ]
            .map(|(name, value)| (name.to_string(), value.to_string()))
        );
    }

    #[test]
    fn an_exit_raises_the_stop_signal() {
        let stop = StopSignal::new();
        let outcome = supervise_mediamtx(shell("exit 3"), &stop);
        assert_eq!(
            outcome,
            MediamtxOutcome::Exited(ExitStatus::from_raw(3 << 8))
        );
        assert!(stop.is_set());
    }

    #[test]
    fn a_stop_terminates_the_child() {
        let stop = StopSignal::new();
        let setter = stop.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(100));
            setter.set();
        });
        let started = Instant::now();
        let outcome = supervise_mediamtx(shell("sleep 30"), &stop);
        assert_eq!(outcome, MediamtxOutcome::Stopped);
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn a_child_that_ignores_term_is_killed() {
        let child = shell("trap '' TERM; sleep 30");
        // Let the shell install its trap before the TERM arrives.
        thread::sleep(Duration::from_millis(200));
        let stop = StopSignal::new();
        stop.set();
        let started = Instant::now();
        let outcome = supervise_mediamtx(child, &stop);
        assert_eq!(outcome, MediamtxOutcome::Stopped);
        assert!(started.elapsed() >= STOP_TIMEOUT);
        assert!(started.elapsed() < STOP_TIMEOUT + Duration::from_secs(5));
    }
}
