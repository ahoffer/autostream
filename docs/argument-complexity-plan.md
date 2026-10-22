# Plan: reduce argument complexity and improve encapsulation

Status: proposal only. No application implementation or deployment changes are included.

Recommendation: use Approach A to remove oversized inputs first, then apply the
ownership changes in Approach B. Keep the current HTTP API, state-file format,
codec decisions, and lifecycle behavior. The primary benefit of B should be
stronger ownership and fewer exposed internals, not just fewer explicit arguments.

## Scoring contract

The following interpretation is provisional pending confirmation of the scoring
question asked during planning:

- Count explicit arguments at project-owned Python call boundaries. Each scalar
  costs 1 point. Positional and keyword arguments count equally.
- A dictionary costs one point per top-level key, capped at 5. Class instances
  cost one point per stored field, including private and derived stored fields.
  These weights replace the scalar weight; they are not added to it.
- Expand class fields one level only. Properties and methods are not stored
  fields. Do not recursively expand a nested object in this primary score.
- A homogeneous collection costs 1 point. For a heterogeneous tuple/list, use
  the sum of the element weights; this is an explicit additional convention.
- Exclude implicit self/cls from the primary score. Measure receiver state
  separately so converting a free function to a method cannot be mistaken for
  eliminating the data it needs.
- Count only supplied arguments at actual call sites. For the interface table,
  use the stated representative call, including all four settings when updating
  settings. Omitted defaults do not count. An explicit None still costs 1.
- Expanding **settings counts the supplied keyword values individually. Passing
  settings as a dictionary counts its keys, up to 5. A four-key update therefore
  has the same weight either way.
- Constructor calls are calls and must be counted. Returning an object has no
  argument score until it is passed onward. Count helpers and forwarding calls
  when comparing an entire operation, not only the outermost method.
- Do not substitute an opaque third-party instance, such as subprocess.Popen,
  with a made-up one-point weight. Such calls need an explicit type policy or
  are marked unscored. Proposed string paths are scalars; existing Path objects
  and OS handles are not silently treated as one-field project classes.

The quantitative comparisons below cover identified project-owned interfaces,
not every stdlib invocation or a runtime-frequency-weighted application total.
They do not use a package's cohesion percentage as an argument score.

### Avoid misleading improvements

Do not replace typed records with dictionaries to exploit the five-point cap.
Do not wrap a large object in a homogeneous list to make it cost one point.
Do not move arguments into a module global, closure, or arbitrary context object.
Track constructor costs, stored receiver fields, and cross-object field access
alongside the requested score. Stream collections already score only 1, even
though processing each member can require many high-cost calls.

## Current inputs

Counts verified against the working tree's declarations and stored assignments:

| Class | Stored fields | Cost when explicitly passed |
|---|---:|---:|
| Config | 13 | 13 |
| Stream | 11 | 11 |
| Output | 9 | 9 |
| FileProfile | 6 | 6 |
| EncodeSpec | 10 | 10 |
| StreamManager | 4 | 4 |
| StreamRegistry | 6 | 6 |
| PersistedStream | 8 | 8 |
| PersistedState | 2 | 2 |

Examples: Config includes max_video_bps even though it is init=False. Output's
running/occupied properties do not add fields. PersistedState's two dictionaries
are two fields under the one-level convention; their contents are not free of
complexity merely because this score does not expand them.

## Approach A: narrow interfaces while keeping the current orchestration

### 1. Pass the process layer only its actual inputs

Change the FFmpeg launch boundary from
`launch_output(argv, stream_name, output_name, config)` to
`launch_output(argv, log_path, show_output)`, with a string log_path.
Compute the output's path where stream/output identity and configuration already
meet. Keep stream-level logging in the manager. Avoid passing both identity and
its already-derived path through multiple helpers.

Likewise use `ffmpeg_death_reason(log_path, show_output)` and
`_ffmpeg_tail(log_path)`. Remove `_ffmpeg_log_path` from the diagnostic call chain.
This is a real reduction in the information the process layer receives: it no
longer sees API ports, UDP allocation bounds, bitrate settings, or video roots.

### 2. Give policies narrowly defined values

Use these small immutable value records; names are provisional:

- BitrateLimit: text and bits_per_second (2 fields). Both values already exist
  in Config. Construct the record once, not on each status/launch call.
- UdpDestination: host, first_port, last_port, reachable (4 fields). Construct
  the initial value at startup and replace/update it when reachability changes.
- OutputAvailability: failure_reason and waiting_reason (2 optional strings,
  at most one populated). Construct it at the output availability decision.

Use `bitrate_cap_for(source_bps, limit)` and
`udp_launchable(udp_port, destination)`. The latter produces availability without
receiving a complete Stream. These records represent specific concepts rather
than a general-purpose parameter bag. Dictionary and class versions would have
identical point weights at these sizes; prefer explicit typed fields.

Keep encoding selection based on Stream initially. Splitting it into many
unrelated scalar parameters would increase arity and can increase total points.

### 3. Separate output state from stream-level presentation

Change `_output_state(stream, output, reachable)` to
`_output_state(output, availability)`.

Change `_output_status(stream, output, reachable)` to
`_output_status(output, availability, argv)`.

The stream-level presenter obtains the encoding specification once and computes
preview commands from it. It supplies the running process's recorded argv when
appropriate. This avoids each output helper deriving commands and rereading all
stream configuration. Remove the redundant `default_argv` forwarding path once
its callers use the existing specification directly.

Preserve state precedence: deliberately stopped first; missing port before
running; completion before transient reachability failure; transient waiting
before crash-restart/error presentation. Keep no-video and launch diagnostics.

### 4. Pass existing settings edits as one mapping

Use `update_settings(stream_name, changes)` with the already validated settings
mapping instead of expanding **settings into four keyword values. Maximum arity
falls from 5 to 2; maximum weighted cost stays 5. Missing keys still mean retain
the old setting, and an explicit None still means automatic behavior.

Keep one atomic Apply and at most one restart per running output. Do not split
this into four setters merely to lower per-call argument counts.

### 5. Preserve boundaries that are already useful

Keep the ten-field EncodeSpec as the explicit encoding/FFmpeg boundary in A.
Keep probing, storage, file watching, and HTTP handling separate. Keep the
process handle inside the lifecycle code where practical; score any remaining
third-party-handle boundary separately rather than inventing its field count.

A intentionally does not claim to resolve direct manager/registry dictionary
access. That remains the ownership problem addressed in B.

## Approach B: make the state owners responsible for behavior

Apply A's narrow configuration concepts, then replace repeated data transfers
with operations on the owning objects. This is a proposed design, not an
implemented class graph or a measured future cohesion score.

### Object responsibilities

- StreamRegistry owns its maps, name collisions, retained settings, port
  assignments, and synchronization. Public operations handle lookup, claim,
  removal, and safe iteration/snapshots. Callers do not access its dictionaries
  or lock field directly.
- Stream owns stream identity, saved settings, the last probe, and its outputs.
  It creates its encoding specification, handles a single settings update, and
  coordinates output choices. It exposes operations rather than writable fields
  for the manager to edit.
- Output owns its process, enabled/completed/stopping state, crash history, and
  log destination. It provides launch(argv), stop(), state(availability),
  status(availability, argv), and death_reason(). It must retain process identity
  checks and refuse to reuse a destination while a process is still stopping.
  Adding log_path and show_output to today's Output gives 11 stored fields; these
  fields count if an Output is explicitly passed anywhere.
- EncodeSpec remains an immutable ten-field record and gains
  build_argv(output_name). Command construction is a behavior over its own data;
  the complete specification no longer needs to be sent into a free function.
- BitrateLimit owns its two values and exposes cap_for(source_bps).
- UdpDestination owns reachability and the configured range, and exposes
  launchable(udp_port). OutputAvailability remains a two-field result.
- StreamManager becomes an application coordinator: resolve a stream through
  the registry and invoke its operation. HTTP routes continue to use this facade.
  The facade's update_settings(stream_name, changes) still costs 5 at full size;
  forwarding changes into a Stream method is an additional 4-point call.

Construct the narrow policy objects at the composition boundary and give each
owner only the policies it uses. All constructor inputs and any later service
object transfers must be included in the final implementation's score. Do not
infer that a service reference costs one point simply because it is an object.

### Keep concurrency behavior explicit

Registry membership/port operations and each stream's lifecycle need clearly
defined ownership. If separate registry and stream locks are used, establish a
single lock ordering and a retired/generation check for removed slots. Prefer
preserving the current synchronization model initially behind the new boundary.
Never hold state locks across probing, DNS, or process waits. After I/O, verify
that the slot/output still owns the operation before changing its state.

Do not replace direct dictionary access with a long chain of trivial forwarding
methods. Each added call has a cost under the requested metric, as well as a
maintenance cost. The new API must express complete operations.

### Provisional naming choices

These are alternatives for later implementation review, not terminology changes:

| Responsibility | Alternatives | Recommendation and tradeoff |
|---|---|---|
| Process-owning output | Output / OutputProcess / OutputController | Keep Output: least churn; OutputProcess is narrower but understates enabled/completed state; OutputController emphasizes orchestration but adds another abstraction. |
| Immutable command description | EncodeSpec / EncodingPlan / FFmpegCommand | Keep EncodeSpec: existing terminology; EncodingPlan emphasizes decisions; FFmpegCommand incorrectly suggests argv already exists. |
| UDP destination policy | UdpDestination / UdpTarget | UdpDestination communicates host/range/reachability; UdpTarget is shorter but can be confused with one concrete host:port. |

## Projected interface metrics

Each cell is **explicit argument count / weighted points**. Count the full
settings update. The B process/status calls are methods on the Output that owns
the data. A's records have exactly the fields specified above.

| Operation and current interface | Current | A | B |
|---|---:|---:|---:|
| bitrate_cap_for(source_bps, config) | 2 / 14 | 2 / 3 | 1 / 1 |
| process launch_output(argv, stream_name, output_name, config) | 4 / 16 | 3 / 3 | 1 / 1 |
| ffmpeg_death_reason(stream_name, output_name, config) | 3 / 15 | 2 / 2 | 0 / 0 |
| udp_launchable(stream, reachable) | 2 / 12 | 2 / 5 | 1 / 1 |
| _output_state(stream, output, reachable) | 3 / 21 | 2 / 11 | 1 / 2 |
| _output_status(stream, output, reachable) | 3 / 21 | 3 / 12 | 2 / 3 |
| build_argv(spec, output_name) | 2 / 11 | 2 / 11 | 1 / 1 |
| encode_spec(stream) | 1 / 11 | 1 / 11 | 0 / 0 |
| manager.update_settings(name, all four edits) | 5 / 5 | 2 / 5 | 2 / 5 |
| Sum of these nine interface samples | **25 / 126** | **19 / 63** | **9 / 14** |

For this fixed sample, A reduces explicit slots by 24% and weighted points by
50%. B reduces slots by 64% and weighted points by 88.9%. These are interface
budget projections, not whole-application savings or runtime measurements.
They exclude setup and additional calls; those cannot be omitted from acceptance
measurement. In particular, the B settings facade forwards the four-key mapping,
so its complete route may cost more despite the unchanged outer score.

### Construction and receiver costs

- BitrateLimit construction costs 2 points; UdpDestination construction costs 4.
  Every new OutputAvailability construction costs 2. Reusing an unchanged value
  avoids reconstruction, not the cost of explicitly passing it again.
- Existing Output construction supplies its name: 1 point. B's proposed
  Output(name, log_path, show_output) costs 3, adding 2 points per output at setup.
- EncodeSpec construction still supplies ten scalar decisions: 10 points in all
  approaches. Its build_argv method saves passing the record onward; it does not
  remove its ten pieces of state.
- Stream construction currently receives three scalars and Config: 16 points.
  B's final constructor must be scored from its actual dependency schema; the
  proposal does not claim a free or already-quantified replacement constructor.
- If receivers are charged as an additional audit, spec.build_argv(kind) is
  10 + 1 = 11, the same as the current free function. An 11-field Output's
  launch(argv) is 12 points including its receiver, not 1. This audit distinguishes
  reduced caller burden from reduced underlying data complexity.

### Two complete, narrowly defined call-chain comparisons

Count project-owned calls only, with argv already built. These are examples,
not complete end-to-end application totals.

1. Non-debug process launch:
   - Current: launch_output = 16; nested _ffmpeg_log_path = 15; total 31.
   - A: launch_output(argv, log_path, show_output) = 3; no nested path helper;
     total 3. Path derivation remains at the caller using known scalar identity.
   - B: output.launch(argv) = 1; the path and output setting are already owned.
   - Including one output construction: current 1 + 31 = 32; A 1 + 3 = 4;
     B 3 + 1 = 4. B's one-point recurring call is not a one-point cold setup.

2. Crash diagnostic with a nonempty file log:
   - Current: death_reason 15 + tail 15 + log_path 15 + two _last_lines calls
     of 2 each = 49.
   - A: death_reason 2 + tail(log_path) 1 + two _last_lines calls of 2 = 7.
   - B: output.death_reason() 0 + output._tail() 0 + the same two _last_lines
     calls of 2 = 4. Output construction is separate as above.

The argument counts for those chains are respectively 7 -> 3 -> 1 for recurring
launch, and 13 -> 7 -> 4 for diagnostics. External file/Popen APIs are unchanged
in these examples and are outside this project-owned-call scope.

## Effect on the existing metrics

| Metric | Current | A expectation | B target / limitation |
|---|---|---|---|
| Manager methods directly accessing registry.streams | 19 of 21 | No claim of reduction | 0; replace representation access with owned operations |
| Manager references to registry.streams | 27 | No claim of reduction | 0 |
| Manager references to registry.lock | 25 | No claim of reduction | 0 external references; synchronization remains inside owners |
| Local import cycles | 0 | Preserve 0 | Preserve 0 |
| Local import edges | 19 | Re-measure; narrower arguments need not remove imports | Re-measure; object ownership does not guarantee fewer imports |
| Maximum outgoing local imports | 6 | Re-measure | Re-measure; no numerical prediction before the class graph is implemented |
| StreamManager TCC / LCOM4 | 0.958 / 1 | May remain similar | Not predictable; removing shared-registry access can lower TCC while improving encapsulation |
| StreamManager cohesion-package field usage | 37.5% | May remain similar | Not predictable; ownership changes alter both numerator and denominator |
| cleanup_dead_processes cyclomatic complexity | 26 | Likely mostly unchanged | Split per-output lifecycle decisions from registry iteration; total decision complexity may remain |

Exact future cohesion, import, and cyclomatic scores require implementation.
They should be measured, not invented from a class diagram. Pure functions and
immutable records remain appropriate where they keep dependencies explicit.

## Implementation sequence and acceptance

1. Agree the scoring conventions and provisional names. Freeze the current
   behavior fixtures and score representative startup, launch, stop, status,
   settings, crash-recovery, and persistence call chains.
2. Implement A's process/config boundary changes first; check log/error
   equivalence and actual arguments at every changed caller.
3. Introduce the narrow UDP and bitrate values; preserve waiting/failure
   precedence. Reuse the encoding specification during status generation.
4. Pass the already-existing settings mapping intact; verify omitted versus
   explicit automatic values and atomic restart behavior.
5. Introduce B's ownership incrementally: EncodeSpec behavior, Output lifecycle,
   Stream settings/lifecycle, then the registry facade. Re-score constructors,
   return-value forwarding, and helpers after each change.
6. Run the existing 25 tests and focused regressions for the new boundaries:
   stop-during-probe, process replacement while stopping, stuck process reuse,
   output enablement, finite completion, UDP recovery, and port retention.
7. Compare status JSON, FFmpeg argv, and persisted state with current behavior.
   Re-run pydeps, cohesion, Radon, and the argument score. Report raw arguments,
   weighted explicit points, setup costs, receiver fields, and representation
   access together. Do not optimize one by hiding another.
8. Build/deploy only after implementation is separately authorized. This plan
   does not start the currently stopped container.
