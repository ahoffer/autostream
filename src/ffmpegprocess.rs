//! One child run with file-backed diagnostics and bounded termination.
//!
//! Called only by the supervisor thread. The logger is shared with the stream that owns the run.

use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::streamlog::StreamLogger;

/// What one look at a child tells the stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessObservation {
    /// The exit code, or the negated signal number, once the child exited.
    pub returncode: Option<i32>,
    pub argv: Vec<String>,
    pub running_for: Duration,
    pub diagnostics: String,
}

/// The process boundary: one wrapper owns one run.
pub trait StreamProcess: Send {
    fn start(&mut self, argv: &[String]) -> io::Result<()>;
    fn observe(&mut self) -> ProcessObservation;
    /// Stop the child; false when it did not exit in time and the handle is retained.
    fn stop(&mut self) -> bool;
}

pub struct FFmpegProcess {
    logger: Arc<StreamLogger>,
    child: Option<Child>,
    exit: Option<ExitStatus>,
    argv: Vec<String>,
    started_at: Option<Instant>,
    capture: Option<File>,
    offset: u64,
    tail: Vec<u8>,
}

impl FFmpegProcess {
    pub const STOP_TIMEOUT: Duration = Duration::from_secs(5);
    pub const TAIL_BYTES: usize = 4096;
    pub const READ_BYTES: usize = 65536;

    pub fn new(logger: Arc<StreamLogger>) -> Self {
        Self {
            logger,
            child: None,
            exit: None,
            argv: Vec::new(),
            started_at: None,
            capture: None,
            offset: 0,
            tail: Vec::new(),
        }
    }

    /// Whether the capture file is still open, for tests.
    pub fn captures(&self) -> bool {
        self.capture.is_some()
    }

    fn poll(&mut self) -> Option<i32> {
        let child = self.child.as_mut()?;
        if self.exit.is_none()
            && let Ok(Some(status)) = child.try_wait()
        {
            self.exit = Some(status);
        }
        self.exit.map(returncode)
    }

    fn read_diagnostics(&mut self, finished: bool) {
        let Some(capture) = self.capture.as_ref() else {
            return;
        };
        let forwarded = (|| -> io::Result<()> {
            let size = capture.metadata()?.len();
            // Bound work per tick, including the final drain after exit. Keep the
            // newest diagnostics if the child produced more than we can forward.
            let offset = self
                .offset
                .max(size.saturating_sub(Self::READ_BYTES as u64));
            if offset > self.offset {
                self.logger.warning(&format!(
                    "Skipped {} bytes of FFmpeg diagnostic backlog",
                    offset - self.offset
                ))?;
            }
            let mut chunk = vec![0u8; Self::READ_BYTES];
            let read = capture.read_at(&mut chunk, offset)?;
            chunk.truncate(read);
            self.offset = offset + read as u64;
            if !chunk.is_empty() {
                self.tail.extend_from_slice(&chunk);
                if self.tail.len() > Self::TAIL_BYTES {
                    self.tail.drain(..self.tail.len() - Self::TAIL_BYTES);
                }
                self.logger
                    .info(String::from_utf8_lossy(&chunk).trim_end())?;
            }
            Ok(())
        })();
        if let Err(error) = forwarded {
            // Capture/logging failure must not prevent process cleanup.
            let mut message = format!("Diagnostic capture failed: {error}").into_bytes();
            if message.len() > Self::TAIL_BYTES {
                message.drain(..message.len() - Self::TAIL_BYTES);
            }
            self.tail = message;
        }
        if finished {
            self.capture = None;
        }
    }

    fn wait_until(&mut self, deadline: Instant) -> bool {
        let mut pause = Duration::from_millis(5);
        loop {
            if self.poll().is_some() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(pause);
            pause = (pause * 2).min(Duration::from_millis(50));
        }
    }

    fn send_term(&self) -> io::Result<()> {
        let Some(child) = &self.child else {
            return Ok(());
        };
        // Sound: the pid belongs to a child this wrapper spawned and has not reaped.
        let outcome = unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
        if outcome == 0 {
            return Ok(());
        }
        Err(io::Error::last_os_error())
    }
}

fn returncode(status: ExitStatus) -> i32 {
    status
        .code()
        .or_else(|| status.signal().map(|signal| -signal))
        .unwrap_or(-1)
}

impl StreamProcess for FFmpegProcess {
    fn start(&mut self, argv: &[String]) -> io::Result<()> {
        if !self.argv.is_empty() {
            return Err(io::Error::other("A process wrapper owns only one run"));
        }
        self.argv = argv.to_vec();
        let capture = tempfile::tempfile()?;
        let spawned = (|| -> io::Result<Child> {
            let (program, arguments) = argv
                .split_first()
                .ok_or_else(|| io::Error::other("empty command"))?;
            Command::new(program)
                .args(arguments)
                .stdin(Stdio::null())
                .stdout(Stdio::from(capture.try_clone()?))
                .stderr(Stdio::from(capture.try_clone()?))
                .spawn()
        })();
        match spawned {
            Ok(child) => {
                self.child = Some(child);
                self.capture = Some(capture);
                self.started_at = Some(Instant::now());
                Ok(())
            }
            Err(error) => {
                self.capture = None;
                Err(error)
            }
        }
    }

    fn observe(&mut self) -> ProcessObservation {
        let code = self.poll();
        self.read_diagnostics(code.is_some());
        ProcessObservation {
            returncode: code,
            argv: self.argv.clone(),
            running_for: self
                .started_at
                .map(|started| started.elapsed())
                .unwrap_or_default(),
            diagnostics: String::from_utf8_lossy(&self.tail).into_owned(),
        }
    }

    fn stop(&mut self) -> bool {
        if self.child.is_none() {
            return true;
        }
        if self.poll().is_none() {
            match self.send_term() {
                Ok(()) => {}
                Err(error) if error.raw_os_error() == Some(libc::ESRCH) => {}
                Err(_) => return false,
            }
            if !self.wait_until(Instant::now() + Self::STOP_TIMEOUT) {
                let killed = self.child.as_mut().map(Child::kill);
                match killed {
                    Some(Err(error)) if error.kind() != io::ErrorKind::InvalidInput => {
                        return false;
                    }
                    _ => {}
                }
                if !self.wait_until(Instant::now() + Self::STOP_TIMEOUT) {
                    return false;
                }
            }
        }
        self.observe().returncode.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|part| part.to_string()).collect()
    }

    fn run_until_exit(
        logger: Arc<StreamLogger>,
        script: &str,
    ) -> (FFmpegProcess, ProcessObservation) {
        let mut run = FFmpegProcess::new(logger);
        run.start(&argv(&["sh", "-c", script])).unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while run.observe().returncode.is_none() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(run.stop());
        let observation = run.observe();
        (run, observation)
    }

    #[test]
    fn large_unterminated_output_cannot_block_child_and_tail_is_bounded() {
        let (_, result) = run_until_exit(
            Arc::new(StreamLogger::discard("test")),
            "head -c 1000000 /dev/zero | tr '\\0' x; echo; echo stdout-tail; echo stderr-tail >&2",
        );
        assert_eq!(result.returncode, Some(0));
        assert!(result.diagnostics.len() <= FFmpegProcess::TAIL_BYTES);
        assert!(result.diagnostics.contains("stdout-tail"));
        assert!(result.diagnostics.contains("stderr-tail"));
    }

    #[test]
    fn broken_logger_does_not_prevent_cleanup() {
        if !Path::new("/dev/full").exists() {
            return;
        }
        let logger =
            Arc::new(StreamLogger::at_path("test", Path::new("/dev/full"), false).unwrap());
        let (run, result) = run_until_exit(logger, "head -c 100000 /dev/zero | tr '\\0' x");
        assert_eq!(result.returncode, Some(0));
        assert!(result.diagnostics.starts_with("Diagnostic capture failed"));
        assert!(!run.captures());
    }

    #[test]
    fn spawn_failure_closes_capture_and_propagates() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        assert!(run.start(&argv(&["/nonexistent/ffmpeg"])).is_err());
        assert!(!run.captures());
        assert!(run.stop());
    }

    #[test]
    fn terminates_child_and_owns_only_one_run() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        run.start(&argv(&["sh", "-c", "exec sleep 30"])).unwrap();
        assert!(run.observe().returncode.is_none());
        assert!(run.stop());
        assert_eq!(run.observe().returncode, Some(-libc::SIGTERM));
        assert!(!run.captures());
        assert!(run.start(&argv(&["unused"])).is_err());
    }

    #[test]
    fn kill_escalation_when_term_is_ignored() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        run.start(&argv(&[
            "sh",
            "-c",
            "trap '' TERM; while true; do sleep 1; done",
        ]))
        .unwrap();
        thread::sleep(Duration::from_millis(100));
        let started = Instant::now();
        assert!(run.stop());
        assert!(started.elapsed() >= FFmpegProcess::STOP_TIMEOUT);
        assert_eq!(run.observe().returncode, Some(-libc::SIGKILL));
        assert!(!run.captures());
    }
}
