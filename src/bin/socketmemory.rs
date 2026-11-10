//! Capture host slab usage and socket queues in every running container.
//!
//! A host diagnostic, not part of the service or its image. It needs a Linux
//! host with procfs and a Docker CLI that can reach the daemon. Each sample is
//! one JSON line appended to a file that rolls over at 16 MiB and keeps four
//! old files.

use std::env;
use std::fs;
use std::io;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{self, ExitStatus, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use autostream::boundedcommand::run_bounded;
use autostream::clocktext::iso_timestamp;
use autostream::rotatingfile::RotatingFile;
use autostream::stopsignal::StopSignal;
use serde_json::{Map, Value, json};

const DESCRIPTION: &str = "Capture host slab usage and socket queues in every running container.";
const USAGE: &str = "usage: socketmemory [-h] [--interval SECONDS] [--output PATH] [--once]";
const DEFAULT_INTERVAL_SECONDS: f64 = 60.0;
const DEFAULT_OUTPUT: &str = ".generated/socket-memory.jsonl";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(20);
const SNAPSHOT_WORKERS: usize = 4;
// Bound disk usage to approximately 80 MiB, including four old files.
const OUTPUT_MAX_BYTES: u64 = 16 * 1024 * 1024;
const OUTPUT_BACKUPS: u32 = 4;

// Docker exec as root can read slab counters and socket tables without sudo or
// extra container capabilities.
const CONTAINER_SNAPSHOT: &str = include_str!("socketmemory.sh");

#[derive(Debug, PartialEq)]
struct Options {
    interval: Duration,
    output: PathBuf,
    once: bool,
}

#[derive(Debug, PartialEq)]
enum Invocation {
    Collect(Options),
    ShowHelp,
}

fn main() {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let options = match parse_arguments(&arguments) {
        Ok(Invocation::Collect(options)) => options,
        Ok(Invocation::ShowHelp) => {
            println!("{}", help_text());
            return;
        }
        Err(message) => {
            eprintln!("{USAGE}\nsocketmemory: error: {message}");
            process::exit(2);
        }
    };
    if let Err(message) = collect(&options) {
        eprintln!("socketmemory: error: {message}");
        process::exit(1);
    }
}

fn help_text() -> String {
    format!(
        "{USAGE}\n\n{DESCRIPTION}\n\noptions:\n  \
         -h, --help          show this help message and exit\n  \
         --interval SECONDS  seconds between samples (default 60)\n  \
         --output PATH       JSONL file to append to (default {DEFAULT_OUTPUT})\n  \
         --once              take one sample and exit"
    )
}

/// Parse the command line as the Python argparse definition did.
fn parse_arguments(arguments: &[String]) -> Result<Invocation, String> {
    let mut interval_seconds = DEFAULT_INTERVAL_SECONDS;
    let mut output = PathBuf::from(DEFAULT_OUTPUT);
    let mut once = false;
    let mut remaining = arguments.iter();
    while let Some(argument) = remaining.next() {
        let (name, inline_value) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (argument.as_str(), None),
        };
        match (name, inline_value) {
            ("-h" | "--help", None) => return Ok(Invocation::ShowHelp),
            ("--once", None) => once = true,
            ("--interval", inline_value) => {
                let text = option_value(name, inline_value, &mut remaining)?;
                interval_seconds = text
                    .trim()
                    .parse()
                    .map_err(|_| format!("argument --interval: invalid float value: '{text}'"))?;
            }
            ("--output", inline_value) => {
                output = PathBuf::from(option_value(name, inline_value, &mut remaining)?);
            }
            _ => return Err(format!("unrecognized arguments: {argument}")),
        }
    }
    if interval_seconds.is_nan() || interval_seconds <= 0.0 {
        return Err("--interval must be positive".to_string());
    }
    let interval = Duration::try_from_secs_f64(interval_seconds)
        .map_err(|_| "--interval must be a finite number of seconds".to_string())?;
    Ok(Invocation::Collect(Options {
        interval,
        output,
        once,
    }))
}

/// The value given after `=` or as the next argument, which must not be another option.
fn option_value<'a>(
    name: &str,
    inline_value: Option<String>,
    remaining: &mut impl Iterator<Item = &'a String>,
) -> Result<String, String> {
    if let Some(value) = inline_value {
        return Ok(value);
    }
    match remaining.next() {
        Some(value) if !value.starts_with("--") => Ok(value.clone()),
        _ => Err(format!("argument {name}: expected one argument")),
    }
}

/// Append one sample per interval until `--once` finishes or Ctrl-C interrupts.
fn collect(options: &Options) -> Result<(), String> {
    let interrupted = stop_on_interrupt();
    let output_path = &options.output;
    if let Some(directory) = output_path.parent() {
        fs::create_dir_all(directory).map_err(|error| describe(directory, &error))?;
    }
    let mut output = RotatingFile::open(output_path, OUTPUT_MAX_BYTES, OUTPUT_BACKUPS)
        .map_err(|error| describe(output_path, &error))?;
    loop {
        let started = Instant::now();
        let sample = snapshot().to_string();
        // An interrupted sample is dropped, as Python's KeyboardInterrupt dropped it.
        if interrupted.is_set() {
            break;
        }
        if let Err(error) = output.write_line(&sample) {
            eprintln!("socketmemory: {}", describe(output_path, &error));
        }
        if options.once || interrupted.wait(options.interval.saturating_sub(started.elapsed())) {
            break;
        }
    }
    Ok(())
}

/// Turn Ctrl-C into a quiet stop instead of killing the process mid-write.
///
/// SIGINT is blocked before any other thread starts, so every thread inherits
/// the block and only the waiting thread receives it. Child commands start with
/// an empty signal mask, so Ctrl-C still reaches them through the terminal.
fn stop_on_interrupt() -> StopSignal {
    let interrupted = StopSignal::new();
    // This is sound because the signal set is initialized by sigemptyset before any other use,
    // and pthread_sigmask changes only the calling thread.
    let blocked = unsafe {
        let mut signals: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut signals);
        libc::sigaddset(&mut signals, libc::SIGINT);
        if libc::pthread_sigmask(libc::SIG_BLOCK, &signals, std::ptr::null_mut()) != 0 {
            return interrupted;
        }
        signals
    };
    let setter = interrupted.clone();
    thread::spawn(move || {
        let mut received = 0;
        // This is sound because the set is initialized and both pointers are valid for the call.
        if unsafe { libc::sigwait(&blocked, &mut received) } == 0 {
            setter.set();
        }
    });
    interrupted
}

/// One sample of host slab usage, host sockets, and every running container.
fn snapshot() -> Value {
    let listing = command(&["docker", "ps", "--format", "{{.Names}}"]);
    let names = container_names(&listing);
    let mut slab = read_text("/proc/slabinfo");
    if slab.starts_with("unavailable:")
        && let Some(first) = names.first()
    {
        // These counters are host-wide even when read from a container.
        let fallback = command(&["docker", "exec", "-u", "0", first, "cat", "/proc/slabinfo"]);
        slab = fallback_slab_text(&fallback);
    }
    let buffers = skbuff_buffers(&slab, page_size());
    let slab_error = if buffers.is_empty() {
        Value::String(slab)
    } else {
        Value::Null
    };
    let containers = snapshot_containers(&names);
    json!({
        "time": iso_timestamp(SystemTime::now()),
        "host_meminfo": read_text("/proc/meminfo"),
        "skbuff": buffers,
        "slab_error": slab_error,
        "host_sockstat": read_text("/proc/net/sockstat"),
        "host_sockstat6": read_text("/proc/net/sockstat6"),
        "host_sockets": command(&["ss", "-tunapme"]),
        "docker_listing": listing,
        "containers": containers,
    })
}

/// Snapshot containers on at most four threads, keeping the `docker ps` order.
fn snapshot_containers(names: &[String]) -> Map<String, Value> {
    let next_index = AtomicUsize::new(0);
    let mut finished: Vec<(usize, Value)> = thread::scope(|scope| {
        let workers: Vec<_> = (0..SNAPSHOT_WORKERS.min(names.len()))
            .map(|_| {
                scope.spawn(|| {
                    let mut snapshots = Vec::new();
                    loop {
                        let index = next_index.fetch_add(1, Ordering::Relaxed);
                        let Some(name) = names.get(index) else {
                            return snapshots;
                        };
                        snapshots.push((index, snapshot_container(name)));
                    }
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|worker| worker.join().expect("a container snapshot worker panicked"))
            .collect()
    });
    finished.sort_by_key(|(index, _)| *index);
    // Keyed by container name because the running containers vary between samples.
    finished
        .into_iter()
        .map(|(index, snapshot)| (names[index].clone(), snapshot))
        .collect()
}

fn snapshot_container(name: &str) -> Value {
    let mut snapshot = command(&[
        "docker",
        "exec",
        "-u",
        "0",
        name,
        "sh",
        "-c",
        CONTAINER_SNAPSHOT,
    ]);
    let processes = command(&["docker", "top", name, "-eo", "pid,comm"]);
    let owners = socket_owners(&processes, Path::new("/proc"));
    snapshot["host_processes"] = processes;
    snapshot["host_socket_owners"] = Value::Object(owners);
    snapshot
}

/// Run a command with the 20 second limit, shaped as the Python `command()` result.
fn command(argv: &[&str]) -> Value {
    let argv: Vec<String> = argv.iter().map(|part| part.to_string()).collect();
    match run_bounded(&argv, COMMAND_TIMEOUT, Stdio::piped()) {
        Ok(output) => json!({
            "exit": exit_code(output.status),
            "output": String::from_utf8_lossy(&output.stdout),
            "error": String::from_utf8_lossy(&output.stderr),
        }),
        Err(failure) => json!({ "error": failure.to_string() }),
    }
}

/// The exit code, or the negated signal number as Python's `returncode` reported it.
fn exit_code(status: ExitStatus) -> i32 {
    status
        .code()
        .unwrap_or_else(|| -status.signal().unwrap_or(0))
}

fn read_text(path: &str) -> String {
    fs::read_to_string(path)
        .unwrap_or_else(|error| format!("unavailable: {}", describe(Path::new(path), &error)))
}

fn describe(path: &Path, error: &io::Error) -> String {
    format!("{}: {error}", path.display())
}

fn page_size() -> u64 {
    // This is sound because sysconf only reads a system constant.
    let size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    u64::try_from(size).expect("sysconf reports the page size")
}

/// Container names from a successful `docker ps` listing, or none.
fn container_names(listing: &Value) -> Vec<String> {
    if listing.get("exit") != Some(&json!(0)) {
        return Vec::new();
    }
    let output = listing.get("output").and_then(Value::as_str).unwrap_or("");
    output.lines().map(str::to_string).collect()
}

/// The slab text from a container fallback: its output, else its error text.
fn fallback_slab_text(result: &Value) -> String {
    match result.get("output").and_then(Value::as_str) {
        Some(output) if !output.is_empty() => output.to_string(),
        _ => result
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unavailable")
            .to_string(),
    }
}

/// Socket buffer caches from `/proc/slabinfo` text.
fn skbuff_buffers(slab: &str, page_size: u64) -> Map<String, Value> {
    // Keyed by cache name because the set of skbuff caches varies by kernel.
    let mut buffers = Map::new();
    for line in slab.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() < 15 || !fields[0].starts_with("skbuff") {
            continue;
        }
        let counter = |index: usize| fields[index].parse::<u64>().ok();
        let (Some(active_objects), Some(allocated_objects), Some(object_bytes)) =
            (counter(1), counter(2), counter(3))
        else {
            continue;
        };
        let (Some(pages_per_slab), Some(slab_count)) = (counter(5), counter(14)) else {
            continue;
        };
        buffers.insert(
            fields[0].to_string(),
            json!({
                "active_objects": active_objects,
                "allocated_objects": allocated_objects,
                "object_bytes": object_bytes,
                "allocated_slab_bytes": slab_count * pages_per_slab * page_size,
            }),
        );
    }
    buffers
}

/// Host PIDs let us read same-user fds even if container root cannot.
fn socket_owners(processes: &Value, proc_root: &Path) -> Map<String, Value> {
    let output = processes
        .get("output")
        .and_then(Value::as_str)
        .unwrap_or("");
    // Keyed by host PID because docker top lists a different set of processes each time.
    host_pids(output)
        .into_iter()
        .map(|pid| {
            let owner = process_sockets(&proc_root.join(pid).join("fd"));
            (pid.to_string(), owner)
        })
        .collect()
}

/// Numeric first fields of `docker top` output after its header line.
fn host_pids(output: &str) -> Vec<&str> {
    output
        .lines()
        .skip(1)
        .filter_map(|line| line.split_whitespace().next())
        .filter(|field| field.bytes().all(|byte| byte.is_ascii_digit()))
        .collect()
}

/// Socket links in one process's fd directory, with any permission error.
fn process_sockets(fd_directory: &Path) -> Value {
    let entries = match fs::read_dir(fd_directory) {
        Ok(entries) => entries,
        Err(error) => return json!({ "error": describe(fd_directory, &error) }),
    };
    // Keyed by fd number because each process holds a different set of descriptors.
    let mut sockets = Map::new();
    let mut access_error = None;
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => return json!({ "error": describe(fd_directory, &error) }),
        };
        let link = entry.path();
        match fs::read_link(&link) {
            Ok(target) => {
                let target = target.to_string_lossy();
                if target.starts_with("socket:") {
                    let fd = entry.file_name().to_string_lossy().into_owned();
                    sockets.insert(fd, Value::String(target.into_owned()));
                }
            }
            Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                access_error = Some(describe(&link, &error));
            }
            // The fd closed during the sample, or the entry is not a link.
            Err(_) => {}
        }
    }
    let mut owner = json!({ "sockets": sockets });
    if let Some(error) = access_error {
        owner["error"] = Value::String(error);
    }
    owner
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn arguments(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|part| part.to_string()).collect()
    }

    #[test]
    fn defaults_collect_every_minute_into_the_generated_file() {
        assert_eq!(
            parse_arguments(&[]),
            Ok(Invocation::Collect(Options {
                interval: Duration::from_secs(60),
                output: PathBuf::from(".generated/socket-memory.jsonl"),
                once: false,
            }))
        );
    }

    #[test]
    fn once_interval_and_output_are_read() {
        assert_eq!(
            parse_arguments(&arguments(&[
                "--once",
                "--interval",
                "2.5",
                "--output=/tmp/out.jsonl"
            ])),
            Ok(Invocation::Collect(Options {
                interval: Duration::from_millis(2500),
                output: PathBuf::from("/tmp/out.jsonl"),
                once: true,
            }))
        );
        assert_eq!(
            parse_arguments(&arguments(&["--help"])),
            Ok(Invocation::ShowHelp)
        );
    }

    #[test]
    fn bad_arguments_are_rejected() {
        let positive = Err("--interval must be positive".to_string());
        assert_eq!(parse_arguments(&arguments(&["--interval", "0"])), positive);
        assert_eq!(parse_arguments(&arguments(&["--interval", "-5"])), positive);
        assert_eq!(
            parse_arguments(&arguments(&["--interval", "soon"])),
            Err("argument --interval: invalid float value: 'soon'".to_string())
        );
        assert_eq!(
            parse_arguments(&arguments(&["--interval"])),
            Err("argument --interval: expected one argument".to_string())
        );
        assert_eq!(
            parse_arguments(&arguments(&["--verbose"])),
            Err("unrecognized arguments: --verbose".to_string())
        );
    }

    #[test]
    fn docker_top_pids_skip_the_header_and_non_numeric_fields() {
        let output = "PID   COMMAND\n123 mediamtx\nworker 9\n\n  456 ffmpeg\n";
        assert_eq!(host_pids(output), vec!["123", "456"]);
        assert_eq!(host_pids("100 header only\n"), Vec::<&str>::new());
    }

    #[test]
    fn socket_owners_collect_socket_links_by_fd_name() {
        let proc_root = tempfile::tempdir().unwrap();
        let fd_directory = proc_root.path().join("123").join("fd");
        fs::create_dir_all(&fd_directory).unwrap();
        symlink("socket:[12345]", fd_directory.join("3")).unwrap();
        symlink("pipe:[777]", fd_directory.join("4")).unwrap();
        symlink("/dev/null", fd_directory.join("5")).unwrap();
        fs::write(fd_directory.join("6"), "not a link").unwrap();
        let processes = json!({
            "exit": 0,
            "output": "PID COMMAND\n123 mediamtx\n456 gone\n",
            "error": "",
        });

        let owners = socket_owners(&processes, proc_root.path());

        assert_eq!(owners.keys().collect::<Vec<_>>(), vec!["123", "456"]);
        assert_eq!(
            owners["123"],
            json!({ "sockets": { "3": "socket:[12345]" } })
        );
        let missing = owners["456"].as_object().unwrap();
        assert_eq!(missing.keys().collect::<Vec<_>>(), vec!["error"]);
        assert!(
            missing["error"]
                .as_str()
                .unwrap()
                .contains("No such file or directory")
        );
    }

    #[test]
    fn a_failed_docker_top_has_no_owners() {
        let failure = json!({ "error": "Command timed out" });
        assert!(socket_owners(&failure, Path::new("/nonexistent")).is_empty());
    }

    #[test]
    fn skbuff_caches_are_read_from_slabinfo() {
        let slab = "\
slabinfo - version: 2.1
# name            <active_objs> <num_objs> <objsize> <objperslab> <pagesperslab> : tunables <limit> <batchcount> <sharedfactor> : slabdata <active_slabs> <num_slabs> <sharedavail>
kmalloc-256         4096   4096    256   32    2 : tunables    0    0    0 : slabdata    128    128      0
skbuff_head_cache   1200   1536    256   32    2 : tunables    0    0    0 : slabdata     48     48      0
skbuff_fclone_cache   90    128    512   32    4 : tunables    0    0    0 : slabdata      4      5      0
skbuff_short          10
";
        let buffers = skbuff_buffers(slab, 4096);
        assert_eq!(
            Value::Object(buffers),
            json!({
                "skbuff_head_cache": {
                    "active_objects": 1200,
                    "allocated_objects": 1536,
                    "object_bytes": 256,
                    "allocated_slab_bytes": 48 * 2 * 4096,
                },
                "skbuff_fclone_cache": {
                    "active_objects": 90,
                    "allocated_objects": 128,
                    "object_bytes": 512,
                    "allocated_slab_bytes": 5 * 4 * 4096,
                },
            })
        );
        assert!(skbuff_buffers("unavailable: denied", 4096).is_empty());
    }
}
