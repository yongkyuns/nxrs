# Production time API qualification

This fixture exercises `hal/time` and the synchronous `service/time-sync`
controller through their public Rust APIs. Ordinary wall-clock and monotonic
reads use `SystemTime` and `Instant`; no fixture C wrapper sets or adjusts time.
The only fixture native code observes the independent APB timer and applies
the already documented QEMU SysTick constructor correction.

The explicit `mps2-an521-time-sync` qualification profile binds `time` to NuttX.
Both image variants use the same software timekeeping configuration, public
adjustment API, 100 microsecond scheduler tick, release settings and toolchain.
The additive `controller` Cargo feature includes the production crates and
their target contract checks. With that feature disabled, the same Rust entry
and observation loop form a `std` baseline. It does not select a different HAL
implementation.

## Build and capture

Use the pinned toolchain setup in the
[clock qualification instructions](../clock-control/README.md#reproduce), and
run from the repository root. Each output must be new. Build sequentially on
memory-constrained hosts:

```sh
python3 tests/nuttx-std/time-sync/workflow.py build \
  --variant baseline --out /dev/shm/nxrs-time-sync/baseline
python3 tests/nuttx-std/time-sync/workflow.py run \
  --build /dev/shm/nxrs-time-sync/baseline
python3 tests/nuttx-std/time-sync/workflow.py build \
  --variant controller --out /dev/shm/nxrs-time-sync/controller
python3 tests/nuttx-std/time-sync/workflow.py run \
  --build /dev/shm/nxrs-time-sync/controller
python3 tests/nuttx-std/time-sync/workflow.py compare \
  --baseline /dev/shm/nxrs-time-sync/baseline \
  --controller /dev/shm/nxrs-time-sync/controller \
  --out /dev/shm/nxrs-time-sync/footprint.json
```

The workflow invokes the normal `cargo nxrs build` frontend. It retains the
resolved profile, selected dependency graph, effective configuration, target
ABI report, native and std patch ledgers, compiler identities, linked image
sections, raw image and source hashes. The enabled build must resolve both
production time crates; the baseline must resolve neither. Capture refuses a
stale source or changed image, starts three fresh QEMU kernels by default, and
requires a complete strict transcript and successful NSH status from each.

## Observed contracts

The common Rust observation loop brackets `SystemTime` and `Instant` reads with
the independent APB counter. It checks every adjacent pair for reversal and a
correction jump; the allowed bound is two scheduler ticks, measured read spans
and the 500 ppm elapsed correction budget. The bound must remain below the
previous backend's 500 microsecond staircase. The transcript records extrema
and sample counts rather than every individual read.

The production HAL checks include exclusive admission, fractional pre-epoch
normalization, positive and negative corrections, query/cancel semantics,
signed whole-second residuals, rejection without state mutation, and explicit
lease and set-time lifecycle behavior. Controller checks use the same real
HAL. Policy failure combinations remain covered by the service's portable
tests with explicit fake controls.

The comparison reports linked flash sections, unpadded binary length and
static data/BSS independently. It checks identical effective NuttX
configuration, compiler identities, std patch identity, source set and native
environment before subtracting. This is a complete Rust integration-image
delta including the enabled assertions, not a measurement isolating only the
service's machine code. Runtime heap and task-stack usage are not inferred
from linked sections.

## Retained results

The [MPS2 100 microsecond evidence](results/mps2-100us/evidence.json) contains
three successful fresh boots for each variant. Both normal builds passed 134
target ABI checks. Across the three controller runs, the dense Rust loops
checked 1,330,106 adjacent reads without a reversal or correction-jump
assertion. The largest measured read span was 44.35 microseconds; the largest
allowed adjacent-pair bound was 263.516 microseconds. Every synchronized
status had a measured error bound below the configured one millisecond;
the largest was 438.005 microseconds.

Initial seeding returned `Checking`, and a subsequent real clock read verified
`Synchronized`. Positive correction, replacement of an active positive
correction with a fresh negative offset, stale/reordered rejection, holdover,
fresh-source recovery, and the suspend/resume epoch boundary all passed.
The target measured `Controller<C, 1>` at 328 bytes. That is the object's
layout, not a measurement of peak stack or heap use.

| Measurement | Rust std baseline | Production APIs enabled | Difference |
| --- | ---: | ---: | ---: |
| Linked flash sections | 299,630 B | 319,642 B | +20,012 B |
| Unpadded image | 299,632 B | 319,644 B | +20,012 B |
| Static data + BSS | 10,828 B | 10,828 B | 0 B |

The [complete section comparison](results/mps2-100us/footprint.json) retains
the individual text, unwind, data and BSS sizes. The enabled variant includes
its additional contract checks, and neither variant starts a controller
worker or queue.

## Replay and build-input evidence

The supplemental [evidence verifier](evidence/verify.py) checks the original
normal-build receipts and actual copied frontend binaries before retaining
the results. Every receipt input matches between the two builds except the
intentional Cargo feature list: `[]` versus `["controller"]`. This includes
the exact compiled build frontend, 273 build inputs, 4,416 SDK inputs, native
compiler, profile fingerprint, source revisions and linker/native options.
These receipts existed when the images were built. The supplemental check
does not rewrite or relabel the original build or capture records.

The published package retains the complete common native input data once,
plus explicit per-build feature differences and canonical input hashes in
clearly labeled projections. Full original native receipts and ELF, raw image
and frontend binaries remain in their local build outputs; their hashes,
lengths and paths are recorded in `evidence.json` instead of adding their
generated bytes to Git.

```sh
python3 tests/nuttx-std/time-sync/evidence/verify.py verify \
  tests/nuttx-std/time-sync/results/mps2-100us
python3 -m unittest discover -s tests/nuttx-std/time-sync -p 'test_*.py'
python3 -m unittest discover -s tests/nuttx-std/time-sync/evidence -p 'test_*.py'
```

Replay checks the unchanged captured source manifest, six raw console logs,
strict oracle arithmetic, original build/capture linkage, retained native
input projections and footprint arithmetic. Add `--check-local-binaries` to
the verifier when the original build outputs are present; that also rehashes
the actual binaries and full original receipts. Its output explicitly states
whether that local rehash was performed. The eight fixture/oracle tests and
four supplemental evidence tests include refusal controls for stale source,
changed images, mismatched compilers, unrelated native input changes,
modified projections and invalid status/measurement transcripts.

## Limits

QEMU runs with `-icount shift=3,sleep=off`: each guest instruction advances
virtual time by eight nanoseconds. This supplies a repeatable virtual-time
contract test, not a physical timing or cycle-accurate measurement. The APB
timer wraps after about 214 seconds; the fixture enforces a shorter 60-second
lifetime. The selected image has no RTC. These tests do not establish RTC
write success, retention, deep-sleep behavior, oscillator accuracy, GNSS/NTP
source quality, protected-build behavior or support on another board. The
older [backend qualification](../time-adjustment/README.md) remains separate
and unchanged.
