//! Autostream: the stream supervisor and its HTTP API.
//!
//! The library holds every testable module; the binaries under `src/bin`
//! compose them into the service and the host helpers.

pub mod appconfig;
pub mod boundedcommand;
pub mod clocktext;
pub mod ffmpegcmd;
pub mod ffmpegprocess;
pub mod filewatch;
pub mod mediastream;
pub mod queryform;
pub mod rotatingfile;
pub mod runtimesettings;
pub mod statefile;
pub mod stopsignal;
pub mod streamapi;
pub mod streamlog;
pub mod streamrecord;
pub mod streamregistry;
pub mod streamresponses;
pub mod streamsupervisor;
pub mod transports;
pub mod udphost;

#[cfg(test)]
pub mod testsupport;
