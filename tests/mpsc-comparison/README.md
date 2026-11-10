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
`platform/nuttx/platforms/{esp32s3,pico2w}-mpsc.toml`. No production HAL API or
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

Backend selection is fixed at build time and bound into the v3 receipt. The
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
relocations, code, data and member order, so differing debug paths do not imply
a different kernel implementation. Runtime-byte or section-placement differences
still fail the control. Application archives and final image addresses differ
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
The early Zephyr memory hooks stay in flash. S3 uses the shared fixture IRAM
fragment alongside each kernel's native wait/scheduler placement. Build receipts
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

The v3 wait observer distinguishes the evidence: native transports observe
the measured queue object; the NuttX std transport observes semaphore-blocked
state with the engine's phase protocol; host checks make no native scheduling
claim. The NuttX native observer also checks the appropriate send/receive wait
state. The native adapter uses public `mq_*`, preserving native error and
cancellation-point behavior, but this workload does not request cancellation
or qualify timed operations and endpoint disconnection. Nonblocking full sends
return the original payload; interruption and other failures are not hidden as
full-queue rejection or retried silently. Native message allocation and pools
remain part of the implementation cost.

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
