# Event service timing, memory and GPIO controls

These local ESP32-S3 controls investigate the remaining questions in the
[service comparison](README.md): publication timing, interference from a
long synchronous handler, fully occupied queue storage, and real GPIO driver
work. They keep independent services, a single wait-any point and no replies.
They are an isolated experiment, not production dependencies or CI jobs.

The later [async scheduling follow-up](SCHEDULING.md) compares natural awaits,
budgeted handoffs, CPU chunking and a simulated I/O wait. The dated controls
below remain historical evidence from their own frozen images.

## What changes and what stays fixed

The timer comparison builds the same revised application at 10 ms and 1 ms
resolution. NuttX changes only `CONFIG_USEC_PER_TICK`; Zephyr changes only
`CONFIG_SYS_CLOCK_TICKS_PER_SEC`; Embassy changes its timer wake interval.
NuttX and Zephyr keep their 10 ms preemptive timeslice and 20 × 4 KiB
spawned thread stacks. Embassy keeps 20 cooperative tasks with an 8 KiB
shared stack and no preemptive timeslice. On all platforms, queue capacity,
CPU/cache/flash settings, release calendar and deadline budgets stay fixed.
The new 10 ms images are controls for the new instrumentation, not silently
substituted for the original dated results.

| Control | Offered traffic and additional work |
| --- | --- |
| Normal and burst | Original 3,900 / 6,240 deliveries over two seconds |
| Short synchronous handler | Normal traffic; service zero performs 10,000 arithmetic iterations per data event |
| Intermediate synchronous handler | Same, with 100,000 iterations per data event |
| Long synchronous handler | Same, with 400,000 iterations per data event |
| GPIO LED | Normal traffic; service zero applies each control event to GPIO2 and verifies pad input |
| Full queues | Park all 20 services, fill all 480 slots, attempt one overflow per queue, then drain; repeat three times |

The handler work uses the same rotating, wrapping 32-bit arithmetic in C and
Rust, and retains the resulting value in service state. Differential tests
check its output, including all three measured iteration counts. It is fixed work,
not a timed busy loop that does less computation when preempted. The actual
handler duration is measured rather than assumed. Only one service receives
this extra work; the other 19 retain the original handler. Service-zero and
worst-other-service response are reported separately so its own processing
time cannot hide interference with peers.

The full-queue control holds the same service execution storage as the
traffic case. It verifies that every queue is at its configured depth at the
same time. Each deliberate overflow must be rejected without changing depth
or contents. Draining checks all 64 bytes and expected order, not only a count.
Empty, full and drained heap snapshots distinguish payload allocation from
execution storage and reveal retained allocations across three cycles. Three
cycles do not prove indefinite leak freedom or every allocation failure path.
Zephyr/Embassy buffers are already included in their ELF reservations; they
must not be added to static RAM again.

The GPIO control uses the board's existing GPIO2 LED, active high. NuttX uses
the user-LED driver's ioctl/readback path, Zephyr uses native GPIO APIs, and
Embassy uses an owned HAL pin with its input buffer enabled. All allow a 1 us
settling interval before reading the pad. Each run requires 21 event-driven
LED operations with no readback errors, and returns the LED to off. This
tests real actuation and driver calls; it does not demonstrate light output,
IMU/GNSS/Wi-Fi integration, or external interrupt-to-service latency. Those
require additional hardware or a separately designed interrupt source.

## Accounting and reproducibility

The controls add one 276-byte test observation block to the original latency
diagnostics. Whole image/RAM figures include that block, new work/control code
and HAL code. The 10 ms/1 ms pair has identical instrumentation. Baseline C/Rust
and these revised C/Rust comparisons each share their own kernel configuration;
a timer change must not be normalized away in the kernel identity.

`build.py --timer-ms 1` requires an already-resolved 1 ms NuttX baseline and
separate prepared build tree; it checks the actual tick and timeslice. Zephyr
uses the experiment-owned `timer-1ms.conf`, and Embassy the `timer-1ms` feature.
Every image is frozen with hashes before measurements. The private backup and
raw serial evidence remain local, and the full original flash must be restored
and verified after each measurement matrix, including failure.

Use `control_matrix.py` for these profiles and `control_report.py` for the
sanitized evidence. The original reporting commands still serve the original
workload. Failed delivery qualifications remain failures; a deadline miss is
reported separately and is not discarded. Success in short normal/burst runs
is not a maximum sustainable-load or hard real-time guarantee.

## Board results, 2026-10-04

There are three independent final matrices: [96 traffic invocations at
1 ms](results/esp32s3-controls-1ms-2026-10-04.json), [32 matched timer controls
at 10 ms](results/esp32s3-controls-10ms-2026-10-04.json), and [32 full-queue
invocations](results/esp32s3-controls-saturation-2026-10-04.json). Each case
has four repetitions in two rotated blocks. All accepted traffic was received
and validated, including overload cases. Every matrix restored and verified
the board's original full flash. Failed pilot qualifications remain in private
local evidence; they are not pooled with these frozen final images.

### Timing: finer wakeups fix the light-workload delay

With three queues per service, median per-run scheduled-release-to-handler
p99 bounds are below. These are milliseconds, not mean response times.

| Implementation | Normal, 10 ms timer | Normal, 1 ms timer | Burst, 10 ms timer | Burst, 1 ms timer |
| --- | ---: | ---: | ---: | ---: |
| NuttX C | 21.90 | 1.69 | 20.33 | 2.01 |
| NuttX Rust | 20.64 | 2.24 | 22.33 | 2.17 |
| Zephyr C | 20.08 | 1.80 | 20.87 | 1.96 |
| Embassy Rust | 12.29 | 1.51 | 12.29 | 1.54 |

Normal publication p99 changes from 19.64 / 19.72 / 19.45 / 10.22 ms to
1.49 / 1.91 / 1.54 / 1.15 ms in the same order. At 1 ms, normal post-to-handler
p99 is 0.366 / 0.512 / 0.351 / 0.256 ms. Publication delay and queue response
are different costs; the timer change improves both in this workload.

All 32 normal/burst invocations at 1 ms have zero rejected events and zero
deadline misses. The matched 10 ms matrix has zero rejected events but 142
control deadline misses. The old timer, rather than a large Rust queue cost,
was the main light-workload timing limitation here. This does not measure the
extra interrupt/CPU/power cost of a finer tick, or guarantee future deadlines.

### Longer handlers: small tasks work; unbounded synchronous work does not

All short and intermediate runs at 1 ms delivered their 3,900 attempted events
without rejections, protocol errors or deadline misses. Only service zero
does additional work. The following table separates its measured handler
duration from the slowest of the other 19 services. Handler duration is a
wall-clock observation and includes preemption, not isolated CPU time.

| Implementation | Intermediate handler p99 | Worst peer release→start p99, intermediate | Longest observed long handler | Rejections / deadline misses across four long runs |
| --- | ---: | ---: | ---: | ---: |
| NuttX C | 2.74 ms | 5.34 ms | 10.92 ms | 0 / 2 |
| NuttX Rust | 6.42 ms | 14.10 ms | 28.46 ms | 115 / 682 |
| Zephyr C | 2.52 ms | 5.21 ms | 10.88 ms | 0 / 0 |
| Embassy Rust | 5.44 ms | 16.55 ms | 21.69 ms | 2,088 / 8,560 |

Peer columns are medians of per-run worst-peer p99 bounds. The long profile
still attempts all 3,900 deliveries; rejected sends are explicitly counted,
never overwritten or retried. All accepted messages drain correctly.
Zephyr passes four long runs, NuttX C two, and neither Rust case passes the
long profile's capacity/deadline qualification. This is a workload result,
not a universal RTOS or language ranking.

The fixed arithmetic compiles differently. In the measured NuttX C ELF,
`es_work_value` uses a hardware `loop` and `ssai`/`src` for rotation; its hot
body is six instructions. The matched Rust loop uses three shift/extract/or
instructions for rotation and an explicit decrement/branch; its body is nine
instructions. Both have one multiply per iteration and strength-reduce the
iteration mix to an addition. Embassy's measured loop has the same Rust
pattern. This identifies a real compiler-code-generation difference, not
`std`, heap allocation or message-queue overhead. Instruction counts explain
the direction, but are not a complete cycle-cost attribution.

The actual Embassy release image retains the profile-selected 10,000 /
100,000 / 400,000 iterations and the loop at `0x42012c0b` through `0x42012c20`,
followed by the state store at `0x42012c26`. NuttX C's hot body is at
`0x42026990` through `0x4202699e`; the ELF hashes are in the 1 ms evidence.
The arithmetic was not optimized away in the measured binaries. The final
state value is not exported as a work checksum, so this artifact inspection
is evidence for these builds, not a guarantee about a future compiler build.

Embassy processed 92 of the 117 offered work jobs in each long-profile run,
with a maximum observed handler duration of 21.69 ms. Across four runs,
2,088 rejected sends and 8,560 deadline misses demonstrate overload of this
tested configuration. CPU utilization was not measured; wall-clock handler
durations do not establish total CPU demand. The long profile combines
compiler-generated handler costs and cooperative interference, so it is not
a fair measurement of scheduler overhead alone. The intermediate profile
is more informative: it passes its budgets,
but three queued 5.44 ms handlers can make another task wait about 16 ms.
One yield between events prevents indefinite monopolization; it cannot
preempt a single synchronous handler. CPU-heavy handlers need bounded work
or a separately characterized execution path. A single wait-any point avoids
sequential timeout delays, not overload or arbitrarily long handlers.

### RAM: full capacity changes the NuttX budget

All 96 fill/drain cycles passed: 46,080 validated deliveries and 3,840
deliberate full-queue rejections, with no content/order/depth errors. Every
cycle held all 480 slots simultaneously while service execution storage
remained present. NuttX's drained heap returned to that cycle's empty value;
minor allocator differences occur between some runs, so stable drain is not
described as identical whole-heap usage across every invocation.

| Implementation | Whole RAM, 20 mailboxes | Whole RAM, 60 class queues | Added heap when filling all slots |
| --- | ---: | ---: | ---: |
| NuttX C | 245,280 bytes | 257,184 bytes | 37,760–37,768 bytes |
| NuttX Rust | 245,360 bytes | 257,576 bytes | 37,760 bytes |
| Zephyr C | 228,056 bytes | 230,936 bytes | 0: buffers already reserved |
| Embassy Rust | 83,584 bytes | 84,864 bytes | 0: buffers already reserved |

Whole RAM is resident ELF RAM plus NuttX's observed allocator high-water
mark. Static queue storage in Zephyr/Embassy is counted once, not added again.
C/Rust share their kernel configuration and resident RAM; small observed heap
differences do not establish a language cost. This is a held-execution-storage
full-queue test, not a bound on every future concurrent driver/error-path
allocation.

The dominant NuttX fill delta has an exact explanation. Its profile provides
eight general-purpose preallocated messages; the eight IRQ-reserved messages
are not used by these thread sends. The remaining 472 messages request 75
bytes each: a 12-byte queue node including its one-byte placeholder, plus
63 further payload bytes. With the four-byte allocation overhead and heap
alignment, each occupies 80 bytes: **472 × 80 = 37,760 bytes**. One C case
has eight additional observed bytes; the snapshots do not attribute that
small residual to a particular allocation. The preallocated pool is already
part of resident RAM; it is not free storage.

This fixture still includes 81,920 bytes of spawned thread stacks on NuttX
and Zephyr, versus Embassy's 8,192-byte shared stack. Neither was tuned for
a favorable result. The common test diagnostics occupy 29,920 + 276 bytes.
Subtracting those diagnostic bytes arithmetically gives 226,988 / 227,380 /
200,740 / 54,668 bytes for the 60-queue cases. Those are not rebuilt lean-image
bounds. At a 250,000-byte budget, even the illustrated NuttX result leaves
only about 23 kB for additional work. All-slots-full NuttX therefore needs
an explicit application/platform RAM budget; its normal-traffic peak was
not sufficient evidence of headroom. Stacks remain execution storage, not
messaging overhead.

### Image size and the GPIO path

These 1 ms images contain all control modes, not separate minimal LED apps.
Loaded flash excludes debug information and padding; merged length includes
boot/layout alignment.

| Three-queue implementation | Loaded flash | Merged image |
| --- | ---: | ---: |
| NuttX C | 176,232 bytes | 214,304 bytes |
| NuttX Rust | 177,068 bytes | 214,328 bytes |
| Zephyr C | 87,619 bytes | 142,108 bytes |
| Embassy Rust | 64,725 bytes | 178,288 bytes |

The matched NuttX Rust delta is **836 loaded bytes and 24 merged bytes**, with
identical resident RAM. The 10 ms control pair adds 828 / 16 bytes. These are
costs of this fixture, not a fixed tax for all future Rust APIs. Adding the
work/saturation/HAL controls changed the app; the original 704 / 16-byte
result remains a different dated build. Whole-platform comparisons still
include unequal shell/VFS/libc/board features.

All 16 GPIO-profile invocations passed 21 event-driven LED operations each:
336 operations, zero readback errors, zero rejected messages and zero deadline
misses. Release-to-handler p99 is 1.81 / 2.31 / 1.85 / 1.51 ms. This confirms
real GPIO driver work fits the tested lightweight event loop. It does not
test an externally generated GPIO interrupt, optical LED output or a sensor
bus transaction.

## Repeating the controls locally

Build fresh platform/layout directories with `build.py --timer-ms 1` and
`--timer-ms 10`, supplying the pinned local tool/input paths. NuttX needs
separate prepared trees and resolved baselines differing only in tick period;
keep both C/Rust cases on the corresponding same baseline. Keep one board
owner, a protected full-flash backup and fresh output directories.

Run normal/burst at both timer settings. At 1 ms, additionally run
`work-short`, `work-medium`, `work-long` and `hal` on the four three-queue
cases. Run `saturation` separately on all eight layout cases, so its full-heap
high-water mark does not contaminate traffic RAM observations. For example:

```sh
python3 tests/event-services-comparison/control_matrix.py \
  --artifacts target/event-controls-1ms --out target/event-controls-full-queues \
  --backup /private/path/device-before.bin --port /dev/cu.YOUR_BOARD \
  --flasher /path/to/esptool.py --runs 2 --blocks 2 --profile saturation
python3 tests/event-services-comparison/control_report.py \
  --matrix target/event-controls-full-queues/control-matrix.json \
  --out tests/event-services-comparison/results/full-queues.json
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
```

The report reparses private serial evidence and checks its hashes, expected
profiles, actual timer configuration, per-service measurements and restoration.
The public JSON contains numeric results and build/source hashes, not device
identifiers, firmware backups or raw boot logs. Zephyr/Embassy remain isolated
comparison dependencies; no CI jobs or upstream-source edits are introduced.
