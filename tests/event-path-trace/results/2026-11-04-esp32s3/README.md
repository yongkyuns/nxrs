# P4 physical blocked-delivery diagnostics — November 4, 2026

Completed 36 fresh-boot captures on the connected ESP32-S3 N16R8: three passes
of four cases in each of baseline quiet, note-enabled quiet and note-enabled
recording. Each boot retained 64 samples after 16 warmups, using a capacity-eight
channel and checked 16-byte payloads. Transport order rotated per pass. All 12
matched comparisons replayed successfully; runs were never pooled.

**Decision:** keep production messaging and scheduler policy unchanged. Equal
FIFO did not improve delivery latency. The traces establish a scoped wake/switch
timeline, but instrumentation changes rankings and cannot quantify P2's original
gap without a less intrusive control.

[Per-pass aggregates](summary.json) and [public provenance](provenance.json)
contain no raw events, runtime addresses, device identifiers or host paths.
Raw consoles, sessions and flash backups remain local.

## Delivery and instrumentation perturbation

These are per-boot median elapsed CCOUNT ticks, not CPU time. The configured
frequency is 240 MHz; independent reference calibration is unavailable, so no
absolute-time qualification is claimed. With 64 observations, nearest-rank p99
equals the maximum; all quantiles remain in the JSON summary.

| Case | Pass | Baseline quiet | Notes quiet | Notes recording |
| --- | --- | ---: | ---: | ---: |
| std 120/180 FIFO | 1 | 11,819 | 26,681 | 26,263 |
| messaging 120/180 FIFO | 1 | 25,311 | 14,101 | 20,734 |
| crossbeam 120/180 FIFO | 1 | 16,779 | 27,015 | 32,887 |
| crossbeam equal FIFO | 1 | 29,259 | 57,957 | 69,411 |
| std 120/180 FIFO | 2 | 15,179 | 26,173 | 27,763 |
| messaging 120/180 FIFO | 2 | 24,462 | 13,144 | 20,734 |
| crossbeam 120/180 FIFO | 2 | 15,822 | 27,015 | 37,381 |
| crossbeam equal FIFO | 2 | 27,357 | 56,970 | 69,411 |
| std 120/180 FIFO | 3 | 13,733 | 26,621 | 26,690 |
| messaging 120/180 FIFO | 3 | 22,497 | 13,144 | 20,734 |
| crossbeam 120/180 FIFO | 3 | 17,316 | 28,944 | 35,814 |
| crossbeam equal FIFO | 3 | 28,266 | 59,739 | 65,577 |

Enabling notes with recording disabled changed each matched delivery median by
0.537–2.257× baseline. Recording then changed it by 0.984–1.577× the note-enabled
quiet arm. These ratios compare distributions; they are not additive hook costs.
Messaging and raw std share the std channel backend, yet their ordering reverses
between these images. Cache/layout effects are hypotheses, not isolated causes.
Do not transfer this diagnostic ranking to the
[P2 transport comparison](../../../event-path-perf/results/2026-11-04-esp32s3/README.md).

In the notes-disabled P4 baseline, equal-FIFO Crossbeam delivery medians were
1.632–1.744× its unequal-FIFO medians. Its sender-call medians fell to
0.466–0.506×, showing why a shorter caller-visible send interval alone does not
establish better delivery. No priority change is adopted.

## Correlated observations and resource placement

Inside every retained admission-to-receive interval, the trace shows exactly
one receiver ready marker and one receiver resume. There is no observed
repeated wake/resume sequence in that interval. Control-gate wakes are outside
this interval. The pinned IRQ definitions identify IRQ 7 as the syscall path
used by context switching and IRQ 65 as system timer target 0; the IRQ count
therefore includes scheduler machinery. See the
[pinned IRQ definitions](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/arch/xtensa/include/esp32s3/irq.h).

The following are ranges of **per-boot medians** across three recorded boots,
not pooled estimates or an additive decomposition. All endpoints are recorder
markers/notes; the ready marker follows insertion, and the resume note precedes
architectural restoration.

| Recorded case | Admission marker → first ready marker | Ready marker → receive marker | Last resume note → receive marker |
| --- | ---: | ---: | ---: |
| std unequal | 19,896–20,411 | 8,245–8,248 | 6,360–6,363 |
| messaging unequal | 15,572–15,572 | 7,292–7,292 | 5,406–5,406 |
| crossbeam unequal | 19,643–23,917 | 13,328–14,267 | 8,839–10,510 |
| crossbeam equal | 27,244–29,158 | 39,080–40,973 | 13,381–14,365 |

The wake/switch/return path is material in these traces, but the markers do not
bound pure architectural switch cost or account for exclusive CPU consumption.
The original P2 gap's quantitative cause remains unresolved by this intrusive
image. Preserve the std-backed facade; a narrower probe with measured
perturbation and independent clock calibration is the next attribution step.

All observed channel-constructor allocations and sender/receiver stack ranges were
in internal DRAM in all 36 boots. Each transport requested 160 bytes aligned to
4 plus 320 bytes aligned to 64 during construction. This excludes allocator
bookkeeping and deferred allocations; it is not total channel/firmware peak
memory. Native TCB readback reported adjusted sender/receiver stack sizes of
65,232/16,160 bytes, rather than peak stack use. The separately reserved event
buffer occupied 65,544 internal-DRAM bytes. Recorded boots retained 1,604–1,984
events without overflow. These are diagnostic resource costs, not production
camera costs, and do not establish P2's allocation placement or cache residency.

## Progress contract and validation

Repeated all six P1 equal-FIFO reservation/reclamation controls on fresh boots:
each completed without repair and the named cohort gate passed. That image
retains its known legacy ROM digest warning. This supports only the
[named P1 contract](https://github.com/yongkyuns/nxrs/blob/dc3bf1e800e4f95dba5fa05ee42f1aec26788dbf/tests/channel-progress/FIFO-SCHEDULING-AUDIT.md), not full
owner progress, arbitrary blocking/full queues or production admission.

A fresh full-flash backup was read and verified before the campaign. The first
quiet capture was excluded after the original host replay rejected NSH's erase
suffix; its failure evidence remains private. The full 36-run matrix was rerun
after correcting prompt validation. The unchanged original was reverified
before flashing and restored and verified after testing. All accepted P4 boots
were free of ROM digest warnings and native faults. Payload/count, native wait,
clock-wrap, overflow, terminal quiescence, thread/watchdog joins and schedule
restoration checks passed, followed by offline replay of all comparisons.

Firmware: `ae36637c`, CI merge `35cef201`, run
[41856717255](https://github.com/yongkyuns/nxrs/actions/runs/41856717255).
Capture/replay: `d6749dd6`; its P4 host and both target CI jobs passed. Compiled
Rust/C/header inputs matched the exact firmware receipts. Two Rust buffer tests,
nine Python refusal/compatibility controls, architecture audit and 33 architecture
and HAL-boundary regression tests passed. Physical timing, general priority
progress and production-transport qualification flags remain false.
