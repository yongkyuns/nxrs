# P1: exact-source channel progress witnesses

This is workstream B of [#252](https://github.com/yongkyuns/nxrs/pull/252) and
[P1 in the event-path plan](../../docs/event-path-benchmarks.md#p1--qualify-progress-for-the-intended-scheduling-contract).
It qualifies specific scheduling interleavings before adopting the selected
three-class owner transport. It does not change a production queue, replace a
parker, or measure latency.

The existing [actual-std reserved/published witnesses](../std-channel-perf/README.md)
and their retained physical results remain separate. P0 owns their latency,
capture, clock and calibration changes. This fixture reuses only the mechanical
native ABI and common build/source preparation helpers, without editing them.

There are three experiment families, each with its own acceptance scope:

| Family | Cases | Contract under observation |
| --- | --- | --- |
| Unequal-priority Crossbeam | Six | Whether a high-priority peer can finish while a lower-priority participant has reserved an unpublished or unreclaimed slot |
| Single-item std primitives | Seven | One-attempt ownership transfer under mutex contention, condition waiting, publication and closure |
| Equal-priority FIFO Crossbeam | Six | Whether the original slot holder commits before a newly ready equal-priority peer resumes under the audited one-core FIFO contract |

The additional suites retain the earlier negative results. They do not introduce
a queue implementation or certify a production three-class owner.

## Recorded progress results — 2026-11-03

All six cases executed on fresh single-core ARM/NuttX QEMU boots from clean
source commit [`ed830b6`](https://github.com/yongkyuns/nxrs/commit/ed830b6d78e2beb44c6da90395f0fe5d2666d413).
An independent [hosted CI cohort](https://github.com/yongkyuns/nxrs/actions/runs/41660612437)
reproduced the same outcomes. Its checkout was merge revision `8d70931a`, with
the identical Git tree `b2a47b0e20564949457f09f80c1a7c884d0e006a`.

| Case | Unrepaired observation | Final delivery and cleanup | No-repair gate |
| --- | --- | --- | --- |
| `direct-reserved` | Receiver and producer ready; publication pending | Priority recovery, 1/1 payload, all joins | **Rejected** |
| `direct-published` | Both workers complete | 1/1 payload, all joins | Passed for this control |
| `select-reserved` | Three arms registered, initial native wait and independent wake verified; owner stalls on the important arm | Priority recovery, 1/1 important payload, all joins | **Rejected** |
| `select-published` | Same registered-wait and independent-wake path completes | 1/1 important payload, all joins | Passed for this control |
| `send-reserved` | Sender and receiver ready; slot reclamation pending | Priority recovery, 9/9 payloads, all joins | **Rejected** |
| `send-reclaimed` | Both workers complete | 9/9 payloads, all joins | Passed for this control |

The [ARM evidence artifact](https://github.com/yongkyuns/nxrs/actions/runs/41660612437/artifacts/13242762554)
retains raw captures, analyses, build receipts, original crate archives, source
ledgers and the six firmware images. Independent replay reproduced each stored
analysis and verified every retained artifact and all 135 source inputs per
case. The archive SHA-256 is
`cdab211f9851b3f1bfb8b09ea4f6d04e7c1ba1eb86a9997c724daca224fa963e`.
The separate
[ESP32-S3 artifact](https://github.com/yongkyuns/nxrs/actions/runs/41660612437/artifacts/13242671644)
contains successfully linked selected-owner reservation/publication images;
these are **build-only evidence**, with no physical execution claimed.
Intermediate dependency binaries are pruned after building; their Cargo source
paths and hashes remain in the bound build proof, while final image bytes are
retained for revalidation.

The tested fixed-priority contract therefore fails for the pinned Crossbeam
implementation in both reservation directions. Selecting Crossbeam alone, or
merely reversing producer and consumer priorities, is not an established remedy.
The successful controls validate the experiment and their named interleavings;
they do not qualify production progress. Physical-device replication and an
admitted scheduling or transfer contract that needs no priority repair remain
outstanding. These results make no latency, clock-calibration or WCET claim.

## Crossbeam experiment contract

Crossbeam is exactly `crossbeam-channel 0.5.17` with `crossbeam-utils 0.8.23`.
The [source audit](SOURCE-AUDIT.md) identifies the actual slot reservation,
publication and reclamation boundaries and the selected-wait registration
marker. Original crate archive checksums match the root lockfile. Both original
and selected complete source trees are inventoried, and receipt validation
reconstructs the selected bytes from authenticated original archives.

| Case | Operation under observation | Forced transition | Unrepaired priority relationship |
| --- | --- | --- | --- |
| `direct-reserved` | `Receiver::recv` | Producer tail reservation, before publication | Receiver 180; producer 120 |
| `direct-published` | `Receiver::recv` | Published-slot control | Receiver 180; producer 120 |
| `select-reserved` | Three-arm `select_biased!` | Important producer reserved; independent ordinary arm wakes the owner | Owner 180; waker 160; producer 120 |
| `select-published` | Three-arm `select_biased!` | Matching published-slot control | Owner 180; waker 160; producer 120 |
| `send-reserved` | `Sender::send` into a full ring | Consumer head reservation, before slot reclamation | Sender 180; consumer 120 |
| `send-reclaimed` | `Sender::send` into a full ring | Reclaimed-slot control | Sender 180; consumer 120 |

All six Crossbeam cases require one-core, flat NuttX, actual FIFO scheduling readback,
capacity eight, and a 16-byte checked payload. The observer runs at priority 220;
an independent watchdog runs at 240. FIFO policy is verified regardless of the
kernel's global round-robin setting. The reverse cases retain and check all nine
messages: the initial eight plus the sender's new message.

The selected owner uses the intended bias order **stop → important → ordinary**.
Before releasing the producer, the observer verifies both the actual three-arm
registration marker in Crossbeam's `run_select` and the owner's NuttX
`TSTATE_WAIT_SEM`. The producer's source hook wakes a separate priority-160
worker through a native notification. That worker publishes to the ordinary
arm. The owner consumes that independent message, then reenters the same biased
selection while the important producer is still preempted at its real boundary.
The report requires that independent message to have been consumed.

Hooks only report the transition and post the peer's gate. They never implement
a substitute queue, force a fake busy loop, wait for the observer, or alter
scheduling priorities. The hook's borrowed gate storage remains live until all
workers have joined.

## What a Crossbeam result means

`CHANNEL_PROGRESS` emits one `channel-progress-v1` metadata record and one result.
The analyzer derives the outcome from their observations:

| Outcome | Diagnostic valid? | Named no-repair contract satisfied? |
| --- | --- | --- |
| `stall-observed` | Yes, when both workers remain ready at their original priorities and bounded recovery delivers every message and joins them | **No** |
| `completed-without-repair` | Yes, when the operation, hook and applicable waker complete before recovery, with delivery and joins verified | Yes, for this named interleaving only |
| `host-functional-only` | Ordinary Linux API smoke only | No NuttX priority conclusion |
| Missing hook, wrong schedule, incomplete selection, timeout, payload error, incomplete join, mixed image or source | Rejected | No |

The observer takes its snapshot before diagnostic recovery. Recovery raises the
preempted low-priority worker to 200 and lowers the other worker to 100, both
below the observer. If setup or observation fails, cancellation wakes the stop
arm and releases the native gates before joining. The watchdog remains alive
through cleanup; an external capture deadline bounds a hung kernel or console.

The 100 ms observer interval establishes a diagnostic observation window. It is
not a measured upper bound on a stall. Calibrated cycle clocks are unnecessary
for this progress result. No record qualifies timing, physical latency, WCET,
or general production transport progress. A successful control cannot erase a
reserved-slot failure. Production admission requires a justified scheduling or
transfer contract that completes **without priority repair**, followed by the
remaining event-path, resource and physical-device checks.

The CI diagnostic job preserves a demonstrated stall as useful evidence. It
requires the three publication/reclamation controls to complete without repair.
Use the explicit acceptance option when checking a candidate contract:

```sh
python3 tests/channel-progress/analyze.py OUT/run/console.log \
  --build-receipt OUT/run/build.json \
  --output OUT/run/acceptance.json --require-no-repair
```

That command writes a valid negative diagnosis before returning exit status 2
for a recovered stall. Invalid evidence returns failure separately.

## Fixed six-case cohort gate

[`cohort.py`](cohort.py) replays all six required Crossbeam captures together.
Its root must contain exactly the six case directories named in the experiment
table, each with a `run/` capture. Keep the std primitive suite outside that root.
Each run must retain its raw console, build receipt, capture/session records and
stored analysis. The validator checks a fresh boot, one echoed command, exactly
two records and the final prompt; it revalidates the firmware/source artifacts
and compares the stored observations with a fresh analysis.

Build the cohort from one clean, frozen source revision. The cases must match
the complete resolved configuration, source and staged-input identities, platform,
target, SDK/compiler, NuttX/apps revisions, prepared std source and original
Crossbeam dependencies. The required mode-specific hook stage and image identity
remain distinct. Matching only a board label, a few configuration fields or
successful control cases is insufficient. Missing cases, extra case directories,
mixed source/configuration, duplicate builds and incomplete capture cleanup are
invalid evidence.

After the six-case build/capture loop below, retain the diagnostic and separately
apply the no-repair gate:

```sh
python3 tests/channel-progress/cohort.py target/channel-progress/mps2 \
  --output target/channel-progress/mps2/cohort.json
python3 tests/channel-progress/cohort.py target/channel-progress/mps2 \
  --output target/channel-progress/mps2/acceptance.json --require-no-repair
```

Both the individual analyzer and the cohort command distinguish these exits:

| Exit | Meaning |
| --- | --- |
| `0` | Valid diagnostic evidence; any explicitly requested gate also passed. Without `--require-no-repair`, a demonstrated progress failure can still be a valid diagnostic. |
| `2` | Valid evidence was written, but a requested acceptance gate refused it. A recovered reserved-slot stall therefore remains a valid negative result. |
| `1` | Evidence is missing, invalid, inconsistent or incomplete; no acceptance conclusion is available. |

The cohort no-repair gate requires **every required case** to complete without
repair. Published/reclaimed controls cannot erase a reserved-slot failure or
stand in for the missing reservation cases. CI retains a valid negative cohort
while treating invalid evidence as a workflow failure; it also requires the
three publication/reclamation controls to complete without repair.

A separate physical gate is available:

```sh
python3 tests/channel-progress/cohort.py PHYSICAL_COHORT_ROOT \
  --output PHYSICAL_COHORT_ROOT/physical-acceptance.json --require-physical
```

Current physical session records are **operator-asserted**. The validator cannot
independently authenticate device identity, flash/readback or exclusive ownership,
so `physical_execution_verified` and `physical_progress_qualified` remain false.
The physical gate therefore returns `2` for a structurally valid operator-declared
cohort, including one labelled `runtime_kind: "physical"`. A label never verifies
hardware execution. Historical session records remain readable with that limit
explicit; their stored bytes are not rewritten.

## Single-item std primitive suite

The [standard primitive source audit](STD-TRANSFER-AUDIT.md) describes the pinned
pthread-backed std mutex, condition variable and parker, and the existing
single-item ownership pattern being tested. The fixture uses a prewarmed
`Mutex<State { value: Option<OwnedMessage>, closed: bool }>` and one `Condvar`.
`OwnedMessage` wraps the checked 16-byte payload without `Copy` or `Clone`.
There is one slot; this suite does not implement a multi-queue selector.

| Case | Required acceptance observation |
| --- | --- |
| `mutex-admit-held` | The priority-180 caller attempts `try_lock` exactly once while the priority-120 holder owns the guard. It returns Busy before release and retains the original input. Accepted and transferred counts are both zero. |
| `mutex-admit-released` | Guard release precedes the single attempt; one input is accepted and transferred once. |
| `mutex-take-held` | A single attempt returns Busy before guard release; the complete protected value remains in the slot. Accepted and transferred counts are both zero. |
| `mutex-take-released` | Guard release precedes the single attempt; the previously stored value transfers once. |
| `condvar-wait-publish` | The caller enters an actual native condition wait before the producer publishes under the same mutex and notifies; one value transfers. |
| `condvar-publish-before-wait` | Publication precedes the caller's predicate check; the value transfers with zero condition-wait calls. |
| `condvar-close-wait` | An actual native condition wait precedes protected sticky closure and notification; the caller observes closure and joins without a fabricated transfer. |

Busy means the bounded primitive operation returned with ownership preserved.
It does not mean accepted, delivered or processed, and the fixture never retries
that attempt in a spin or yield loop. This result alone does not define a caller's
retention, overload or subsequent retry policy.

All seven native cases require one-core flat NuttX and actual FIFO policy and
priority readback: caller 180, holder 120, observer 220 and an independent watchdog
at 240. Mutex and condition-variable initialization precede worker release.
Recorded event ordering proves which operation returned before or after guard
release. For the two blocked condition-wait cases, a pre-wait marker alone cannot
pass: the observer must also see the caller's native `TSTATE_WAIT_SEM` before
allowing publication or closure. Notification follows guard release, and the
predicate is checked under that same mutex. Setup failure cancels the predicate
wait, releases setup gates and joins workers; the watchdog stays live through
the joins. A successful case performs no priority repair.

The suite emits two `CHANNEL_PROGRESS` records using
`std-transfer-progress-v1`, with the actual case in `mode` and
`build_mode: "std-mutex-suite"`. The common analyzer dispatches to
[`mutex_analyze.py`](mutex_analyze.py), which validates the one-attempt counts,
ownership, ordering, native wait evidence, unchanged priorities and complete
cleanup. A valid result can satisfy its named primitive contract while all
production-transport, timing and general priority-progress qualification flags
remain false. It does not establish a latency bound under intervening runnable
priorities or a priority-inheritance guarantee.

All seven std primitive cases passed these checks in
[CI run 41680413250](https://github.com/yongkyuns/nxrs/actions/runs/41680413250)
at head `0bc150ce28b3710da31525615e3daa0a4672a0a7` (merge checkout
`2cfb7f4593863abaa3857500cf9631558b07f9d1`). Independent replay of the retained
[ARM artifact](https://github.com/yongkyuns/nxrs/actions/runs/41680413250/artifacts/13253305906)
confirmed the seven primitive results and the original six Crossbeam outcomes.
These are virtual NuttX observations with the narrow primitive scope above.

## Equal-priority FIFO reservation suite

The [FIFO scheduling audit](FIFO-SCHEDULING-AUDIT.md) defines
`equal-fifo-run-to-block-v1`. All channel participants use the same **effective
FIFO priority 180** on one core. The observer runs at 220 and the independent
watchdog at 240. Channel endpoints remain live throughout the experiment.

The source argument is specific: at the pinned NuttX revision, a newly ready
equal-priority FIFO task is inserted behind the existing preempted task. The
audited direct try operations and receive-only biased selection finish an
acquired slot before invoking user code. A participant must not block, yield,
change its effective priority or perform arbitrary code while holding a reserved
slot. The contract also requires bounded runnable work between actual blocking
points and bounded interfering work; equal priorities alone do not establish
those application-level conditions.

| Case | Holder boundary | Peer path that must follow the actual release store |
| --- | --- | --- |
| `fifo-try-recv-reserved` | Producer owns an unpublished slot | The receiver's single `try_recv` entry |
| `fifo-try-recv-published` | Producer has published, before notifying | The receiver's single `try_recv` entry |
| `fifo-select-reserved` | Important producer owns an unpublished slot | The selected owner's return from its native context wait |
| `fifo-select-published` | Important producer has published, before notifying | The selected owner's return from its native context wait |
| `fifo-try-send-reserved` | Consumer owns an unreclaimed slot in a full ring | The sender's single `try_send` entry |
| `fifo-try-send-reclaimed` | Consumer has reclaimed, before notifying | The sender's single `try_send` entry |

Each case creates a real scheduling opportunity. At the selected source boundary,
the holder's hook wakes the higher-priority observer. For direct and reverse
cases the observer makes the peer runnable; it then verifies that both actors
are actually `READY` at 180 while the holder is still in its callback. The peer
has not entered its conflicting operation. In reserved cases the actual slot
release marker must still be absent.

For selected cases the owner first registers all three receive arms, in the order
**stop → important → ordinary**, and enters an observed native `WAIT_SEM` at 180.
The observer checks that this wait still exists when the producer reaches its
hook. It then performs a finite, capped polling episode with interrupts enabled,
without sleeping or yielding, until the kernel deadline makes the owner ready.
There is no higher-priority channel waker. Both equal-priority actors must be
ready before the observer relinquishes the CPU.

The observer blocks on a distinct, initially empty completion gate. On resuming,
the holder independently verifies that the observer is actually `WAIT_SEM` at
220. The real source markers must show publication or reclamation before the
peer enters its try operation or returns from the selected context wait. Thus
a sequential run with no ready competitor cannot satisfy the validator.

A kernel timeout makes the selected task runnable; it does not force Crossbeam's
default arm to win. The producer can publish and select the important operation
before the owner resumes. The suite records and checks the actual important
payload outcome, including explicit timeout counts. It never counts a timeout,
retry or ordinary-arm wake as important delivery. The reverse cases check the
entire nine-message sequence, including the initial eight messages.

Final native holds keep both actors alive for scheduling readback after their
operations return, followed by complete joins. No case invokes priority repair.
A failed setup, ordering check, deadline, payload or join is invalid evidence;
the watchdog and external capture deadline preserve failures without turning
cleanup into a positive result.

The `fifo-channel-progress-v1` records dispatch to
[`fifo_analyze.py`](fifo_analyze.py). Its scope is
`equal-fifo-reservation-progress`, with `build_mode: "fifo-channel-suite"` and
hook stage 5. [`fifo_cohort.py`](fifo_cohort.py) requires exactly these six cases,
each on a fresh boot of **one identical suite image and build receipt**. It
replays raw records, the echoed command, fresh boot, final prompt, build sources,
capture/session data and stored analysis; missing or mixed evidence is rejected.
The original unequal-priority cohort keeps its separate six-build contract.

The builder and replay require ten exact scheduler/semaphore source files from
the audited kernel revision. Independent retained copies are part of the
firmware's build identity. The builder checks the actual prepared kernel again
after compilation, and retains that source-selection proof. The prepared std
library must also match the nine primitive source pins and two SDK-specific
parker/dispatch pins. All older Crossbeam source recipes and build identities
retain their existing interpretation.

These requirements define the new suite's acceptance procedure. Report success
only with a retained target cohort from the corresponding source/build revision.
Passing the suite does not establish general wait-freedom, a latency bound,
production admission or physical execution. Crossbeam's notification locks run
after slot release and can still delay method return. Manual selected tokens,
selected-send expressions, ISR/signal reentry and unaudited endpoint or priority
changes are outside this contract. Owner fairness, overload, cancellation and
the complete work-to-block budget remain separate qualification obligations.

## Host validation

```sh
python3 -m unittest discover -s tests/channel-progress -p 'test_*.py' -v
cargo +1.90.0 fmt --check -p nxrs-channel-progress
cargo +1.90.0 test --locked -p nxrs-channel-progress
cargo +1.90.0 clippy --locked -p nxrs-channel-progress --all-targets -- -D warnings
CARGO_PROFILE_RELEASE_LTO=false cargo +1.90.0 build --locked --release -p nxrs-channel-progress
timeout 30 target/release/channel-progress host-functional > host.log
python3 tests/channel-progress/analyze.py host.log
python3 tools/check-architecture.py
```

Host execution refuses all **19 native cases**: the six original Crossbeam modes,
seven std primitive modes and six equal-FIFO modes. Its `host-functional` smoke result cannot
pass `--require-no-repair`. Host primitive tests check retained ownership,
publication at the wait boundary, publication before waiting and sticky close;
they do not qualify NuttX progress. Negative controls cover altered source bytes,
configuration and artifacts, fabricated progress/ownership labels, repeated
try-lock counts, wrong schedules, missing registered/native wait evidence, mixed
builds, incomplete cohorts, truncated captures and cleanup.

## Build and run on NuttX

Use the existing nxrs ARM or ESP tool prerequisites. The ARM SDK is
`nightly-2025-09-15`; ESP uses the existing hash-pinned `esp-1.90.0.0` SDK.
Check out the pinned NuttX and apps submodules. Each Crossbeam mode needs a
**new** output directory. Build and capture the six ARM cases sequentially:

```sh
for mode in direct-reserved direct-published select-reserved select-published send-reserved send-reclaimed; do
  out="target/channel-progress/mps2/$mode"
  python3 tests/channel-progress/build.py --platform mps2-an521-mock --mode "$mode" --out "$out"
  python3 tests/channel-progress/capture.py qemu --build-directory "$out" --mode "$mode" --output "$out/run"
done
```

Build the std primitive suite once, then capture each of its seven commands on
a separate fresh boot. Each capture retains the same clean suite image/build
identity and its own case-specific transcript:

```sh
out="target/channel-progress/std-transfer/mps2"
python3 tests/channel-progress/build.py \
  --platform mps2-an521-mock --mode std-mutex-suite --out "$out"
for mode in mutex-admit-held mutex-admit-released mutex-take-held mutex-take-released condvar-wait-publish condvar-publish-before-wait condvar-close-wait; do
  run="$out/cases/$mode/run"
  python3 tests/channel-progress/capture.py qemu \
    --build-directory "$out" --mode "$mode" --output "$run"
  python3 tests/channel-progress/analyze.py "$run/console.log" \
    --build-receipt "$run/build.json" --output "$run/acceptance.json" --require-no-repair
done
```

`std-mutex-suite` is a build mode, not an executable case name. Its Crossbeam
hook stage is zero and its Crossbeam source is clean; it adds no std source
hooks. The prepared std library is checked against the audited primitive source
hashes for the selected SDK before the firmware build and again on receipt
replay; changed or missing required source files are rejected. The seven
commands test the actual std primitives in that one image.
They are analyzed individually and are not inputs to the fixed six-case
Crossbeam cohort gate.

Build the FIFO suite once and capture its six cases on fresh boots:

```sh
out="target/channel-progress/fifo/mps2"
python3 tests/channel-progress/build.py \
  --platform mps2-an521-mock --mode fifo-channel-suite --out "$out"
for mode in fifo-try-recv-reserved fifo-try-recv-published fifo-select-reserved fifo-select-published fifo-try-send-reserved fifo-try-send-reclaimed; do
  run="$out/cases/$mode/run"
  python3 tests/channel-progress/capture.py qemu \
    --build-directory "$out" --mode "$mode" --output "$run"
  python3 tests/channel-progress/analyze.py "$run/console.log" \
    --build-receipt "$run/build.json" --output "$run/acceptance.json" --require-no-repair
done
python3 tests/channel-progress/fifo_cohort.py "$out/cases" \
  --output "$out/cohort.json" --require-no-repair
```

`fifo-channel-suite` is a build mode, not a runnable command. Its private
`fifo-ordered` Crossbeam recipe marks all four slot boundaries, selected
registration and the return from the context wait. The FIFO cohort also offers
`--require-physical`, which refuses operator-only labels just as the original
cohort does. Keep all three families in their separate output roots.

`build.py` copies the fixture, its native ABI and selected platform metadata into
a new minimal Cargo workspace. Only that workspace gets a generated patch
lockfile. Private Crossbeam and Rust std trees, compiler selection, Cargo
artifacts and final ELF/image hashes are joined in `build.json`. Actual Cargo
compiler-artifact messages must name both selected Crossbeam source paths.
Rust std receives only the established common ABI patches; its channel source
is unchanged. The root lockfile, installed SDK, shared Cargo source cache and
P0 directories remain independent.

`run/` retains all referenced artifacts needed to revalidate its build receipt,
including original crate archives, source ledgers, selected-source proof,
configuration and firmware. Raw logs and failure receipts survive rejection.
QEMU is explicitly reported as virtual execution; it is not a physical run.

For the physical ESP32-S3, build with `--platform esp32s3-service-footprint`.
The current ESP CI job is configured to build exactly `select-reserved`,
`select-published`, `std-mutex-suite` and `fifo-channel-suite`, retaining four images for later
hardware sessions. It does not flash or execute a physical board. The historical
ESP artifact identified above still contains only its original two selected-owner
images. Other Crossbeam cases use the same build command with their own named
modes; all seven primitive cases share the suite build mode.

Only one workstream may own the board at a time. After exclusive ownership and
flashing the verified selected image, record a `channel-progress-session-v1`
JSON with `runtime_kind: "physical"`, the `board`, `board_serial`,
`measurement_session`, `active_cores: 1`, exact `build_id` and `image_sha256` from
the build receipt, and retained `flash_verification` details. These hardware and
ownership descriptions are operator assertions; neither the session validator
nor matching labels independently verify them. Then capture one command after
a fresh reset:

```sh
python3 tests/channel-progress/capture.py physical \
  --build-directory OUT --mode select-reserved --port /dev/ttyUSB0 \
  --session physical-session.json --output OUT/run
```

The physical session bytes are frozen before opening the port and retained even
when capture fails. Boot ROM digest warnings are recorded. Such a warning must
be resolved in the broader firmware/physical qualification; neither this fixture
nor a user-supplied session label certifies a production image. The analysis
records `execution_evidence: "operator-asserted"` and keeps
`physical_execution_verified: false` even for a correctly bound physical-session
record. QEMU remains explicitly virtual.

## Scope and outstanding qualification

The implementation supplies the P1 Crossbeam source witnesses, complete-cohort
checks and narrow std primitive ownership/wait witnesses. Source audits and host
tests alone do not establish target progress. Archive concrete target observations
with their own build/source identity; retain physical qualification as a separate
gate. A valid Busy retention result is not a delivery result or a remedy for the
three-class selected owner. No changes to the intended production transport or
scheduling contract are authorized by a diagnostic stall alone.
