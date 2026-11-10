//! Run a child command to completion with a deadline, capturing its output.
//!
//! The standard library waits without a timeout; this polls the child and
//! kills it when the deadline passes, as Python's `subprocess.run(timeout=...)`
//! did. Output is drained on separate threads so a chatty child never blocks.

use std::fmt;
use std::io::{self, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub struct CommandOutput {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

#[derive(Debug)]
pub enum CommandFailure {
    Spawn(io::Error),
    TimedOut {
        argv: Vec<String>,
        timeout: Duration,
    },
}

impl fmt::Display for CommandFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CommandFailure::Spawn(error) => write!(formatter, "{error}"),
            CommandFailure::TimedOut { argv, timeout } => write!(
                formatter,
                "Command {argv:?} timed out after {} seconds",
                timeout.as_secs_f64()
            ),
        }
    }
}

impl std::error::Error for CommandFailure {}

/// Run `argv` with stdin closed, capturing stdout, and stderr as `stderr` says.
pub fn run_bounded(
    argv: &[String],
    timeout: Duration,
    stderr: Stdio,
) -> Result<CommandOutput, CommandFailure> {
    let (program, arguments) = argv
        .split_first()
        .ok_or_else(|| CommandFailure::Spawn(io::Error::other("empty command")))?;
    let mut child = Command::new(program)
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .spawn()
        .map_err(CommandFailure::Spawn)?;
    let stdout_reader = drain(child.stdout.take());
    let stderr_reader = drain(child.stderr.take());
    let status = wait_until(&mut child, Instant::now() + timeout);
    let stdout = stdout_reader.join().unwrap_or_default();
    let stderr = stderr_reader.join().unwrap_or_default();
    match status {
        Some(status) => Ok(CommandOutput {
            status,
            stdout,
            stderr,
        }),
        None => Err(CommandFailure::TimedOut {
            argv: argv.to_vec(),
            timeout,
        }),
    }
}

fn drain<R: Read + Send + 'static>(pipe: Option<R>) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut captured = Vec::new();
        if let Some(mut pipe) = pipe {
            let _ = pipe.read_to_end(&mut captured);
        }
        captured
    })
}

/// Poll the child until it exits or the deadline passes; a late child is killed and reaped.
fn wait_until(child: &mut Child, deadline: Instant) -> Option<ExitStatus> {
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) => {}
            Err(_) => return None,
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|part| part.to_string()).collect()
    }

    #[test]
    fn captures_output_and_exit_status() {
        let output = run_bounded(
            &argv(&["sh", "-c", "echo out; echo err >&2; exit 3"]),
            Duration::from_secs(5),
            Stdio::piped(),
        )
        .unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
    }

    #[test]
    fn kills_a_child_that_outlives_the_deadline() {
        let started = Instant::now();
        let failure = run_bounded(
            &argv(&["sh", "-c", "exec sleep 30"]),
            Duration::from_millis(100),
            Stdio::null(),
        )
        .err()
        .expect("the sleep should time out");
        assert!(matches!(failure, CommandFailure::TimedOut { .. }));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(failure.to_string().contains("timed out after 0.1 seconds"));
    }

    #[test]
    fn a_missing_program_is_a_spawn_failure() {
        let failure = run_bounded(
            &argv(&["/nonexistent/program"]),
            Duration::from_secs(1),
            Stdio::null(),
        )
        .err()
        .expect("spawn should fail");
        assert!(matches!(failure, CommandFailure::Spawn(_)));
    }
}
