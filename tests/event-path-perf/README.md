# P2 event-path timing ladder

This implements the transport and scheduling controls from
[PR #252](https://github.com/yongkyuns/nxrs/pull/252) and the
[P2 benchmark plan](../../docs/event-path-benchmarks.md#p2--a-small-timing-ladder-that-isolates-useful-costs).
It measures the actual `messaging` facade and pinned Crossbeam 0.5.17 with
statically dispatched Rust callers. It does not change a production transport.

For the engineering conclusions and the next board run, start with the
[P2 decision report](DECISION-REPORT.md). It records the compiled facade review,
the latest physical P0/P1 observations, and an eighteen-capture first campaign
using the existing cases. The [sanitized physical summary](results/2026-11-04-esp32s3/README.md)
now records all 18 captures and six matched comparisons; independent timing
qualification remains open.

## Experiment contract

The baseline is a checked 16-byte payload and capacity eight. The separately
named `imu16` profile carries the real `imu::ImuSample` at capacity sixteen;
each executable records its target `size_of` and alignment. Neither profile
acquires hardware. Both validate all meaningful payload fields and preserve
provider/sequence identity. IMU padding is never hashed or interpreted.

| Family / command | Measured operation and controls |
| --- | --- |
| `ready-{std,messaging,crossbeam}-{try,send}` | Same-thread accepted admission into a nonfull queue followed by receive. Record admission and receive boundaries separately. `send` and `try_send` are distinct experiments. |
| `full-{std,messaging,crossbeam}` | Fill exactly to capacity outside the timed call, time `try_send`, require the original rejected value, then verify every retained queue entry and FIFO order. Only the rejected call enters the admission counts. |
| `blocked-{std,messaging,crossbeam}-{try,send}` | Empty receive followed by one admission; same worker stack and scheduling relationship. Fresh native `WAIT_SEM` proof for every target iteration, including warmup. |
| `ready-select-{one,three}`, `blocked-select-{one,three}` | Compare Crossbeam direct receive with one-arm and three-arm `select_biased!`. The ordinary queue is active; idle stop/important arms stay connected. |
| `semaphore` | Sequence publication plus native semaphore post, ending at return from `sem_clockwait`. This has zero message payload and is a notification control. |
| `park` | Sequence publication plus `unpark`, ending at the actual `park` return that satisfies the predicate. Count park returns separately from useful completions. |
| `timer-provider-messaging` | Native monotonic timeout wakes a blocked provider, which publishes the typed payload through the actual messaging facade; the owner receives and performs bounded checked work. Retain deadline lateness and correlated provider/admission/receive/handler markers. |
| `fixed-peer` | Known-peer marker after sequence publication and `yield_now`, with two distinct equal-priority FIFO tasks and a native peer-ready observation. Host execution refuses this contract. |
| `fanin-{std,messaging,crossbeam}-{1,4}` | One/four task-context providers issue finite unpaced `try_send` attempts while the owner drains one shared class queue. Retain accepted and rejected attempts and reconcile actual handling; these six cases do not guarantee saturation. |
| `fanin-messaging-{1,4}-normal` | One/four providers share an epoch and attempt one admission per 1 ms absolute slot. Retain each scheduled attempt and release lateness while the owner drains concurrently. |
| `fanin-messaging-{1,4}-saturation` | Hold the owner until all provider admissions finish. Require more attempts than queue capacity, exactly capacity admissions, and rejection of every excess attempt before draining. |

All current `*-send` cases admit into nonfull queues. They measure the blocking
API on a path with available capacity; they do not exercise a producer blocked
on a full queue or its later release.

The selector follows the Wi-Fi fixture's stop → important → ordinary ordering.
One-arm selection and direct receive use the same one-queue topology. In pinned
Crossbeam 0.5.17, the [single-receive macro expansion](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/select_macro.rs#L559-L573)
calls `Receiver::recv()` directly. The `*-select-one` cases therefore measure
the macro's direct receive path; they do not independently measure the selector's
registration engine. Three-arm selection has additional stop capacity one and
important capacity N, alongside ordinary capacity N. That topology cost must
not be described as selector-only overhead. This P2 adapter does not claim the
complete P3 owner, fairness, deadline, closed-arm disabling or registration-race
qualification.

All target notification/blocked cases use FIFO sender 120, receiver 180 and
16 KiB worker stacks. Fan-in and timer-provider cases use the same 120/180
provider/owner relationship. The fixed-peer workers both use FIFO 120. The observer
uses FIFO 220 where needed and an independently runnable watchdog uses FIFO
240. Every requested policy/priority is read back. The reused native adapter
refuses SMP or non-flat NuttX at compilation. Linux reports its actual policy
and never invents target blocking, single-core execution or fixed-priority proof.

## Timing boundaries and lifecycle

Setup → excluded warmup → retained work → terminal quiescence → cancellation
and joins is explicit. Payload construction, setup gates, validation, allocation
and console output are outside the named timed calls. Samples are preallocated.
Transport and admission dispatch is monomorphized before the loop; there is no
dynamic transport call inserted into every measurement.

`start_ticks` → `admission_return_ticks` is caller-visible elapsed admission
time. It can include a higher-priority receiver's execution. `start_ticks` →
`receive_return_ticks` is end-to-end delivery. Ready cases also retain the
receive entry timestamp. Notification controls include their sequence
publication, as their operation names explicitly state. Fixed-peer markers
prove that the named peer ran; they do not measure exact intervening switch
counts or pure register-save/restore cost. No result divides a round trip by two.

The receiver announces a fresh sequence immediately before entering the measured
receive/selection/notification wait. On NuttX, the observer requires the matching
unfinished sequence in `TSTATE_WAIT_SEM`. An announcement or setup sleep alone
never qualifies a blocked sample. The final receiver stays behind a separate
gate until the sender records its final return, so thread destruction cannot
enter that last sample. Errors close the measured endpoint, release all other
wait sites and join. Shutdown never requires admission into a full data queue.

Park notifications use Release/Acquire sequence predicates. Early, stale,
coalesced and spurious tokens do not count as delivered work; tests cover
publication between predicate checking and a real `park`, cancellation and
partial-run cleanup. The native counter is initialized once before readers,
including during parallel host tests. Raw 32-bit timestamps have conservative
monotonic bounds and wrap/ordering checks; these are not clock calibration.

Finite fan-in preallocates a record for each attempted sequence. The owner
requests `recv_timeout(10 ms)` to revisit completion/cancellation
without requiring a sentinel slot or dropping worker resources during active
timing. Its exact receive API is recorded. Admission is published after
`try_send` returns, so the higher-priority owner does not wait for a preempted
producer's completion flag. Every provider's TID/policy/priority is recorded.
Duplicate, corrupt, missing and unadmitted delivery fail reconciliation. Workers
hold at terminal gates until active work is complete. Every fan-in case is capped
at 256 retained attempts and 64 warmups per provider.

The requested timeout is not a bound on the complete receive operation. In the
pinned Crossbeam path, the deadline is checked after `start_recv` returns; a
reserved-slot spin can prevent that check and the next cancellation check.
The independent watchdog and external capture deadline contain such a failure.
Neither a timeout parameter nor a `try_send`/`try_recv` method name establishes
bounded progress under fixed priorities.

The six original fan-in cases have no release pacing or guaranteed saturation:
providers run their finite attempt loops while the owner drains concurrently.
The four scheduled messaging cases use one shared monotonic epoch. In `normal`,
provider sequence `s` has planned release `epoch + (s - 1) * 1_000_000 ns`.
Providers wait only until that absolute slot, outside timed admission. A late
slot is still attempted; it is not skipped or moved to a new schedule. Coarse
native timer ticks can make multiple releases occur together. Each retained
attempt records `planned_release_ns` and `release_observed_ns`, and analysis
reports release lateness separately from transport latency. This records what
the scheduler achieved without claiming ideal 1 ms cadence.

In `saturation`, every planned release is the shared epoch. The owner remains
behind its start gate until all providers finish their unpaced admission loops.
The total attempts across providers, including warmup, must exceed capacity.
Exactly capacity attempts must be admitted and all excess attempts rejected;
the owner then drains and validates the admitted work. Admission still measures
`try_send` entry to return. Delivery latency includes the deliberate owner hold
and must be interpreted with that schedule.

Warmup exclusion is a prefix of each provider's sequence in every fan-in mode,
not a common barrier between providers. One provider can enter retained work
while another is still warming up. Saturation warmups can occupy every queue
slot, leaving only rejected retained attempts. Whole-run counts include these
warmups; retained records and checksums describe only the retained attempts.
These are finite scheduling and overload controls, not sustained offered-load,
coordinated-omission, fairness or deadline qualification.

### Native timer to typed handler

`timer-provider-messaging` exercises the native timer alternative in P2's
provider-to-handler family. Each iteration first establishes an empty owner
wait. The supervisor then sets a `CLOCK_MONOTONIC` deadline 50 ms ahead, and the
provider enters `sem_clockwait` on a dedicated empty semaphore. On the pinned
NuttX implementation, the semaphore deadline arms a kernel watchdog; its timer
interrupt completes the wait with `ETIMEDOUT`. The provider checks that exact
outcome before constructing and admitting its typed payload. An ordinary
semaphore post is cancellation, never a timer event.

The P2-owned `native-timer.c` only translates the absolute deadline and native
return status using target headers. Rust owns the sequence protocol, waits,
payload, admission, handler validation, cancellation and joins. P0's native
adapter remains unchanged. On NuttX, every iteration requires a fresh owner
queue wait and a fresh provider timer wait observed before the deadline.
Host runs exercise behavior and preserve both wait-proof flags as false.

Each retained `timer` record separates the planned deadline and monotonic
provider-resume time from raw-counter provider resume, admission entry/return,
receive, handler-start and handler-end markers. The handler performs bounded
payload validation and checksum work. A higher-priority owner can finish that
work before the provider's admission call returns; the analysis permits that
ordering while checking each correlated stage. Final gates retain the last
admission-return timestamp before worker cleanup.

The 50 ms arming interval is outside the raw stage measurements. Deadline to
provider resume is reported as unqualified monotonic lateness; the deadline is
not an observed IRQ/callback timestamp. This control does not acquire a sensor
or claim a physical device's interrupt latency. It is capped at 128 retained
iterations and 32 excluded warmups, with per-iteration counter-wrap guards and
the independent watchdog still active.

## Build and run

From the repository root:

```sh
cargo +1.90.0 test --locked -p nxrs-event-path-perf
cargo +1.90.0 clippy --locked -p nxrs-event-path-perf --all-targets -- -D warnings
python3 -m unittest discover -s tests/event-path-perf -p 'test_*.py' -v
CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_LTO=false cargo +1.90.0 build --locked --release -p nxrs-event-path-perf
python3 tests/event-path-perf/run-host.py --binary target/release/event-path-perf --output target/event-path-host
```

The host corpus checks 42 expectations: all 32 supported named cases, nine
normalized IMU controls and the expected fixed-peer refusal. This includes all
four normal/saturation messaging fan-in cases and the timer-provider case with
both payload profiles. Every run gets a new directory
with raw stdout/stderr, a capture receipt and analysis. The corpus retains one
shared executable and replays each saved result, including the expected refusal.
It also requires all seven fixed comparison groups to match and retains each
group report under `comparisons/`.
Reusing an output directory is rejected. Individual runs accept
`CASE SAMPLES WARMUP PROFILE`:

```sh
target/release/event-path-perf blocked-messaging-try 128 16 bytes16
target/release/event-path-perf ready-messaging-try 128 16 imu16
target/release/event-path-perf fanin-messaging-4-normal 128 16 bytes16
target/release/event-path-perf fanin-messaging-1-saturation 16 4 bytes16
target/release/event-path-perf timer-provider-messaging 16 4 imu16
python3 tests/event-path-perf/capture.py host --binary target/release/event-path-perf --case park --samples 128 --warmup 16 --output target/event-path-park
```

With the pinned SDK and target prerequisites from the existing std-channel
fixture installed:

```sh
python3 tests/event-path-perf/build.py --platform mps2-an521-mock --out target/event-path-arm
python3 tests/event-path-perf/capture.py qemu --build-directory target/event-path-arm --case fixed-peer --samples 16 --warmup 4 --output target/event-path-arm/runs/fixed-peer
```

Build the ESP32-S3 baseline and optional digest candidate into separate new
directories:

```sh
python3 tests/event-path-perf/build.py \
  --platform esp32s3-service-footprint --clock-source native-counter \
  --boot-image-profile platform-default --out target/event-path-esp32s3
python3 tests/event-path-perf/build.py \
  --platform esp32s3-service-footprint --clock-source native-counter \
  --boot-image-profile ram-prefix-digest-v1 --out target/event-path-esp32s3-rom-digest
```

`platform-default` remains the default packing. The optional
`ram-prefix-digest-v1` profile is restricted to `esp32s3-service-footprint` and
uses [P0's paired image/loader contract](../std-channel-perf/SIMPLEBOOT_DIGEST.md).
Both are new builds with the matching loader patch; old images are not repacked
or relabeled. The profile participates in each new runtime build identity.
Legacy receipts retain their original identity derivation.

Each target build gets a private prepared std copy and Cargo directory. The
builder reuses P0's source/configuration checks in uninstrumented `latency` mode.
It additionally checks the selected Crossbeam and Crossbeam-utils dependency
edges, exact Cargo.lock registry checksums, every unpacked source byte against
the downloaded archives, and actual compiler-artifact paths. Instrumented or
changed source refuses the build. Compiler, configuration, source and image
identities are retained in `event-path-build-v1`.

The candidate retains the original `nuttx.merged.bin`, selected
`nuttx.rom-digest.bin`, packing report `simpleboot-digest.json`, patch ledger
`nuttx-patches.json`, actual `esp_loader.c`, and a copy of
`0107-xtensa-simpleboot-hash-offset.patch`. Build and capture validation recompute
the candidate bytes from the retained original and check the exact loader,
patch and provenance binding; offline replay performs the same checks without
requiring the original private build directory. The ROM-visible prefix digest
excludes hidden mapped payloads and does not establish secure boot. Whole-image
hashes remain required, and an offline digest check cannot clear an observed
ROM warning. The P2 candidate is build-only; these commands do not flash or
establish physical execution.

The [dedicated workflow](../../.github/workflows/event-path-perf.yml) runs the
host corpus, builds ARM, runs 29 captures in separate fresh QEMU boots, and
builds separate ESP32-S3 baseline and digest-candidate images with native-counter
selection. Nineteen of those captures cover every member of the
seven comparison groups on the 16-byte/capacity-eight baseline. The other ten
retain the notification, fixed-peer, fan-in and timer controls. The QEMU corpus includes one and four
providers in both normal and saturation modes and the timer-provider case with
both payload profiles. It does not flash or take
physical-board ownership.

## Evidence and pending acceptance

`analyze.py` accepts one ordered `event-path-perf-v1` run. It rejects malformed,
duplicate or mixed records; relabeled APIs/topologies; missing attempts;
incorrect payload checksums; invalid timestamps; fabricated scheduler/wait
proof; absent joins/final holds; and attempts to promote timing or progress
qualification. Retained checksums are independently reconstructed. Counts
include warmup; fan-in's excluded, nondeterministically admitted warmups mean
its whole-run checksum may be firmware-validated without independently
reconstructible warmup detail, which analysis reports explicitly.

Capture reuses P0's mechanical console I/O and independent-clock checks, with
P2-owned fault detection and target-console framing. Live capture and offline
replay reject case-insensitive kernel/fixture faults. A completed target capture
requires a boot before exactly one echoed command, contiguous ordered event
records, and the returned literal `nsh> ` prompt. Known NSH erase-line decoration
is accepted; only its exact `ESC` or `ESC[` fragments at raw EOF after that final
prompt may be incomplete. Repeated boots or commands, other terminal controls
and trailing content are refused. Serial sessions use schema
`event-path-session-v1` with the same
physical identity/calibration fields described in the
[P0 fixture](../std-channel-perf/README.md). Build ID, image hash, clock request,
CPU frequency and source receipts must agree. Artifact hashes are checked before
and after execution. Capture reconstructs the runtime build ID from the original
canonical source/compiler/build inputs, and cross-checks the compiler and std
library identities against the retained source proofs. Clock rejection and ROM
digest warnings remain visible without erasing valid functional data. Raw output and failed receipts survive
console deadlines, fixture refusal and serial-cleanup errors.

After detecting a native fault, capture keeps the outcome failed and collects
only incoming diagnostic bytes for at most one second, bounded by the existing
capture deadline, 64 KiB of additional output and the overall console limit.
It sends no command or retry and then performs the ordinary cleanup. This
preserves a delayed task/file/PC dump without accepting a faulted boot.

Physical session labels remain operator assertions. A matching image receipt,
observed boot banner and accepted clock calibration do not independently prove
the execution location, board identity or exclusive access. Capture keeps
`physical_execution_verified` false while retaining those separate facts.
Replay rejects unsupported true claims, including in failed captures, without
rewriting the original evidence.

### Preserved ARM startup failure

At head `5f9cad59`, [run 41749403901](https://github.com/yongkyuns/nxrs/actions/runs/41749403901)
built the ARM image and completed 28 captures, then detected a native assertion
before the final `timer-provider-messaging` / `imu16` command could be sent.
The [failed ARM archive](https://github.com/yongkyuns/nxrs/actions/runs/41749403901/artifacts/13285793832)
retains the failed receipt and the original 174-byte console prefix. It ends
at `dump_assert_info: Assertion`, after the NuttX version line and before any
boot banner, shell prompt or event-path record. Its image SHA-256 is
`f847433a922d30cc33c58c75d5390cc4b4df841137b8c57aafb021e13725f288`.

The complete-run replay/comparison gates were not reached. This is a pre-command
startup failure; the prefix contains no task, file or PC with which to establish
its cause or equate it to P1's earlier Idle_Task fault. The bounded diagnostic
collection above corrects capture's early truncation, not the native fault.
Later attempts retain their own source/build identities and cannot overwrite
or qualify this failed attempt.

### Retention and independent replay

A standalone capture retains the executable or the target build's exact receipt
and hashed artifact inventory. Target captures store these under `build/`;
compiler caches and SDK trees are not copied. Physical sessions retain their
original JSON bytes and any safely readable calibration trace, including a trace
whose hash disagrees with the session. `evidence.json` binds the retained files
and records missing or rejected calibration evidence. Replay uses the retained
trace without rewriting the operator's original session.

```sh
python3 tests/event-path-perf/replay.py target/event-path-park
python3 tests/event-path-perf/replay.py target/event-path-arm/runs/fixed-peer
```

Replay checks file hashes, rebuilds the build identity, reanalyzes the raw
transcript and compares the stored analysis and capture facts. It runs no
executable and opens no device. A zero exit status means the retained evidence
is internally consistent; the returned `capture_complete` remains false for a
failed or refused capture. The host corpus separately checks its one expected
refusal and requires every other run to complete.

For a corpus, `capture.py --artifact-root DIR` can retain one shared build or
executable already inside `DIR`; the capture output must also be inside it.
Replay then requires the explicit root and resolves only normalized relative
paths within it. Archive the entire declared artifact inventory and the run
directories together. The host runner and target CI use this mode to avoid
duplicating an executable or firmware image for every run:

```sh
python3 tests/event-path-perf/replay.py target/event-path-host/park-bytes16 --artifact-root target/event-path-host
python3 tests/event-path-perf/replay.py target/event-path-arm/runs/fixed-peer --artifact-root target/event-path-arm
```

Capture validates named cases and their storage limits before opening a device.
Timer runs are limited to 128 samples and 32 warmups; fan-in remains limited to
256 samples and 64 warmups per provider. Saturation controls require enough total
attempts, including warmups, to exceed the selected payload's queue capacity.
Serial capture computes a conservative deadline from the case's retained output volume
at 115200 baud, reset/command allowance and fixture watchdog budget. Its default
grows with the requested run; an explicitly shorter deadline is rejected, and
the maximum is 600 seconds. Host and QEMU retain their 30-second default. These
capture budgets include console transmission and are not timing measurements.

### Matched comparison groups

`compare.py` validates exactly three explicitly supplied, completed captures for
one of seven fixed groups. Each capture must pass offline replay first. The
groups share 19 named cases; direct Crossbeam receive is reused in the transport
and selection comparisons.

| Group | Required cases |
| --- | --- |
| `ready-try` | `ready-std-try`, `ready-messaging-try`, `ready-crossbeam-try` |
| `ready-send` | `ready-std-send`, `ready-messaging-send`, `ready-crossbeam-send` |
| `blocked-try` | `blocked-std-try`, `blocked-messaging-try`, `blocked-crossbeam-try` |
| `blocked-send` | `blocked-std-send`, `blocked-messaging-send`, `blocked-crossbeam-send` |
| `full-rejection` | `full-std`, `full-messaging`, `full-crossbeam` |
| `ready-selection` | `ready-crossbeam-try`, `ready-select-one`, `ready-select-three` |
| `blocked-selection` | `blocked-crossbeam-try`, `blocked-select-one`, `blocked-select-three` |

For example, after the ARM corpus has completed:

```sh
python3 tests/event-path-perf/compare.py ready-try \
  target/event-path-arm/runs/ready-std-try \
  target/event-path-arm/runs/ready-messaging-try \
  target/event-path-arm/runs/ready-crossbeam-try \
  --artifact-root target/event-path-arm
```

Inputs must share the same executable, or the exact target build receipt,
image, ELF and configuration. Payload/layout/capacity, counts and warmups,
clock properties, release schedule, worker stacks and logical scheduling roles
must agree. Actual thread IDs and run IDs may differ across fresh boots.
Physical comparisons match the declared board and operating conditions while
retaining each boot's measurement-session identity and calibration separately.
QEMU comparisons check the recorded board and command options, substituting the
verified ELF hash for its original path.

The JSON result retains each case's hashes, counts and raw distributions in a
canonical order. It does not pool samples, subtract percentiles, calculate
speedup, or rank transports. `matched: true` establishes the retained artifact
and declared-input match. It does not independently identify the emulator
binary or execution host, and operator labels do not verify physical identity;
`execution_context_verified`, physical-execution, timing and progress
qualification remain false. This gate compares cases within one build, not
separately optimized candidate builds. Baseline and digest-candidate images
therefore require separate comparison groups.

The two selection groups explicitly retain the direct-receive macro control and
the additional idle queues in the three-arm topology. Their whole difference
cannot be labeled pure selector overhead. Notification and fixed-peer controls
remain separately reported because they do not transfer the same payload.

All executable timing/progress qualification fields remain false. Raw-tick
distributions are characterization, not a speedup, WCET or production acceptance
claim. Physical captures still require P0's corrected independently calibrated,
phase-covered cohort and resource/placement review. P1's no-repair progress gate
is required before production transport adoption.

### P1 progress results and the P2 paths

[P1 PR #255](https://github.com/yongkyuns/nxrs/pull/255), at head `0bc150c`,
records the following [ARM/NuttX cohort](https://github.com/yongkyuns/nxrs/actions/runs/41680413250)
for Crossbeam 0.5.17 with crossbeam-utils 0.8.23. Each case used a fresh
single-core QEMU boot, capacity eight and a checked 16-byte payload.

| Reservation witness: no-repair gate refused | Matching control: no-repair gate passed | Tested interleaving |
| --- | --- | --- |
| `direct-reserved` | `direct-published` | Receiver at FIFO 180 enters `recv` while producer at FIFO 120 is reserved but unpublished; the control publishes first. |
| `select-reserved` | `select-published` | Three arms are registered and the owner is blocked. An independent ordinary-arm wake makes owner at FIFO 180 reenter selection while producer at FIFO 120 holds an important-arm reservation; the control publishes first. |
| `send-reserved` | `send-reclaimed` | Sender at FIFO 180 enters `send` on a full ring while consumer at FIFO 120 has reserved but not reclaimed a slot; the control reclaims first. |

All three reservation cases recorded a stall at the original priorities, then
delivered and joined only after diagnostic priority recovery. Their successful
CI diagnosis preserves a **failed no-repair contract**. The three controls
passed only their named interleavings. That archived cohort contained no
physical Crossbeam captures; the later fifteen-case physical follow-up is
described below. A general production contract requiring no repair remains open.

That P1 revision also passes seven prewarmed std mutex/condition-variable
witnesses without repair on ARM/QEMU: held/released single-item admission and
retrieval, blocked publication, publication before waiting, and blocked closure.
The held cases make one `try_lock` attempt, return Busy, preserve ownership and
transfer no value. These are primitive contracts with one item; they do not
define a bounded queue's overload, retry, fairness or multi-class owner policy.
The physical follow-up below adds seven std-primitive cases. The three Crossbeam
reservation failures remain failures in the complete ARM cohort.

At revision `d6f76e4d`, P1 also defines the separate
[`equal-fifo-run-to-block-v1` contract](https://github.com/yongkyuns/nxrs/blob/d6f76e4d21dbbb97167b32899da88e8137ffff8d/tests/channel-progress/FIFO-SCHEDULING-AUDIT.md).
Its six cases use two channel participants at FIFO 180 on one core and retain
the actual reservation/publication or reclamation order. The audited scheduler
places a newly ready equal-priority peer behind the preempted slot holder;
the holder must finish its reservation before the peer enters the conflicting
operation. All endpoint participants must obey the same effective-priority
contract, and no reserved slot may survive a wait, yield or user callback.
Bounded runnable work and bounded interference remain separate requirements.
Notification locks after slot release can still delay method return, so this
contract does not establish wait-freedom or a latency bound.

At evidence revision `8efbd59c`, P1 records
[fifteen fresh physical WROOM boots](https://github.com/yongkyuns/nxrs/blob/8efbd59cd392678aaedf1f00addc80a30a1682a3/tests/channel-progress/results/2026-11-04-esp32s3/README.md)
of firmware from `dc3bf1e8`: all seven prewarmed std-primitive cases and all six
equal-FIFO cases complete without priority recovery. The unequal-priority
selected reserved-slot case reproduces the stall and needs diagnostic recovery;
its published control completes without repair. The original direct
reserved/published and reverse send reserved/reclaimed cases still lack physical
execution. All fifteen boots retain the legacy ROM digest warning, and physical
execution/progress qualification flags remain false. These are scoped progress
observations, with no physical latency or complete-owner qualification.

P2's current scheduler profiles do not implement that equal-FIFO experiment:

| P2 family | Current schedule | Relation to P1's equal-FIFO contract |
| --- | --- | --- |
| Ready and full rejection | One FIFO 120 thread performs admission and receive/drain | No competing reservation holder is made runnable; these are call-cost controls. |
| Blocked transports/selectors, finite fan-in and timer-provider | Sender/providers at FIFO 120; receiver/owner at FIFO 180 | Unequal effective priorities are outside the contract. |
| Fixed peer | Two FIFO 120 tasks exchange a marker and yield | No channel payload or reservation is transferred. |
| Semaphore and park | Sender at FIFO 120; receiver at FIFO 180 | Notification controls have no channel reservation to qualify. |

The new P1 source argument concerns pinned Crossbeam's bounded array and
receive-only selection paths. It does not qualify the raw std queue or the
actual messaging facade. P2 keeps its existing priorities, case names and
qualification flags; a future equal-priority transport measurement would need
an explicitly named schedule and its own matching evidence. The original
unequal-priority failures remain applicable to their recorded interleavings.

P2's passing functional corpus establishes completion and conservation for its
named schedules. Its direct Crossbeam adapter calls the same `recv` path, and
the one-arm macro expands to that call. Its three-arm case keeps stop and
important idle and sends only on ordinary; it does not force P1's important
reservation plus independent wake. P2's nonfull `send` cases also do not exercise
P1's reverse full-ring interleaving. The `imu16` capacity and four-provider
topologies require their own contract justification.

The existing [actual-std witness](../std-channel-perf/README.md) separately
records a reserve-before-publication `try_recv` stall. P2's raw std adapter and
the actual [`messaging` facade](../../shared/messaging/src/channel.rs) use that
bounded std queue; the facade delegates admission and receive to it. This
source-path mapping does not represent a separately executed facade witness.
Keep instrumented witnesses and recovery out of P2 timing images and
distributions. Completed P2 samples remain useful characterization; production
adoption still requires the applicable no-repair progress contract and P0's
independent physical/clock gates.

### Remaining scope

The provider-to-handler case implements P2's native timer path. Physical sensor
acquisition and a device-specific IRQ notification source remain outside this
synthetic control; their acceptance requires the chosen source path and physical
evidence. A full-queue blocked-producer release control remains conditional on
a selected production call site that permits blocking and requires that control.
Current `send` cases use nonfull queues and cannot satisfy that separate control.

[P0's recorded WROOM comparison](https://github.com/yongkyuns/nxrs/blob/634552533aeee23c1dbed5b7389c11612e98e15a/tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/README.md)
contains four fresh boots of its paired digest candidate without the ROM SHA-256
warning; a separately built legacy-packing control reproduced the warning on
the same board. The candidate's three tight runs and one seeded run completed
with matching flashed-image/runtime identities. This is an observation of P0's
`std_channel_perf` firmware on that board and USB-reset path. It is not a P2
physical run or a comparison between identical executable builds.

All five P0 captures still record missing independent clock calibration and
`physical_timing_qualified: false`. Exact channel/stack allocation placement and
resource qualification remain open. P2's separately built digest candidate
needs its own coordinated fresh-boot/image evidence and calibration review;
neither the packing profile nor P0's boot observation verifies P2 execution or
changes P2's qualification flags. Physical-board ownership remains with the
coordinated P0/hardware workstream.

P3 owns full selected-owner behavior, P4 owns correlated native switch/IRQ
attribution, and P5 owns sustained offered-load and resource/product budgets.

This work owns `tests/event-path-perf`, its dedicated workflow, and the minimal
workspace member/lock entry. Existing P0 capture/timing code, P1 source hooks,
production modules, shared architecture documents and archived physical evidence
remain under their existing owners.
