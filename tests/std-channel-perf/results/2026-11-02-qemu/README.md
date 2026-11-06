# Std channel qualification evidence — 2026-11-02

These are **NuttX/QEMU behavioral results and physical-target build records**.
They do not establish physical MCU timing, a channel speedup, or a ranking against
PX5, ThreadX or Zephyr. The date uses America/Toronto; individual host paths and
run identities are preserved as recorded. The captures use the frozen
`c03ac02`-based worktree, before integration with later main changes; the
[provenance limits](#provenance-and-archive-limits) below explain that distinction.

## Rebuilt observations

Each rebuilt MPS2 case used its own firmware and one command after a fresh
NuttShell boot. All accepted runs joined their workers without a deadline failure.
The actual pinned std channel has eight slots and carries a 16-byte Rust value.

| Case | Observed result | Meaning |
| --- | --- | --- |
| [Blocked receiver](after-clock-fix/latency/run/analysis.json) | 64/64 retained samples have independent native blocking observations, after 8 warmups | The receiver was in `TSTATE_WAIT_SEM` inside `recv`, with the expected sequence and FIFO priority; all payloads were valid |
| [Reserved slot](after-clock-fix/witness-reserved/run/analysis.json) | `reservation-stall-observed`; recovery applied | The independently awakened high-priority consumer stayed in `try_recv` behind the low-priority producer's unpublished reservation |
| [Published-slot control](after-clock-fix/witness-published/run/analysis.json) | `published-slot-control-completed`; no recovery | Both operations completed when the independent wake followed actual slot publication |

The reservation result is **negative evidence for the channel's fixed-priority
progress contract**. Both workers were ready at their original priorities
(producer 120, consumer 180), and neither operation nor the producer hook had
completed at observation. Raising the producer to 200 and lowering the consumer
to 100 allowed publication and valid delivery. The published control proves only
its specific interleaving; it does not qualify arbitrary priority assignments.
The observer ran at 220 and the deadline watchdog at 240.

The blocking observation identifies a native wait inside the receive call, not
the address of the private pthread condition variable. Timing boundaries, setup
allocation, warmup and send-call preemption are described in the
[fixture README](../../README.md). The private std hooks are absent from the
latency image and distinct in the two witness images.

The rebuilt emulator metadata reports `clock_source: nuttx-monotonic` and
`counter_validated: false`. Its 1,000,000,000 Hz field encodes nanosecond timestamps;
it is not a claim that the emulated CPU has a calibrated 1 GHz cycle counter.
Raw stamps and distributions are retained for audit, but every capture has
`physical_timing_qualified: false`. No emulator latency numbers are used as a
performance result here.

## Preserve the failed clock measurements

The original latency image was rejected twice with `monotonic-reversed`:
[first fresh boot](before-clock-fix/latency/run/capture.json) and
[second fresh boot](before-clock-fix/latency/run-02/capture.json).
The failure records and raw consoles remain in the archive; no successful
analysis was fabricated for them. The original reserved and published controls
are also retained under `before-clock-fix/`.

Investigation identified two ARMv8-M SysTick readout defects: mixing a pre-wrap
CURRENT value with a post-wrap pending interrupt, and mistaking a positive raw
count rounded to zero microseconds for an actual reset count. The
[original SysTick patch 0105](inputs/0105-armv8m-systick-coherent-snapshot.patch)
takes a coherent pending/count snapshot and uses the raw count for zero tests.
The [native regression](../../../nuttx-std/test-patches.py) executes the original
and patched functions with scripted MMIO transitions. The original fails the
three defect cases; the corrected function passes those cases and five
normal/reset/callback controls.

The rebuilds in this archive include the original SysTick patch 0105 in
their recorded source inventory. During later integration with an advanced main
branch, the same patch bytes were assigned series slot **0106** because a different
change had taken 0105. The original filename and hashes are preserved here.
These observations do not establish execution of that later integrated source
tree or its additional changes. Temporary incremental diagnostic runs are not
part of this archive and are not substitutes for these completed build receipts. This correction does not remove the existing
single-pending-bit limitation after multiple timer wraps during a long interrupt
mask, and does not establish physical clock accuracy.

## Physical-target builds remain build-only

| Target | Recorded builds | Source cohort |
| --- | --- | --- |
| [ESP32-S3 N16R8](physical-build-only/esp32s3/latency/build.json) | Latency, reserved-slot witness and published-slot control, with the native cycle-counter configuration | Completed before ARM-only patch 0105; receipts preserve that earlier source inventory |
| [Pico 2](physical-build-only/pico2/latency/build.json) | Latency with the native DWT cycle-counter configuration | Completed with patch 0105 in the source inventory |

No physical board was executed for this archive. These builds establish target
header/ABI compatibility and a linked image for their recorded inputs. They do
not verify the physical CPU frequency, counter operation, scheduling behavior or
latency. Use the [physical capture procedure](../../README.md#physical-board-recipe)
with the matching flashed image, session evidence and fresh boot for each run.

## Provenance and archive limits

The builds report source revision
`c03ac02ff1443da34acc6022ea4d38246e7c1417` and `source_dirty: true` because the implementation was built before its changes were committed. That revision alone
therefore does not identify the tested worktree. Each original `build.json`
retains the `source_sha256` inventory, selected std source/compiler identities,
configuration and executable digests, mode and unique build ID. Raw console build
IDs were checked against those receipts. Historical absolute paths identify the
original build worker; they are not portable checkout paths.

The archiving step checked the actual retained ELF, flash image and resolved
configuration against their receipt hashes, then copied text evidence unchanged.
[manifest.json](manifest.json) lists those checks and each copied file's SHA-256.
ELF/flash binaries, SDK trees and Cargo caches are deliberately absent from this
text archive. Consequently, a fresh checkout can check transcript consistency
and archived-file integrity, but cannot revalidate an omitted executable from its
hash alone. Original build/CI artifacts are needed for that check; newly rebuilt
images must produce their own receipts and captures.

To verify the archived file bytes from this directory:

```sh
sha256sum --check SHA256SUMS
```

`SHA256SUMS` covers every retained file except itself, including the manifest and
this explanation. The manifest's `files` entries cover original copied evidence;
the README, Git attributes and checksum list are archive metadata, not target
observations. Git attributes preserve the byte-hashed files' original line endings.
