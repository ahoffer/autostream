//! Logging: one-line stderr records for the service, and a rotating file per stream for ffmpeg output.

use std::io::{self, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::SystemTime;

use log::{Level, LevelFilter, Log, Metadata, Record};

use crate::clocktext::clock_text;
use crate::rotatingfile::RotatingFile;

/// Per-stream ffmpeg output: appended to `<name>.log` (2 MB, one backup) and,
/// when the service runs at debug level, echoed into the service log.
pub struct StreamLogger {
    name: String,
    file: Option<Mutex<RotatingFile>>,
    echo: bool,
}

impl StreamLogger {
    pub const MAX_BYTES: u64 = 2_000_000;
    pub const BACKUPS: u32 = 1;

    /// The stream's log file under the ffmpeg log directory, created on demand.
    pub fn open(name: &str, directory: &Path, echo: bool) -> io::Result<Self> {
        std::fs::create_dir_all(directory)?;
        Self::at_path(name, &directory.join(format!("{name}.log")), echo)
    }

    pub fn at_path(name: &str, path: &Path, echo: bool) -> io::Result<Self> {
        Ok(Self {
            name: name.to_string(),
            file: Some(Mutex::new(RotatingFile::open(
                path,
                Self::MAX_BYTES,
                Self::BACKUPS,
            )?)),
            echo,
        })
    }

    /// A logger that keeps nothing, for tests.
    pub fn discard(name: &str) -> Self {
        Self {
            name: name.to_string(),
            file: None,
            echo: false,
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn info(&self, message: &str) -> io::Result<()> {
        self.write(Level::Info, message)
    }

    pub fn warning(&self, message: &str) -> io::Result<()> {
        self.write(Level::Warn, message)
    }

    fn write(&self, level: Level, message: &str) -> io::Result<()> {
        if let Some(file) = &self.file {
            file.lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .write_line(message)?;
        }
        if self.echo {
            log::log!(target: "ffmpeg", level, "{message}");
        }
        Ok(())
    }
}

/// `HH:MM:SS LEVEL message` on stderr, one line per record.
struct OneLineLogger {
    level: LevelFilter,
}

impl Log for OneLineLogger {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        metadata.level() <= self.level
    }

    fn log(&self, record: &Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let level = match record.level() {
            Level::Error => "ERROR",
            Level::Warn => "WARN",
            Level::Info => "INFO",
            Level::Debug | Level::Trace => "DEBUG",
        };
        let message = record.args().to_string().replace('\n', "; ");
        let line = format!("{} {level} {message}\n", clock_text(SystemTime::now()));
        let _ = io::stderr().lock().write_all(line.as_bytes());
    }

    fn flush(&self) {}
}

/// Install the service logger at the configured level name (debug, info, warning or error).
pub fn configure_logging(log_level: &str) {
    let level = match log_level {
        "debug" => LevelFilter::Debug,
        "warning" => LevelFilter::Warn,
        "error" => LevelFilter::Error,
        _ => LevelFilter::Info,
    };
    if log::set_boxed_logger(Box::new(OneLineLogger { level })).is_ok() {
        log::set_max_level(level);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_logger_writes_the_message_as_given() {
        let directory = tempfile::tempdir().unwrap();
        let logger = StreamLogger::open("boats", &directory.path().join("logs"), false).unwrap();
        logger.info("frame=1\nframe=2").unwrap();
        logger.warning("late").unwrap();
        let text = std::fs::read_to_string(directory.path().join("logs/boats.log")).unwrap();
        assert_eq!(text, "frame=1\nframe=2\nlate\n");
        assert_eq!(logger.name(), "boats");
        StreamLogger::discard("quiet").info("dropped").unwrap();
    }

    #[test]
    fn a_full_device_reports_the_write_failure() {
        if !Path::new("/dev/full").exists() {
            return;
        }
        let logger = StreamLogger::at_path("boats", Path::new("/dev/full"), false).unwrap();
        assert!(logger.info("x").is_err());
    }
}
