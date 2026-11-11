# Portable MPSC comparison

Build **`nxrs-mpsc-comparison`**, executable **`mpsc-comparison`**, firmware command
**`mpsc_perf`**. This qualification fixture uses one Rust workload with three
separately linked transport choices:

| Runtime / backend | Transport | Worker execution |
| --- | --- | --- |
| `nuttx` / `std` | Actual `std::sync::mpsc::sync_channel` | `std::thread` |
| `nuttx` / `native` | Public POSIX `mq_*` through a target-header adapter | The same `std::thread` implementation |
| `zephyr` / `native` | Native `k_msgq` through a target-header adapter | Native Zephyr threads |

The NuttX pair substitutes the queue under the same Rust workload, startup and
thread model. It is not a native-C baseline or a measurement of all NXRS layers.
The Zephyr arm does not port Rust std to Zephyr. The
[broader characterization](../../docs/rtos-characterization.md) explains the
other criteria and experiments.

`rust/` owns workload scheduling, message validation, endpoint retirement and
joined cleanup. `sdk_{nuttx,zephyr}.c` translate native operations;
`boards/` owns clocks, console setup and native startup. NuttX profiles are in
`tests/profiles/{esp32s3,pico2w}-mpsc.toml`. No production HAL API or
benchmark Cargo feature is added.

## Build

Use Python 3.11+, Cargo, CMake/Ninja, pyelftools for Pico placement and pyserial
for physical capture. Initialize the pinned NuttX/apps submodules. The normal
`cargo nxrs` frontend prepares its private patched Rust SDK; installed toolchains
are unchanged. Copy [toolchains.example.toml](toolchains.example.toml) outside the
checkout, then fill in your existing tool paths. Zephyr is pinned to 4.5.0;
module revisions and board contracts are in [boards.toml](boards.toml). The Rust
compiler must match the NuttX SDK: ESP 1.90.0.0 for S3, nightly-2025-09-15 for Pico.
The S3 `nuttx-tools` directory must include the normal frontend's
`downloads.sha256`; `path` can add existing native build tools such as genromfs.

From the repository root, using that Python environment:

```sh
python tests/mpsc-comparison/bench.py build --board pico2w --runtime nuttx \
  --backend std --tools /path/to/local-tools.toml --out target/mpsc/pico2w/nuttx-std
python tests/mpsc-comparison/bench.py build --board pico2w --runtime nuttx \
  --backend native --tools /path/to/local-tools.toml --out target/mpsc/pico2w/nuttx-native
python tests/mpsc-comparison/bench.py build --board pico2w --runtime zephyr \
  --tools /path/to/local-tools.toml --out target/mpsc/pico2w/zephyr
```

Use `--board esp32s3` and separate output paths for the S3 pair. Output directories
must be new. `build.json` identifies the exact image and ELF and binds sources,
resolved configuration, compiler versions and relevant native build evidence.
Failed builds remain failed receipts. Capture/replay reject changed inputs or
artifacts; replay historical captures from their matching source checkout.

Backend and observer selection are fixed at build time and bound into the v4 receipt. The
final ELF must retain the native queue adapter only in a native-backend image.
Both NuttX images use the same profile, including enabled POSIX queues and
support for every tested payload. This is a controlled substitution, not the
minimum configuration for a standalone std-only application.

Before collecting the NuttX control pair, validate the shared substrate:

```sh
python tests/mpsc-comparison/bench.py check-control \
  --nuttx-build target/mpsc/pico2w/nuttx-std \
  --nuttx-native-build target/mpsc/pico2w/nuttx-native
```

This checks the resolved configuration, compiler identities, patch receipts and
every non-application kernel archive. Raw archive hashes are retained. A separate
per-member fingerprint strips debug sections only, preserving symbols,
relocations, code, data and member order, including every occurrence of duplicate
member names. The NuttX builds use the pinned NuttX commit time for
`SOURCE_DATE_EPOCH` and map their owned build directories to a common prefix
before compilation. Timestamp and assertion-path bytes therefore stay in the
objects with reproducible values; the comparison does not remove them.
Differing debug paths do not imply a different kernel implementation, but
runtime-byte or section-attribute differences still fail the control.
Application archives and final image addresses differ
by design; a successful check does not establish matched final hot-path placement.

Both kernels use one core, FIFO workers of equal effective priority, a 100 Hz
tick, no timeslicing, no radio or external RAM, an 8 KiB controller and 4 KiB
worker stacks. Pico uses Rust Oz/fat LTO; S3 uses O2/no LTO. Compare kernels on
the **same board**, not absolute numbers across these optimization policies.
Timing uses S3's 16 MHz SYSTIMER scaled to 240 MHz units, or Pico's 1 MHz TIMER0,
identically on both kernels; these are wall counters, not CPU-time measurements.
The full image includes the sample array (62,720 bytes), release records, stack reservations
and SDK resources; it is not the production channel's memory footprint.

Pico hot operations are relocated by changing input section names only, with
instruction-byte checks, then linked through each OS's startup SRAM copy path.
Early memory routines stay in flash on both Pico kernels. For NuttX, the
placement step redirects compiler-generated memory calls only in the reset
object and verifies the reset call targets in the final ELF. S3 uses the shared
fixture IRAM fragment alongside each kernel's native wait/scheduler placement. Build receipts
are not hardware qualification: inspect linked hot operations and indirect
callees before claiming a new cohort has matched placement. The linked-image
audit on both boards records direct paths, noninternal memory boundaries,
missing roots and unresolved or indirect calls. Existing Pico std and Zephyr
SRAM gates remain required. The newly exposed NuttX native path records its
additional dependencies as diagnostic evidence; it does not silently promote
all queue/allocator/descriptor code or declare placement matched.
The ESP ROM-prefix digest covers the visible RAM prefix, not hidden flash
segments, and is not firmware authentication. The carried NuttX loader fix
skips that declared digest before reading the next segment; flag-zero images
retain their existing layout.

## Run and replay

The entry point deliberately **does not flash, back up, reset or restore** boards.
Use the board's established tools to create a fresh private full-flash backup,
verify it, flash the receipt's `image_file`, independently verify flashing, and
reset into that image before every capture. Restore and verify the original
image when the campaign ends, including after failures. Pico USB recovery uses
[`tools/pico-bootloader.py`](../../tools/pico-bootloader.py); an interrupted Zephyr
report may need its CDC output drained while holding 1200 baud before recovery.

Create a separate private session file for each capture:

```json
{
  "schema": "mpsc-comparison-session-v1",
  "image_sha256": "SHA256_OF_THE_RECEIPT_IMAGE",
  "fresh_backup_verified": true,
  "flash_verified": true,
  "fresh_boot": true,
  "pass": 1
}
```

These are operator attestations, not measurements made by the capture tool.
The firmware accepts one workload per boot and repeats `MPSC_READY` while
waiting for its command; open capture within 120 seconds after reset. No NSH
prefix is needed: the standalone image starts `mpsc_perf` directly.

```sh
python tests/mpsc-comparison/bench.py plan --passes 3 --include-native-control \
  > /path/to/private-plan.json
python tests/mpsc-comparison/bench.py run \
  --build-directory target/mpsc/pico2w/nuttx-std --port /dev/your-console \
  --case stream-p4-b16-q8 --session /path/to/session.json \
  --output target/mpsc/captures/pass-1-nuttx-std-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py replay \
  --build-directory target/mpsc/pico2w/nuttx-std \
  --output target/mpsc/captures/pass-1-nuttx-std-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py compare \
  --nuttx-build target/mpsc/pico2w/nuttx-std \
  --nuttx-native-build target/mpsc/pico2w/nuttx-native \
  --zephyr-build target/mpsc/pico2w/zephyr \
  target/mpsc/captures/pass-1-*
```

The plan has 16 cases per implementation: sustained throughput; ordinary stream; paced,
burst and overload arrivals; verified full-queue backpressure; 64-byte messages;
and a 64-slot stream control, each with one/four producers. With the native
control enabled, it rotates the three implementations across passes, giving
each one every position over three passes. The default two-implementation plan
still alternates order. Start with a pilot per implementation, then use fresh
boots. Capture every selected implementation before comparing a case/pass; the
single capture example above is only one member. Repeat a focal case as another
pass rather than pooling samples. Comparison accepts either native comparator
or both, and rejects missing or duplicate implementation/case/pass records.

Strict replay checks complete offered/admitted/rejected/delivered accounting,
payload checksum, per-producer FIFO, blocking proof, scheduling, stacks and
joined cleanup. Report throughput, delivery/admission/release tails and losses
together. Normal-case loss gates remain explicit. CPU accounting is unavailable
in these primary images and is reported as unavailable, never zero. Comparison
requires the same board, source inventory, contract and Rust/C compilers, pairs
each case within its pass and never pools distributions. Passing replay alone
does not establish a product deadline or physical timing qualification.

The wait observer distinguishes the evidence: native transports observe
the measured queue object; the NuttX std transport observes semaphore-blocked
state with the engine's phase protocol; host checks make no native scheduling
claim. The NuttX native observer also checks the appropriate send/receive wait
state. The native adapter uses public `mq_*`, preserving native error and
cancellation-point behavior, but this workload does not request cancellation
or qualify timed operations and endpoint disconnection. Nonblocking full sends
return the original payload; interruption and other failures are not hidden as
full-queue rejection or retried silently. Native message allocation and pools
remain part of the implementation cost.

## Separate CPU-accounted diagnostic

The default `--observer primary` keeps runtime accounting disabled. On
ESP32-S3, build separate images with `--observer cpu`, applying that option to
each selected runtime/backend and using new output directories:

```sh
python tests/mpsc-comparison/bench.py build --board esp32s3 --runtime nuttx \
  --backend std --observer cpu --tools /path/to/local-tools.toml \
  --out target/mpsc/esp32s3/cpu-nuttx-std
python tests/mpsc-comparison/bench.py plan --passes 3 --include-native-control \
  --observer cpu > /path/to/private-cpu-plan.json
```

The same `run`, `replay`, `compare` and `check-control` commands use the observer
bound to the build. Capture refuses a CPU image without complete CPU observations;
comparison refuses a mixture of primary, CPU and IRQ observer builds. Keep each
observer's passes and distributions separate. The CPU observer is unavailable
on Pico: its performance-counter startup has not been qualified, and the build
rejects this selection.

The CPU build enables NuttX hardware performance events with overflow correction
and per-thread runtime accounting, or Zephyr thread runtime statistics using its
timing counter. A 20 ms busy interval before warmup checks counter availability
and gross unit errors against the independent wall counter. Its record is
separate from workload accounting; it is not an external clock calibration.
The existing empty-receiver interval also records that receiver's CPU charge.

These are **participant thread charges**, including task-context kernel work.
The NuttX counter includes interrupt work attributed to a scheduled thread.
Zephyr's Xtensa hooks stop charging before ISR dispatch. Reports retain distinct
`cpu_scope` values and state whether their charge scopes match. The NuttX
std/native pair shares one accounting scope; cross-kernel values are not a
matched measurement of total MCU active time or energy. Accounting changes
kernel behavior and code placement, so CPU-image latency is diagnostic evidence
and does not replace the primary timing distribution.

Heap snapshots also have explicit availability and scope. NuttX reports the
allocator arena visible through `mallinfo`: current usage, peak since boot,
free space and largest free block. This is not a timed allocation count or a
channel-only footprint. Zephyr and host heap observation is unimplemented and
is emitted as `available: false`, `scope: "unavailable"` and null values. All
unavailable CPU durations are null as well; replay rejects fabricated zeros or
the former integer sentinel where an unavailable observation is required.

## Separate interrupt-response diagnostic

On ESP32-S3, `--observer irq` builds a separate image for each selected
runtime/backend. CPU accounting stays disabled. Pico rejects this observer.
Use the same workload plan with `plan --observer irq`; never pool its timings
with primary or CPU images. The diagnostic generates one normal, maskable,
priority-1 interrupt every nominal 100 microseconds. This deliberately adds
interrupt and sample-recording work to the workload.

The probe owns **SYSTIMER target1 on counter0** only in the pinned single-core
configuration. OS alarm target0 and ESP timer target2 remain untouched. It
refuses an active, pending, externally mapped or SDK-claimed target1; NuttX
also checks its IRQ handle registry. It reads the existing 16 MHz counter
without resetting it or changing its clock/stall policy. Allocation occurs
only when acquiring the SDK interrupt handle, before the measured phases.
The existing wall-clock fixture initializes its counter policy separately.
Zephyr's board supplies a 4096-byte system-heap minimum even though this fixture
requests `HEAP_MEM_POOL_SIZE=0`; the IRQ image verifies that minimum is enabled.
The absence of a Zephyr heap observation does not imply absence of a heap.

The measurement is **programmed hardware alarm deadline to the first probe
counter read after ISR dispatch**. It includes interrupt masking, pending IRQ
arbitration, dispatch and the probe's entry/read cost. It is not a measurement
of critical-section duration alone, an external GPIO edge, or a calibrated
external clock. The runtime validates CPU0, the actual priority-1 interrupt
line and the four probe root addresses. The ELF-bound `irq-audit.json` also
inspects direct dependencies without cold-path exclusions. Flash, other-memory,
compiler-helper and unresolved indirect boundaries remain explicit; root
placement alone never qualifies the complete interrupt path. No dependencies
are automatically promoted.

An idle control covers the existing 100 ms empty-receiver interval, then a
separate loaded phase covers the workload release through its completion.
The Rust protocol preserves absolute cadence, skips overdue periods without
an ISR catch-up loop, and records `missed_periods`. Before inspection or output,
the adapter disables and acknowledges only its alarm, quiesces delivery on
the single core, and releases its handle. It restores its saved disabled target
configuration before releasing ownership and leaves the SDK's disabled matrix
route. It performs no timer-register writes after freeing the handle. Release
failure invalidates the run; SDK descriptor caching is not reported as complete
allocator reclamation.

Each phase retains its **first 256 samples** in fixed storage. All serviced
events still contribute to the event count and maximum, including events whose
individual records were dropped. Reports therefore label percentiles as
`retained_first_sample_lateness_ticks` and the maximum as
`maximum_serviced_lateness_ticks`. `dropped`, `missed_periods`,
`trailing_periods` and `pending_at_stop` remain visible. Deadlines that passed
without an ISR entry before shutdown have no measured latency; a loaded phase
with no delivered interrupt reports null statistics, not zero. An idle control
with no delivery fails. The record includes its fixed sample bytes and total
Rust probe-state bytes; these exclude SDK interrupt-manager overhead. This
observer does not establish physical timing or matched-path qualification.

## Inspect a prepared cohort without hardware

`inspect` revalidates the build receipt, source/configuration identity and bound
image/ELF artifacts. `check-cohort` applies the same matching rules used by
comparison and also checks the exact NuttX std/native kernel substrate:

```sh
python tests/mpsc-comparison/bench.py inspect --build-directory target/mpsc/esp32s3/nuttx-std
python tests/mpsc-comparison/bench.py check-cohort \
  --nuttx-build target/mpsc/esp32s3/nuttx-std \
  --nuttx-native-build target/mpsc/esp32s3/nuttx-native \
  --zephyr-build target/mpsc/esp32s3/zephyr-native
```

All three builds must use the same board, sources, compiler cohort and observer.
A successful preflight means those compiled controls match. Physical timing,
matched final operation placement and product qualification remain false.

Keep raw console logs, tool paths, receipts and flash backups local; boot output
can contain device identifiers. The physical capture workflow uploads nothing. The historical
[ESP results](../../docs/std-channel-performance.md) retain their original
configuration and evidence; this packaged entry is a new build cohort.

## Host controls

```sh
python -m unittest discover -s tests/mpsc-comparison -p 'test_*.py'
```

These run the actual host engine across all workload modes and reject corrupted
work, scheduling claims, incomplete ownership, changed receipts and unverified
sessions. Host output is explicitly labelled non-native and is not timing data
for either board.

The [MPSC CI workflow](../../.github/workflows/mpsc-comparison.yml) runs these
controls, builds the std/native NuttX pair on both board targets and checks their
kernel identities. Its artifacts contain build and inspection evidence, not
board execution. Target compilation, host controls and physical measurements
remain separate evidence levels.
