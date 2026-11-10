# Rust and Cargo migration plan

Status: planning only. Branch `rust` starts at `e403838`; no application or
deployment changes have been made for this plan.

## Scope and baseline

Convert all maintained Python code to Rust: the 13 application modules in
`src/`, their tests and support code, `scripts/compose_up.py` and its tests,
and the host diagnostic `socket_memory.py`. Preserve the existing HTML/JS UI,
font, FFmpeg/ffprobe integration, MediaMTX, and Compose networking modes.

The application currently uses Python's standard library only. There is no
pip, Poetry, uv, requirements file, or Python package manifest to replace.
Packaging currently means copying source into a Docker image, with Make
wrapping build, container tests, and deployment. Cargo will own Rust dependency
resolution, compilation, tests, and source packaging. Docker will continue to
package FFmpeg, MediaMTX, and the application together for deployment.

Verified on 2026-11-10:

- Clean `master` checkout before creating and switching to `rust`.
- Python 3.14.4; rustc and Cargo 1.92.0 installed locally.
- Application baseline: 83 tests pass with
  `PYTHONPATH=src python3 -B -m unittest discover -s tests -v`.
- Deployment helper baseline: 4 tests pass with
  `python3 -B -m unittest discover -s scripts -p 'test_*.py' -v`.
- Dockerfile pins MediaMTX 1.21.2 and rebuilds it with Go/networking fixes.
  Preserve that build stage and source checksum verification.
- No container build, live playback, or deployment was performed for this plan.

## Recommended design

Use one Cargo package with a library for testable application modules and
three binary targets: the service, Compose startup helper, and socket-memory
diagnostic. Keep helper implementations under separate modules so the service
does not include their code. Keep existing domain terminology and serialized
names; settle new executable names when implementing the CLI surface.

| Organization | Tradeoff | Decision |
| --- | --- | --- |
| One package, library plus binary targets | Minimal configuration and one dependency lock; sufficient for this small application | Recommended |
| Workspace with separate service and tools packages | Cleaner independent dependency graphs and releases, but more manifests and boundaries | Revisit if tools grow or need separate releases |

Use Tokio and Axum for HTTP and shutdown coordination, Serde/serde_json for
typed JSON, and insertion-ordered maps where stream order is observable.
Keep the stream supervisor on one dedicated thread using a bounded standard
library channel of capacity 128. HTTP handlers use nonblocking enqueue and
await a one-shot response; the watcher submits detached observations to the
same channel. A short-held lock protects an immutable, reference-counted
status snapshot. Only the supervisor owns mutable stream state, processes,
port allocation, and persistence.

This preserves the current ownership model while keeping blocking ffprobe,
filesystem, DNS, and process-stop operations off HTTP runtime workers. Preserve
bounded command batches and supervision ticks so sustained requests cannot
starve recovery. Do not introduce one independently mutating task per stream.
Do not add request timeouts or automatic mutation retries during the port:
an accepted operation must not silently disappear when its client disconnects.

Use standard process APIs for FFmpeg/ffprobe. Select small, maintained crates
for temporary files, Unix signals, random port selection, and logging only
where needed. Retain file-backed diagnostics and size-based log rotation.
Select exact dependency versions during implementation, check compiler support,
and commit Cargo.lock. Start with the installed Rust 1.92.0 toolchain pinned
in `rust-toolchain.toml`; change the pin only if a verified requirement warrants
it. Set the supported compiler version and edition in Cargo.toml.

Cargo's [manifest and lockfile guidance](https://doc.rust-lang.org/cargo/guide/cargo-toml-vs-cargo-lock.html)
supports tracking both dependency intent and resolved versions. Its
[workspace documentation](https://doc.rust-lang.org/cargo/reference/workspaces.html)
describes the alternative if separate packages become useful.
[Axum's documentation](https://docs.rs/axum/latest/axum/)
documents routing, shared state, and its Tokio integration.

## Compatibility requirements

- Preserve all routes, POST query parameters, response fields, transport IDs,
  settings names, blank/default/`auto` behavior, and relevant error messages.
  Explicitly preserve 400, 404, 409, 500, and queue-full 503 responses; framework
  defaults must not silently change validation to different status codes.
- Keep `mounted/streams_config.json` readable and writable by both versions.
  Preserve optional values, defaults, stream creation order, rename position,
  enabled choices, and ports. Load without rewriting; write a temporary file
  beside the destination and atomically replace it before updating the cache.
  Failed replacement must preserve the old file and cached document.
- Missing state enables first-start seeding after videos stabilize. Existing
  empty state stays empty. The first successful seed batch or manually created
  stream ends seeding. Correct the stale empty-first-boot instruction in
  `docs/regression.md` to match current code and tests.
- Preserve all lifecycle states, finite-loop completion, crash recovery,
  settings-driven selective restarts, and refusal to replace or delete a stream
  whose process cannot stop. An actual Stop still takes effect if saving fails;
  shutdown must not persist disabled choices.
- Keep the two-second directory poll and four-second stability check. Preserve
  nanosecond modification times, deterministic ordering, and the distinction
  between an unsuccessful scan and deleted files. Continue polling for network
  filesystems rather than relying exclusively on native filesystem events.
- Preserve lowest-free-port allocation, multicast's 50000–59999 range, DNS
  waiting/recovery, and per-request advertised-host URL construction.
- Preserve argument arrays and the displayed FFmpeg command. Keep software
  libx264, codec fallback, AAC handling, FPS/GOP/bitrate, and looping behavior.
- Keep MPEG-TS/SRT ingest into MediaMTX on loopback, RTSP KLV delivery, and
  video/audio-only MediaMTX HLS/RTMP. Retain `-map 0 -copy_unknown -c:d copy`
  where applicable and omit the data timestamp clamp only on local SRT ingest.
- Preserve HLS path containment, allowed extensions, content types, and cache
  headers. Serve the existing UI and font offline; embedding these two assets
  in the service binary is recommended. HLS output remains runtime filesystem
  data, not an embedded asset.
- Preserve UID/GID 1000, volume paths, environment variables, listener ports,
  memory limits, and both Compose network configurations.

## Implementation sequence

Each phase should be a reviewable change with its own validation. Keep the
Python implementation runnable until the Rust replacement passes parity checks.

### 1. Capture contracts and introduce Cargo

Add Cargo.toml, Cargo.lock, the toolchain pin, library/binary entry points,
and `/target/` ignores for Git and Docker. Do not change the deployed entrypoint
yet. Use a temporary side-by-side Rust implementation in the existing tree;
Python and Rust files can coexist in `src/` during migration.

Capture representative state documents, API responses, ffprobe JSON, transport
URLs, and FFmpeg argv as fixtures. Include missing versus empty state, ordered
streams, fractional FPS, absent tracks, sparse KLV, and invalid input. Compare
JSON semantically while preserving array order and observable numeric/null
behavior. Record any source/test/documentation disagreement explicitly rather
than introducing an incidental behavior change.

Gate: Cargo builds a skeleton, existing Python tests still pass, fixtures are
checked against the baseline, and runtime deployment still uses Python.

### 2. Port configuration and pure domain logic

| Python source | Rust responsibility |
| --- | --- |
| `appconfig.py`, `runtimesettings.py` | Environment configuration, defaults, settings parsing and validation |
| `transports.py`, `streamresponses.py` | Transport definitions, detached response models, advertised URLs |
| `ffmpegcmd.py` | Probe decoding, encoding decisions, FFmpeg argv construction |
| `statefile.py` | Compatible JSON documents, validation, atomic persistence |
| `filewatch.py`, `udphost.py` | Stable-file inventory, observation batches, cached DNS results |

Retain domain names and wire formats; use Rust enums and structs internally
without changing API terminology. Avoid generic frameworks around small pure
functions. Port behavior-focused tests, using temporary directories and injected
probe/DNS inputs where useful.

Gate: fixture parity for every transport and video mode; state round trips
through both implementations; failed writes and scan errors preserve prior
state; matching validation/default behavior.

### 3. Port process management and supervision

Port `ffmpegprocess.py`, `mediastream.py`, `streamregistry.py`, and
`stream_supervisor.py`. Maintain one owner per child; explicitly wait/reap
after termination. Preserve the five-second TERM wait followed by the bounded
KILL wait, retained handles on failed shutdown, signal-derived exit reporting,
4 KiB diagnostic tail, bounded reads, and per-stream log rotation.

Port the bounded queue, serialized commands, 32-item maximum batches,
periodic supervision, snapshot publication even after partial failure,
watcher failure propagation, pending-request failure on shutdown, and
MediaMTX readiness wait. Use fake process/clock boundaries for lifecycle tests
and real short-lived child processes for cleanup and diagnostic checks.

Gate: concurrent start/stop/settings requests never create two live children
for one stream; queue saturation leaves snapshot GETs responsive; finite
completion and crash recovery match; no orphaned children after shutdown.

### 4. Port HTTP and exercise the unchanged UI

Port `streamapi.py` onto Axum. Preserve query parsing details, including blanks,
duplicate-key handling, errors, action routing, and request-host fallback.
Serve embedded UI/font assets and protected HLS files. Keep request handling
independent of probing, DNS, and mutable stream traversal.

Gate: Rust HTTP integration tests cover all current routes, successful
mutations, failure responses, queue saturation, and HLS traversal rejection.
Run the existing UI against an isolated Rust instance and complete the relevant
checks in `docs/ui-requirements.md` and `docs/regression.md`.

### 5. Replace host utilities and switch build packaging

Port `scripts/compose_up.py`: preserve explicit environment overrides,
Docker-daemon hostname discovery, Docker context/DOCKER_HOST precedence,
local-versus-remote daemon detection, lowest-metric IPv4 route selection,
subprocess timeouts, and Compose's exit status. Port its four existing tests.

Port `socket_memory.py`: preserve `--interval`, `--output`, `--once`, JSONL
fields, procfs and container observations, permission/error reporting,
four-worker concurrency, 20-second command timeout, and 16 MiB/four-backup
rotation. Keep the diagnostic host-only and document its Linux requirements.
Use fixture tests and an explicit one-shot smoke run; never start a background
collector as part of ordinary build or test commands.

Add a Rust builder stage compatible with the Alpine/musl runtime. Build the
service using `cargo build --locked --release`; copy only the service binary
into the runtime image alongside existing external tools and configuration.
Verify linked libraries in the final image and build supported architectures
explicitly. Keep the patched MediaMTX stage. Remove Python installation and
source copying after parity passes, and update entrypoint.sh to launch Rust.

Keep Make as thin convenience wrappers: image build remains `make build`,
`make up` invokes the Cargo-built host helper, and `make test` runs Rust tests
and the required image-level checks. Compile/run tests in a dedicated Docker
test stage with the same FFmpeg/MediaMTX binaries as production; the final
runtime image must contain neither compiler nor test harness.

Use Cargo.toml as the application-version source and pass that value to image
build/tagging. Remove the duplicate `.env` VERSION declaration only when both
the helper and direct-Compose documentation supply the derived version
consistently. Keep deployment settings in `.env`.

Gate: host tools run without Python; both Compose configurations render;
the built image runs as UID 1000 with no Python requirement; assets and runtime
configuration are present. `cargo package --locked` verifies a source package
with the required assets; this does not publish it or bundle FFmpeg/MediaMTX.

### 6. Validate transport parity, cut over, and remove Python

Use isolated ports, separate writable state/HLS directories, and a copy of
saved state for comparison. Never run both versions against one state file or
the same stream listeners. Preserve the previous image and a state backup
before replacing a running deployment.

Run all ten transport kinds with real FFmpeg/MediaMTX. Validate passthrough,
transcode, MJPEG fallback, loops, listener reconnects, host and bridged
networking, and a read from the intended consumer container. For KLV, compare
packet counts and payload SHA-256 hashes against the source across a loop in
both video modes, including sparse/untimestamped metadata. Codec labels alone
are insufficient. Compare repeated-loop RSS and CPU against the Python image
under the same workload; most video processing remains in FFmpeg, so do not
assume the language change improves encoding performance.

Reconcile the container's current one-second stop grace period with the
supervisor's longer bounded child shutdown. Measure shutdown with several
streams and choose an explicit container-wide deadline/grace period that
allows cleanup; verify both vital-child exit and SIGTERM cases.

After parity succeeds, remove maintained Python sources, tests, support files,
and Python-specific build/documentation commands. Keep media, saved state,
generated diagnostics, and historical artifacts. Update README, regression
instructions, ignores, and the module map to the final Rust layout.

Gate: deploy the verified image, confirm the UI/API and live consumer playback,
restart and verify saved-state continuity, and report build, test, and deployment
results separately. For rollback, stop Rust first and restore the previous image
with the compatible state; restore the backup if state integrity is uncertain.

## Completion checks

- `cargo fmt --all -- --check`
- `cargo clippy --locked --all-targets -- -D warnings`
- `cargo test --locked --all-targets`
- `cargo build --locked --release` and `cargo package --locked`
- Container-stage tests, both Compose config checks, browser regression,
  live transport/KLV checks, persistence/restart, and shutdown checks pass.
- A clean checkout builds and packages using Cargo/Docker without Python;
  shipped code, tests, and host helpers are Rust.
- Existing state and external API clients require no conversion.
- No publishing, Git push, or production cutover is implied by this plan alone.
