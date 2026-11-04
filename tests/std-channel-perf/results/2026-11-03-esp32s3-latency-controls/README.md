# P0 latency controls — physical WROOM campaign

All 13 fresh-boot cases completed on the same new digest-candidate executable:
13,000 retained samples, verified native blocking and terminal quiescence,
valid payloads and joined workers. Every capture matched the flashed image's
runtime build identity and contained no ROM SHA-256 warning. Each command retained
1,000 samples after 100 or 101 excluded warmups. The fixed pairs were repeated
in reverse order, with intervening seeded cases; the table is execution order.

| Case | Warmup | Receive p50 / p99 / maximum ticks | 1,872-tick receives at retained even / odd indices |
| --- | ---: | --- | --- |
| Tight | 100 | 1865 / 1872 / 5140 | 0 / 125 |
| Seed 1, A | 100 | 1865 / 2821 / 6050 | 1 / 97 |
| Seed 17 | 100 | 1865 / 2821 / 2821 | 0 / 97 |
| Fixed 100 µs, even A | 100 | 1865 / 2815 / 5515 | 0 / 119 |
| Fixed 100 µs, odd A | 101 | 1865 / 2815 / 5518 | 119 / 0 |
| Fixed 1000 µs, even A | 100 | 1865 / 2821 / 3079 | 0 / 105 |
| Fixed 1000 µs, odd A | 101 | 1865 / 2815 / 2821 | 108 / 0 |
| Seed 113 | 100 | 1865 / 2821 / 2821 | 1 / 101 |
| Seed 1, B | 100 | 1865 / 2821 / 6050 | 0 / 97 |
| Fixed 1000 µs, odd B | 101 | 1865 / 2821 / 5341 | 107 / 0 |
| Fixed 1000 µs, even B | 100 | 1865 / 2821 / 3079 | 0 / 108 |
| Fixed 100 µs, odd B | 101 | 1865 / 2815 / 5310 | 119 / 0 |
| Fixed 100 µs, even B | 100 | 1865 / 2815 / 5515 | 0 / 119 |

Delays are nominal configuration-derived microseconds, actively paced before the
blocked-state check. All timing results above are raw counter ticks. Send-call
distributions remain separately recorded in every `analysis.json` and in
`observations.json`; they include preempting receiver execution. No samples,
including the final sample, or estimated overhead have been removed.

## What the controls show

The old seed-1 2,990/3,673-tick parity pattern did **not** recur in this executable.
Both new seed-1 boots instead have p50 1,865, p99 2,821 and maximum 6,050 ticks;
their complete histograms are retained and need not be identical. Seeds 17 and
113 also have p50 1,865 and p99 2,821. These are distinct executable/layouts from
the old archive, so this is not evidence of a channel or scheduler improvement.

A smaller iteration-dependent component repeats. In all four fixed-100 µs
captures, exactly 119 receives take 1,872 ticks. They fall at retained odd indices
after 100 warmups and retained even indices after 101 warmups, including when pair
order is reversed. All 119 belong to `(warmup + retained_index) % 8 == 7` in each
run. The long fixed-delay pairs show a similar shift, with counts and tails that
vary slightly. The full mod-2/mod-capacity reports preserve those differences.

This supports a repeatable relationship to total transfer count in this fixture.
**Total transfer index is not an observed internal channel slot.** Allocation,
cache, ready/switch and IRQ state were not instrumented; a specific source line
or mechanism remains unproved. Do not subtract percentile values to attribute
cost. Placement and scheduling/IRQ correlations require a separate diagnostic
image. Active pacing is not representative offered load, idle or power evidence.

## Device, provenance and limits

ESP32-S3 N16R8 rev v0.2, USB/MAC `20:6E:F1:A7:6F:58`, 16 MiB flash and
8 MiB octal PSRAM. One configured 240 MHz core, PM disabled, 16 KiB instruction
and 32 KiB data cache, PSRAM in the common heap; exact allocation/stack placement
was not measured. No camera/radio application ran; normal tick/USB interrupts
remained. All boots used the USB reset path. These observations are specific to
this device, executable and workload.

The [successful workflow](https://github.com/yongkyuns/nxrs/actions/runs/41749212399)
produced [artifact 13286185090](https://github.com/yongkyuns/nxrs/actions/runs/41749212399/artifacts/13286185090),
ZIP SHA-256 `bc584a1f6ae2018dc2ed85f518cbf99e9ea4182da8443921b9c94e5e92d55356`.
Its CI checkout was `32dca9f1ff869d27a556c65cbc03f4f58676ac9f`; all 127 recorded
firmware-input hashes match tested PR #259 head
`2b84a8b37193fb5839ec9a9a7fa6baad4a28eb53`. All recorded firmware artifact hashes
were verified. Build ID:
`5067c5db0afde2932e79ba446a51bb02201ab35373d5ce45d14d118af1054803`;
flashed image SHA-256:
`7ab92e67f5442d324abeb753266edfac64206230e8ac06972162e17bd0123943`.
The older artifact was not reused or relabelled.

**Absolute timing remains unqualified.** No independent reference calibration
was available. Every official capture reports `physical_timing_qualified: false`
with only `clock-not-independently-calibrated` as its timing blocker. Clean ROM
boot is not secure-boot qualification. No WCET, root cause, performance gain or
IRQ phase coverage is claimed.

`campaign.json` preserves case order; `runs/` contains raw serial bytes, sessions,
official analyses, build receipts and `alternation.json` reports. Every alternation
report was independently regenerated from its hash-bound original console bytes
and compared exactly. `historical-seed-1-alternation.json` describes the unchanged
older capture using the same analyzer. Firmware binaries remain in the CI
artifact. `provenance.json` identifies the capture/analyzer sources and receipts.

The fresh private full-flash backup was verified before flashing, restored after
the campaign and verified again. Older snapshots were not used. Backup bytes and
credentials are excluded. `run-physical.py` records the operator used, with local
paths; future runs require another fresh backup and new directories. `SHA256SUMS`
covers every retained file except itself, preserving raw console line endings.

Local validation passed: 91 fixture tests, the 52-package architecture audit,
23 architecture, 10 HAL-boundary and three documentation regressions. Exact-head
std-channel CI passed host/Rust and target checks; the inherited separate secrets
check was failing and is not resolved here. This campaign closes the listed
physical control runs, not the remaining calibration/placement/causal gates.
