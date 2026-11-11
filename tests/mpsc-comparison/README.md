# Portable MPSC comparison

Build **`nxrs-mpsc-comparison`**, executable **`mpsc-comparison`**, firmware command
**`mpsc_perf`**. This qualification fixture uses one Rust workload on both kernels:
NuttX uses `std::sync::mpsc::sync_channel`; Zephyr uses native `k_msgq` through
thin target-header adapters. It does not port Rust std to Zephyr.

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
  --tools /path/to/local-tools.toml --out target/mpsc/pico2w/nuttx
python tests/mpsc-comparison/bench.py build --board pico2w --runtime zephyr \
  --tools /path/to/local-tools.toml --out target/mpsc/pico2w/zephyr
```

Use `--board esp32s3` and separate output paths for the S3 pair. Output directories
must be new. `build.json` identifies the exact image and ELF and binds sources,
resolved configuration, compiler versions and relevant native build evidence.
Failed builds remain failed receipts. Capture/replay reject changed inputs or
artifacts; replay historical captures from their matching source checkout.

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
callees before claiming a new cohort has matched placement. The Pico automated
audit follows direct branches and veneers; it does not prove indirect targets.
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
python tests/mpsc-comparison/bench.py plan --passes 3 > /path/to/private-plan.json
python tests/mpsc-comparison/bench.py run \
  --build-directory target/mpsc/pico2w/nuttx --port /dev/your-console \
  --case stream-p4-b16-q8 --session /path/to/session.json \
  --output target/mpsc/captures/pass-1-nuttx-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py replay \
  --build-directory target/mpsc/pico2w/nuttx \
  --output target/mpsc/captures/pass-1-nuttx-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py compare \
  --nuttx-build target/mpsc/pico2w/nuttx --zephyr-build target/mpsc/pico2w/zephyr \
  target/mpsc/captures/pass-1-*
```

The plan has 16 cases per kernel: sustained throughput; ordinary stream; paced,
burst and overload arrivals; verified full-queue backpressure; 64-byte messages;
and a 64-slot stream control, each with one/four producers. It alternates kernel
order by pass. Start with one pilot per kernel, then follow the plan with fresh
boots. Repeat a focal case as another pass rather than pooling samples.

Strict replay checks complete offered/admitted/rejected/delivered accounting,
payload checksum, per-producer FIFO, blocking proof, scheduling, stacks and
joined cleanup. Report throughput, delivery/admission/release tails and losses
together. Normal-case loss gates remain explicit. CPU accounting is unavailable
in these primary images and is reported as unavailable, never zero. Comparison
requires the same board, source inventory, contract and Rust compiler, pairs
each case within its pass and never pools distributions. Passing replay alone
does not establish a product deadline or physical timing qualification.

Keep raw console logs, tool paths, receipts and flash backups local; boot output
can contain device identifiers. This workflow uploads nothing. The historical
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
