# A finite characterization campaign

This campaign prepares evidence for the questions in
[Characterizing Zephyr, NuttX and NXRS](../../docs/rtos-characterization.md).
It combines existing fixture protocols; it is not another runtime or benchmark
implementation. `campaign.py` performs read-only build inspection and replay.
It never flashes, resets, or opens a serial port.

The unit of comparison is a required behavior on one board. The NuttX native
queue control and NuttX std image share std threads and the same kernel inputs.
The Zephyr image uses native threads and queues. The ordinary owner witness
uses the real current Rust owner/controller with synthetic Wi-Fi input; it is
not a physical radio test. Embassy is an async RAM reference in the native
event-service fixture, not a fourth arm of the std messaging benchmark.

## Fixed first-campaign scope

| Criterion | Executable evidence | Remaining boundary |
| --- | --- | --- |
| Physical SRAM and allocation scaling | Four ordinary Rust binaries; native C/Rust, Zephyr and Embassy RAM controls; ELF section ledgers; construction, wait, full, drain and release snapshots | Observed stack use is not a worst-case bound; arenas and suballocations must not be counted twice |
| Useful delivery under load | All 16 MPSC cases, three transports, three passes on each selected board | Keep offered, admitted, rejected and delivered work together; no universal platform score |
| CPU cost | Four focal cases with separate S3 CPU-accounted images | Participant thread charges have different NuttX/Zephyr IRQ attribution; not total MCU activity or energy |
| Interrupt response under queue work | Four focal cases with a separate S3 timer-alarm observer | Normal-priority alarm deadline to probe ISR entry; not isolated lock duration or externally calibrated sensor latency |
| Priority progress and disconnection | Current std blocked delivery, unequal-priority full rejection/drain, blocked receiver disconnect and join | Finite witnesses, not a proof for all schedules or all std APIs |
| Selection, overload and lifecycle | Actual owner independent inputs, isolated stop while data queues are full, fairness/deadline, retained failure and retry | Synthetic provider; real driver callbacks and electrical failure need their own qualification |
| Portability | Same fixture and owner logic compiles and runs on both selected boards; native bridges remain at fixture boundaries | A target build is not target execution; real device substitution is separate |
| Reproducibility and diagnosis | Exact source/image/configuration identities; native kernel control; strict saved-transcript replay; complete run census | Matching inputs does not certify final hot-path placement or future SDK upgrades |

S3 CPU and IRQ diagnostics use `paced-p4-b16-q8`, `stream-p4-b16-q8`,
`stream-p4-b64-q8`, and `backpressure-p4-b16-q8`. They test quiet offered load,
ordinary traffic, larger copies, and a verified full-queue wait. Do not pool
these observations with primary images. The complete 16-case diagnostic matrix
remains available through the MPSC runner if the first campaign exposes a
specific question. Pico CPU accounting and the alarm observer are explicitly
unavailable in this campaign.

With both boards selected, the fixed census is **455 evidence entries / 464
fresh boots**: 288 primary MPSC captures, 36 CPU captures, 36 IRQ captures, 14
owner cases, 78 ordinary RAM captures, and three native RAM matrices containing
four image boots each. S3 alone is 304 entries / 313 boots; Pico alone is 151
entries / boots. This is a complete planned census, not a number of completed
physical tests. Run a short pilot before committing to the measured census.
Firmware permits one ordinary Rust main per boot; shell exit is not a substitute
for rebooting. Grouping flashes must not eliminate those boot boundaries.

## Prepare all artifacts first

Use a clean committed checkout, the pinned fixture toolchains, initialized
submodules, and fresh output directories. Build from the same source revision.
Every failed build remains a failed attempt; do not relabel its receipt.

1. Build all three MPSC transports with `--observer primary` on each board.
   On S3 also build all three with `--observer cpu` and `--observer irq`.
   Follow the [MPSC tool and build recipe](../mpsc-comparison/README.md).
2. Build one [owner qualification](../owner-qualification/README.md) image per
   board. Each image contains all seven cases. S3 uses
   `esp32s3-ram-footprint`; Pico uses `pico2w-std-qualification`.
3. On S3 build the four [ordinary RAM binaries](../ram-footprint-std/README.md)
   with the same profile, main stack, ABI and optimization policy. Preserve the
   generated native `config.h` as well as `resolved.config`.
4. Build the four [native RAM controls](../event-services-comparison/RAM.md)
   with 4,096-byte worker requests and an 8,192-byte Embassy shared reservation,
   lean instrumentation, stack observations and 1 ms timers. The dedicated
   `esp32s3-ram-native-baseline` prepares the common NuttX C/Rust substrate.
   Smaller stack candidates follow physical baseline observations and a declared
   margin; a successful smaller link is insufficient.

Create a private input manifest and fill in the actual completed build paths:

```sh
python3 tests/rtos-characterization/campaign.py template \
  --out target/characterization-inputs.json
python3 tests/rtos-characterization/campaign.py preflight \
  --inputs target/characterization-inputs.json \
  --out target/characterization-ready.json
```

`template --board esp32s3` or `--board pico2w` explicitly scopes one board.
An S3-only campaign does not establish portability across MCU families. Paths
in the input manifest are absolute or relative to the repository root. Fill
`readelf` with the actual Xtensa readelf executable. The preflight checks every
selected build, the three-way MPSC source/compiler cohort, and the NuttX kernel
control separately for each observer. It freezes identities and the exact run
list under `campaign_id`. Missing images fail the gate. Successful preflight
means the artifact set is ready for board execution; every hardware, placement
and production qualification flag remains false.

Preserve the checkout and toolchain together with the artifacts. MPSC replay
requires the matching source inventory; a different checkout is a new cohort.
Owner captures retain their own source/artifact snapshot. Ordinary RAM builds
must have canonical clean-source provenance because their native frontend hash
receipt does not enumerate every Rust source file.

## Execute in stages

Start with one owner progress case and one MPSC primary stream capture per
transport on each board. On S3 add one CPU and one IRQ capture per transport,
one ordinary-RAM worker case, and one native RAM matrix. These are pilots:
verify USB recovery, console protocol, scheduling readback, stack headroom,
observer availability, and cleanup before beginning the three measured passes.
Keep pilot directories separate from the measured ledger.

The `runs` array in the readiness file is the measured order. It runs correctness
witnesses first, rotates the three MPSC arms across passes, and keeps each
observer separate. Complete every selected arm for a case/pass before drawing
a comparison. Stop on a failed correctness, overflow, guard, or cleanup check;
retain the failed transcript and fix the cause before starting a new cohort.

For MPSC and owner rows, use the matching fixture's capture command and private
session JSON. Its exact image hash must match the readiness file. Retain the
fresh boot, full response, terminal status and post-completion tail. The capture
tools do not flash or restore boards; use the established board tools and
verified original backup, then restore and verify it after the session.
Add the exact `campaign_id` and `run_id` to each MPSC/owner session before
capture; the fixture preserves and hashes these additional fields.

For an ordinary-RAM row, flash its `build` image and capture exactly one fresh
NSH boot. Enter the invocation selected by `case`, `owners` and `stack_bytes`:

```text
if ramprobe services 20 4096
then echo RAM_COMMAND_EXIT status=0
else echo RAM_COMMAND_EXIT status=1
fi
```

The baseline uses `if ramprobe` without arguments. Preserve the entire console
through the next prompt and a late-fault tail. Save a session JSON with schema
`ordinary-ram-session-v1`, the exact `campaign_id`, `run_id`, image and console
SHA-256 values, and true `fresh_boot`, `flash_verified`,
`fresh_backup_verified` operator attestations. These claims are not generated
by the parser and must describe the actual session. The fixture README gives
the standalone replay command and accounting limits.

For each native-RAM row, invoke `ram_matrix.py` with **`--runs 1`** in a new
directory. Run all four images. The collector verifies and restores the private
backup, including on failure. Three separate collections provide three image
boots per implementation. `--runs 3` in one collection instead repeats profiles
within each image's single boot and does not satisfy this campaign. Within an
image, profile observations remain warmed and cumulative; ordinary RAM supplies
the separate first-use controls.
Save a companion session with schema `native-ram-session-v1`, the exact
`campaign_id`, `run_id`, `matrix_sha256`, and a true `fresh_boots` attestation.
This binds each independent collection to the selected campaign pass.

## Complete replay and reporting

Create a private ledger of this form, with exactly one entry per readiness run:

```json
{
  "schema": "rtos-characterization-ledger-v1",
  "campaign_id": "FROM_THE_READY_FILE",
  "runs": [
    {"id": "EXACT_RUN_ID", "evidence": "/private/path/capture-directory"},
    {"id": "ORDINARY_RAM_RUN_ID", "evidence": "/private/path/console.log",
     "session": "/private/path/session.json"},
    {"id": "esp32s3.ramnative.p1", "evidence": "/private/path/ram-matrix.json",
     "session": "/private/path/native-session.json"}
  ],
  "restoration": {
    "esp32s3": {"verified": true, "evidence": "private restore/readback record"},
    "pico2w": {"verified": true, "evidence": "private restore/readback record"}
  }
}
```

The illustrated entries are examples, not a complete ledger. `evidence` is a
capture directory for MPSC/owner, one console file for ordinary RAM, or the
collector's `ram-matrix.json` for native RAM. Use only selected boards in
`restoration`.

```sh
python3 tests/rtos-characterization/campaign.py replay \
  --ready target/characterization-ready.json \
  --ledger /private/path/characterization-ledger.json \
  --out target/characterization-replayed.json
```

Replay rechecks the frozen artifacts, requires the full census, rejects missing,
duplicate, reused or misassigned captures, reruns each fixture's parser and
creates three-way MPSC comparisons separately for each board/observer. It does
not pool samples or promote operator-attested execution to externally calibrated
timing. Report resource budgets, useful delivery/load envelopes, scope-labeled
CPU and IRQ observations, lifecycle outcomes, and integration limitations.

Real sensor acquisition, device substitution/multiplicity, independent clock
calibration, power and protection modes remain explicitly separate. The
[existing IMU qualification](../nuttx-std/imu-qualification/README.md) supplies
a concrete optional hardware track; synthetic owner inputs do not replace it.
Select its exact sensor/wiring profile before execution. Never infer power,
complete POSIX compatibility, fault isolation or arbitrary command restart
support from this campaign.
