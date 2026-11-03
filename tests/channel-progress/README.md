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

## Experiment contract

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

All native cases require one-core, flat NuttX, actual FIFO scheduling readback,
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

## What a result means

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

Host execution refuses the six NuttX witness modes. Its smoke result cannot
pass `--require-no-repair`. Negative controls cover altered source bytes,
configuration and artifacts, fabricated progress labels, wrong schedules,
missing registered-wait evidence, mixed builds, truncated captures and cleanup.

## Build and run on NuttX

Use the existing nxrs ARM or ESP tool prerequisites. The ARM SDK is
`nightly-2025-09-15`; ESP uses the existing hash-pinned `esp-1.90.0.0` SDK.
Check out the pinned NuttX and apps submodules. Each mode needs a **new** output
directory. Build and capture the six ARM cases sequentially:

```sh
for mode in direct-reserved direct-published select-reserved select-published send-reserved send-reclaimed; do
  out="target/channel-progress/mps2/$mode"
  python3 tests/channel-progress/build.py --platform mps2-an521-mock --mode "$mode" --out "$out"
  python3 tests/channel-progress/capture.py qemu --build-directory "$out" --mode "$mode" --output "$out/run"
done
```

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
The CI target job builds the two selected-owner images and preserves them for
later hardware sessions; it does not flash or execute a physical board. Other
cases use the same build command with their own named modes.

Only one workstream may own the board at a time. After exclusive ownership and
flashing the verified selected image, record a `channel-progress-session-v1`
JSON with `runtime_kind: "physical"`, the actual `board`, exact `build_id` and
`image_sha256` from the build receipt, and retained flash-verification details.
Then capture one command after a fresh reset:

```sh
python3 tests/channel-progress/capture.py physical \
  --build-directory OUT --mode select-reserved --port /dev/ttyUSB0 \
  --session physical-session.json --output OUT/run
```

The physical session bytes are frozen before opening the port and retained even
when capture fails. Boot ROM digest warnings are recorded. Such a warning must
be resolved in the broader firmware/physical qualification; neither this fixture
nor a user-supplied session label certifies a production image.

## Scope and outstanding qualification

The implementation supplies the P1 source witnesses, exact-path experiments,
bounded recovery and acceptance checks. Source audits and host tests alone do
not establish target progress. Archive concrete target observations with their
own build/source identity; retain physical qualification as a separate gate.
No changes to the intended production transport or scheduling contract are
authorized by a diagnostic stall alone.
