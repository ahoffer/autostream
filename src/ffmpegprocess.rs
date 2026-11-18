//! One run with file-backed diagnostics and bounded termination.
//!
//! A run is ffmpeg alone, or ffmpeg feeding a relay through a pipe on its
//! standard output. Called only by the supervisor thread. The logger is
//! shared with the stream that owns the run.

use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::ffmpegcmd::shell_join;
use crate::streamlog::StreamLogger;

/// What one run executes: ffmpeg, and for a relayed delivery the relay that reads ffmpeg's standard output.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct StreamCommand {
    pub argv: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub relay: Option<Vec<String>>,
}

impl StreamCommand {
    pub fn is_empty(&self) -> bool {
        self.argv.is_empty()
    }

    /// The command as a POSIX shell would need it typed, relay included.
    pub fn shell_text(&self) -> String {
        let mut text = shell_join(&self.argv);
        if let Some(relay) = &self.relay {
            text.push_str(" | ");
            text.push_str(&shell_join(relay));
        }
        text
    }
}

/// What one look at a run tells the stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessObservation {
    /// Once every process exited, an exit code or negated signal number. A
    /// failed relay explains the run, since ffmpeg then fails on the broken
    /// pipe; otherwise it is ffmpeg's.
    pub returncode: Option<i32>,
    pub command: StreamCommand,
    pub running_for: Duration,
    pub diagnostics: String,
}

/// The process boundary: one wrapper owns one run.
pub trait StreamProcess: Send {
    fn start(&mut self, command: &StreamCommand) -> io::Result<()>;
    fn observe(&mut self) -> ProcessObservation;
    /// Stop the run; false when it did not exit in time and the handles are retained.
    fn stop(&mut self) -> bool;
}

/// One spawned process of a run and how it ended.
struct RunChild {
    child: Child,
    exit: Option<ExitStatus>,
}

pub struct FFmpegProcess {
    logger: Arc<StreamLogger>,
    /// ffmpeg first, then the relay when the command has one.
    children: Vec<RunChild>,
    first_exit_at: Option<Instant>,
    partners_terminated: bool,
    command: StreamCommand,
    started_at: Option<Instant>,
    capture: Option<File>,
    offset: u64,
    tail: Vec<u8>,
}

impl FFmpegProcess {
    pub const STOP_TIMEOUT: Duration = Duration::from_secs(5);
    /// How long the rest of a pipeline may run on after one process exits.
    /// A clean ffmpeg exit leaves the relay this long to drain the pipe.
    pub const PARTNER_GRACE: Duration = Duration::from_secs(5);
    pub const TAIL_BYTES: usize = 4096;
    pub const READ_BYTES: usize = 65536;

    pub fn new(logger: Arc<StreamLogger>) -> Self {
        Self {
            logger,
            children: Vec::new(),
            first_exit_at: None,
            partners_terminated: false,
            command: StreamCommand::default(),
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
        if self.children.is_empty() {
            return None;
        }
        for run_child in &mut self.children {
            if run_child.exit.is_none()
                && let Ok(Some(status)) = run_child.child.try_wait()
            {
                run_child.exit = Some(status);
            }
        }
        let codes: Vec<Option<i32>> = self
            .children
            .iter()
            .map(|run_child| run_child.exit.map(returncode))
            .collect();
        if codes.iter().all(Option::is_some) {
            let downstream_failure = codes.iter().flatten().rev().find(|code| **code != 0);
            return Some(downstream_failure.copied().unwrap_or(0));
        }
        if codes.iter().any(Option::is_some) {
            let first_exit = *self.first_exit_at.get_or_insert_with(Instant::now);
            if !self.partners_terminated && first_exit.elapsed() >= Self::PARTNER_GRACE {
                self.partners_terminated = true;
                let _ = self.send_term();
            }
        }
        None
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

    /// TERM every process still running; a process that is already gone is not an error.
    fn send_term(&self) -> io::Result<()> {
        for run_child in self
            .children
            .iter()
            .filter(|run_child| run_child.exit.is_none())
        {
            // Sound: the pid belongs to a child this wrapper spawned and has not reaped.
            let outcome = unsafe { libc::kill(run_child.child.id() as libc::pid_t, libc::SIGTERM) };
            if outcome != 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(error);
                }
            }
        }
        Ok(())
    }
}

/// Spawn ffmpeg and, when the command has one, the relay reading ffmpeg's standard output.
fn spawn_pipeline(command: &StreamCommand, capture: &File) -> io::Result<Vec<RunChild>> {
    let ffmpeg_output = match command.relay {
        Some(_) => Stdio::piped(),
        None => Stdio::from(capture.try_clone()?),
    };
    let mut ffmpeg = spawn_child(&command.argv, Stdio::null(), ffmpeg_output, capture)?;
    let Some(relay_argv) = &command.relay else {
        return Ok(vec![RunChild {
            child: ffmpeg,
            exit: None,
        }]);
    };
    let relay = ffmpeg
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("ffmpeg has no output pipe"))
        .and_then(|pipe| {
            spawn_child(
                relay_argv,
                Stdio::from(pipe),
                Stdio::from(capture.try_clone()?),
                capture,
            )
        });
    match relay {
        Ok(relay) => Ok(vec![
            RunChild {
                child: ffmpeg,
                exit: None,
            },
            RunChild {
                child: relay,
                exit: None,
            },
        ]),
        Err(error) => {
            let _ = ffmpeg.kill();
            let _ = ffmpeg.wait();
            Err(error)
        }
    }
}

fn spawn_child(argv: &[String], input: Stdio, output: Stdio, capture: &File) -> io::Result<Child> {
    let (program, arguments) = argv
        .split_first()
        .ok_or_else(|| io::Error::other("empty command"))?;
    Command::new(program)
        .args(arguments)
        .stdin(input)
        .stdout(output)
        .stderr(Stdio::from(capture.try_clone()?))
        .spawn()
}

fn returncode(status: ExitStatus) -> i32 {
    status
        .code()
        .or_else(|| status.signal().map(|signal| -signal))
        .unwrap_or(-1)
}

impl StreamProcess for FFmpegProcess {
    fn start(&mut self, command: &StreamCommand) -> io::Result<()> {
        if !self.command.is_empty() {
            return Err(io::Error::other("A process wrapper owns only one run"));
        }
        self.command = command.clone();
        let capture = tempfile::tempfile()?;
        match spawn_pipeline(command, &capture) {
            Ok(children) => {
                self.children = children;
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
            command: self.command.clone(),
            running_for: self
                .started_at
                .map(|started| started.elapsed())
                .unwrap_or_default(),
            diagnostics: String::from_utf8_lossy(&self.tail).into_owned(),
        }
    }

    fn stop(&mut self) -> bool {
        if self.children.is_empty() {
            return true;
        }
        if self.poll().is_none() {
            if self.send_term().is_err() {
                return false;
            }
            if !self.wait_until(Instant::now() + Self::STOP_TIMEOUT) {
                for run_child in self
                    .children
                    .iter_mut()
                    .filter(|run_child| run_child.exit.is_none())
                {
                    if let Err(error) = run_child.child.kill()
                        && error.kind() != io::ErrorKind::InvalidInput
                    {
                        return false;
                    }
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

    fn command(parts: &[&str]) -> StreamCommand {
        StreamCommand {
            argv: argv(parts),
            relay: None,
        }
    }

    fn script(text: &str) -> StreamCommand {
        command(&["sh", "-c", text])
    }

    fn relayed(ffmpeg: &str, relay: &str) -> StreamCommand {
        StreamCommand {
            relay: Some(argv(&["sh", "-c", relay])),
            ..script(ffmpeg)
        }
    }

    fn run_until_exit(
        logger: Arc<StreamLogger>,
        command: &StreamCommand,
    ) -> (FFmpegProcess, ProcessObservation) {
        let mut run = FFmpegProcess::new(logger);
        run.start(command).unwrap();
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
            &script(
                "head -c 1000000 /dev/zero | tr '\\0' x; echo; echo stdout-tail; echo stderr-tail >&2",
            ),
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
        let (run, result) =
            run_until_exit(logger, &script("head -c 100000 /dev/zero | tr '\\0' x"));
        assert_eq!(result.returncode, Some(0));
        assert!(result.diagnostics.starts_with("Diagnostic capture failed"));
        assert!(!run.captures());
    }

    #[test]
    fn spawn_failure_closes_capture_and_propagates() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        assert!(run.start(&command(&["/nonexistent/ffmpeg"])).is_err());
        assert!(!run.captures());
        assert!(run.stop());
        let mut relayed_run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        let missing_relay = StreamCommand {
            relay: Some(argv(&["/nonexistent/gst-launch-1.0"])),
            ..script("exec sleep 30")
        };
        assert!(relayed_run.start(&missing_relay).is_err());
        assert!(!relayed_run.captures());
        assert!(relayed_run.stop());
    }

    #[test]
    fn relay_reads_ffmpeg_output_and_drains_after_a_clean_exit() {
        let (_, result) = run_until_exit(
            Arc::new(StreamLogger::discard("test")),
            &relayed(
                "echo transport-stream",
                "sleep 1; tr a-z A-Z; echo relay-done",
            ),
        );
        assert_eq!(result.returncode, Some(0));
        assert!(result.diagnostics.contains("TRANSPORT-STREAM"));
        assert!(result.diagnostics.contains("relay-done"));
        assert_eq!(
            result.command.shell_text(),
            "sh -c 'echo transport-stream' | sh -c 'sleep 1; tr a-z A-Z; echo relay-done'"
        );
    }

    #[test]
    fn relay_failure_ends_the_run_with_its_code() {
        let (_, result) = run_until_exit(
            Arc::new(StreamLogger::discard("test")),
            &relayed("exec yes", "echo relay-refused >&2; exit 3"),
        );
        assert_eq!(result.returncode, Some(3));
        assert!(result.diagnostics.contains("relay-refused"));
    }

    #[test]
    fn stop_terminates_ffmpeg_and_relay() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        run.start(&relayed("exec sleep 30", "exec sleep 30"))
            .unwrap();
        assert!(run.observe().returncode.is_none());
        assert!(run.stop());
        assert_eq!(run.observe().returncode, Some(-libc::SIGTERM));
        assert!(!run.captures());
    }

    #[test]
    fn terminates_child_and_owns_only_one_run() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        run.start(&script("exec sleep 30")).unwrap();
        assert!(run.observe().returncode.is_none());
        assert!(run.stop());
        assert_eq!(run.observe().returncode, Some(-libc::SIGTERM));
        assert!(!run.captures());
        assert!(run.start(&command(&["unused"])).is_err());
    }

    #[test]
    fn kill_escalation_when_term_is_ignored() {
        let mut run = FFmpegProcess::new(Arc::new(StreamLogger::discard("test")));
        run.start(&script("trap '' TERM; while true; do sleep 1; done"))
            .unwrap();
        thread::sleep(Duration::from_millis(100));
        let started = Instant::now();
        assert!(run.stop());
        assert!(started.elapsed() >= FFmpegProcess::STOP_TIMEOUT);
        assert_eq!(run.observe().returncode, Some(-libc::SIGKILL));
        assert!(!run.captures());
    }
}
