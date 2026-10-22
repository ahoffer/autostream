# Simplified refactor measurements

The main improvement is single-thread ownership of output behavior. The simplified code removes lifecycle coordination state and reduces import coupling. It does **not** substantially reduce average cyclomatic complexity.

## Scope and reproducibility

- Pre-refactor baseline: commit `1a6587091b1b2e3ccb8692bdd318555aa1539473`, preserved in `.generated/rdd-refactor/before/`. This is the modular application before the RDD attempt, not the earlier monolith.
- Rejected draft: the concurrent RDD implementation immediately before simplification, preserved in `.generated/rdd-refactor/rejected/`. It was never deployed.
- Simplified: current working-tree application modules reachable from `stream_supervisor.py`. Tests and `socket_memory.py` are excluded.
- Tools: Python 3.14.4, Radon 6.0.1, cohesion 1.2.0, and pydeps 3.0.9, using the existing isolated metrics environment.
- Reproduce the static comparison with `.generated/code-metrics/venv/bin/python .generated/rdd-refactor/measure.py`. Machine-readable results are in `.generated/rdd-refactor/metrics.json`. Pydeps independently confirmed all 10 current local import edges.

## Size, dependencies, and complexity

| Measure | Pre-refactor | Rejected draft | Simplified |
|---|---:|---:|---:|
| Application modules | 12 | 11 | 11 |
| Local import edges | 19 | 11 | 10 |
| Import cycles | 0 | 0 | 0 |
| Largest module, physical lines | 435 | 306 | 306 |
| Application physical lines | 1848 | 1408 | 1381 |
| Maximum outgoing local imports | 6 | 6 | 7 |
| Maximum incoming local imports | 5 | 2 | 1 |
| Mean cyclomatic complexity | 4.53 | 4.53 | 4.53 |
| Maximum cyclomatic complexity | 26 | 20 | 21 |
| Named parameters across declarations | 109 | 79 | 95 |
| Mean named parameters per declaration | 1.24 | 1.04 | 1.16 |

Radon counts each function/method once; class aggregate scores are excluded. Parameter counts exclude self/cls and variadic forwarding. They describe declarations, **not the weighted argument score**. Physical lines include comments and docstrings. The composition root now has more imports because dependencies are assembled there.

The maximum is now state-file validation (21), followed by command decisions (19) and probing (18). Those responsibilities remain real complexity; splitting functions just to lower a score would not establish a better design.

## Stored fields and cohesion

| Class | Draft fields | Simplified fields | Draft cohesion | Simplified cohesion |
|---|---:|---:|---:|---:|
| MediaOutput | 18 | 12 | 32.29% | 33.33% |
| OutputRegistry | 12 | 8 | 40.56% | 47.25% |
| FFmpegProcess | 8 | 7 | 58.18% | 46.00% |
| StreamStateStore | 4 | 2 | 34.38% | 43.75% |
| UdpHostResolver | 2 | 1 | 100.00% | 100.00% |
| StreamHandler | 2 | 3 | 37.50% | 35.71% |

The output, registry, process, and store together hold 29 fields, down from 42 in the rejected draft. This includes dependencies and useful business state, not just coordination fields. The five-field ProcessObservation class was also removed.

The cohesion package measures method/field usage density, not responsibility quality. A process wrapper can score lower after separating diagnostic reading from termination even while the design becomes easier to reason about. Constructors and injected collaborators affect these scores. No claim is made that every class improved numerically.

## Weighted arguments: concrete boundaries

The agreed rules are retained: scalars 1; dictionaries count keys up to 5; stored fields of passed objects count individually; homogeneous collections 1. Implicit self is excluded, and receiver fields are reported above. The following are boundary measurements, not a fabricated whole-program total.

| Interaction | Pre-refactor points | Simplified points |
|---|---:|---:|
| Apply all four settings: entry plus output lookup | 5 (source name + four settings) | 5 (output identity lookup + four-key changes mapping) |
| Launch helper inputs, including process-wrapper construction | 16 (argv + source name + output name + 13-field Config) | 9 (8-field configured Logger at construction + argv at start) |
| Build argv from resolved decisions, with a string source path | 11 (10-field EncodeSpec + output kind) | 8 (path + 6-field FFmpegCommandSettings + destination) |
| Store one output record | Not a scoped operation | 6 (identity + record capped at five keys) |
| Deliver watcher observation to registry supervision | Not a queued operation | 5 (five-key observation mapping) |

The Logger count is measured from the standalone standard-library Logger used by composition on this Python version. Popen, Future, Queue, and pathlib objects are not assigned arbitrary one-point costs. The command-row example explicitly uses a string path; actual domain preparation passes a Path, whose stored implementation fields must be included in a runtime total.

The queue is not free. For a four-field edit, `_submit(action, output_id, changes)` receives 6 points. Its deferred `_execute_request(registry, action, output_id, changes)` receives 14 points with the current eight-field registry. The partial captures that registry dependency; it is not disguised as a one-point function. Future/Queue internals, serialization, preparation, nested calls, optional paths, and repeated supervision are additional costs. The boundary rows must not be summed and presented as an end-to-end score.

Whole-program weighted totals are not reported: static analysis does not reliably infer dynamic dictionary cardinality, opaque library state, closure captures, or execution frequency. The concrete counts show that encapsulation mainly reduces repeated inputs to process operations; it does not eliminate information or guarantee a lower total for every HTTP interaction.

## Behavioral validation

- 45 standard-library tests: independent settings, queued FIFO operations, Stop received during preparation, full-queue rejection, cached GET during blocked work, watcher stability/backpressure/errors, migration, saved choices, stuck shutdown, finite completion, recovery, and cleanup.
- 32 generated-command comparisons match the pre-refactor commands across transports, codecs, modes, and frame-rate choices, normalizing equivalent bitrate notation.
- Real local FFmpeg 8.0.1 UDP smoke test: passthrough and transcode both exited successfully and delivered packets. This does not establish deployed RTSP/HLS behavior.
- Browser check using Playwright: changed RTSP to transcode at 12 FPS while UDP stayed passthrough at 30 FPS; stopping RTSP left UDP running. No console errors.
- All four RDD Mermaid diagrams parse and render with Mermaid 12.2.0. Reserved participant identifiers and semicolon separators were corrected.
- Docker module-copy manifest and whitespace checks pass. No application image build, deployment, or live-state migration was performed; the existing application container remains stopped.
