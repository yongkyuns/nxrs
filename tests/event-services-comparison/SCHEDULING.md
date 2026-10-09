# Async waits, bounded CPU work and thread-wrapper costs

This is a local ESP32-S3 follow-up to the [event-service comparison](README.md)
and [timing controls](CONTROLS.md). It separates a genuine asynchronous wait
from CPU work, and compares Embassy scheduling policies without changing the
offered traffic. Zephyr and Embassy remain experimental, not nxrs dependencies.

Natural async waits passed the I/O and lighter-work profiles without explicit
yields. Bounded CPU chunks greatly reduced interference with other services,
but did not make an overloaded service loss-free. The standard thread wrapper
has a modest measured RAM cost, separate from the much larger common stacks.

These are the 2026-10-04 portable-Rust results. The later
[arithmetic follow-up](ARITHMETIC.md) now measures unchanged portable apps with
a private pinned LLVM/Rust rebuild. All five newer cases complete the long load
without rejections, but the patched Rust cases still miss some long-work
deadlines. In particular, chunked Embassy is loss-free, not fully
deadline-qualified. The dated matrices are separate; the earlier assembly
diagnostic is not compiler evidence or a current application feature.

## What the scheduling variants test

All cases have 20 service owners, three eight-slot inboxes per service,
64-byte events, one wait-any point, and the same release calendar. There are
no replies or sequential queue timeouts. Sends never block, retry or overwrite
a full queue. Normal traffic attempts 3,900 deliveries; burst attempts 6,240.
Traffic lasts two seconds with a 500 ms drain interval.

The board uses one 240 MHz core, DIO 40 MHz flash, identical cache settings,
application `-O2` and 1 ms publication wake resolution. NuttX C/Rust share the
same kernel configuration and native queue/thread adapter. NuttX and Zephyr
retain their 10 ms preemptive timeslice and 20 × 4 KiB service stacks. Embassy
uses 20 cooperative tasks with an 8 KiB shared stack. No stack tuning is used
to improve a language or messaging result.

| Embassy policy | When the service hands control back |
| --- | --- |
| Per-event control | An explicit handoff before every inbox wait, including ready inboxes |
| Natural waits | Only when the inbox/deadline or I/O future returns `Pending` |
| Budgeted loop | Natural waits; additionally, when an inbox remains ready after four handled events or 500 µs since the last suspension/handoff |
| Budgeted, chunked work | The same policy, plus a handoff between 10,000-iteration CPU chunks |

The budget is checked between handlers. It cannot interrupt a synchronous
handler at 500 µs. Chunk length is an iteration count, not a proven time bound.
The budget resets on resumption from the inbox or I/O wait and after chunk
handoffs: time spent suspended must not be mistaken for uninterrupted work.
The per-event policy is a historical control, not the preferred I/O design.

An `.await` is not necessarily a scheduling break: an already-ready future
can complete immediately. A task repeatedly receiving ready messages can
therefore keep executing within one executor poll. Embassy's fairness between
task polls cannot preempt that poll or a CPU loop inside it. This follows the
[Rust future contract](https://doc.rust-lang.org/std/future/trait.Future.html)
and [Embassy executor model](https://docs.embassy.dev/embassy-executor/git/std/index.html).

Only service zero's data handler gets additional work. The CPU profiles do
10,000, 100,000 or 400,000 iterations of the same wrapping arithmetic in C and
Rust. Chunking preserves every iteration, its absolute index and the final
value; host differential tests check this. Each firmware exports a work digest
to keep the result observable. Digests are not required to match across runs
because valid interleavings of different source queues change state order.

The separate I/O profile waits at least 3 ms per data event without adding CPU
work. Embassy registers a hardware-timer wake and returns `Pending`; NuttX
uses `nanosleep`, and Zephyr uses `k_sleep`. This simulates a peripheral wait;
it does not measure a real sensor, DMA, driver or interrupt-to-task path.
Service zero still handles one event at a time while its peers can progress.

## ESP32-S3 measurements, 2026-10-04

The [numeric evidence](results/esp32s3-scheduling-2026-10-04.json) contains
seven frozen images, six profiles and four invocations per image/profile in two
rotated blocks: 168 invocations. Earlier budget-reset pilot runs remain private
and are not pooled with this final matrix. Rejections and deadline misses are
retained rather than treated as unexplained loss or removed from the tables.
All 720,720 scheduled deliveries were attempted; 716,448 were accepted and
received, with zero protocol errors. The 4,272 rejected sends occurred only in
the long CPU profile. The original full 16 MiB flash was restored and verified.

### Speed: natural I/O waits need no explicit yield

All 140 invocations outside the long CPU profile had zero rejections and zero
deadline misses, including the intermediate CPU profile. Every I/O run completed
117 simulated operations. Natural, budgeted and chunked Embassy variants used
zero explicit handoffs in every I/O run; the per-event control used about 5,121
per run. The async wait itself suspends until a timer wake, rather than yielding
repeatedly while checking a condition.

| Implementation | Normal worst-peer queue p99, ms | I/O profile worst-peer queue maximum, ms | Longest simulated I/O operation, ms |
| --- | ---: | ---: | ---: |
| NuttX C | 0.399 | 0.468 | 3.980 |
| NuttX Rust, native threads | 0.857 | 0.908 | 3.983 |
| Zephyr C | 0.351 | 0.362 | 3.988 |
| Embassy, per-event control | 0.491 | 0.493 | 4.089 |
| Embassy, natural waits | 0.512 | 0.572 | 4.097 |
| Embassy, budgeted loop | 0.435 | 0.593 | 4.085 |
| Embassy, budgeted/chunked | 0.472 | 0.561 | 4.090 |

Queue response starts at the send call and ends when the handler starts. Peer
figures exclude service zero, which performs the extra work/wait. The p99 column
is the median of four per-run worst-peer histogram upper bounds, not a pooled
percentile. Maxima are the largest observations across those runs, not hard
bounds. Millisecond values are rounded. A minimum 3 ms wait need not finish at
exactly 3 ms with 1 ms wakes; these observations also include wake scheduling
and handler bookkeeping, not isolated device latency.
Removing forced handoffs is correct for this I/O design, but these short runs
do not show a general speed advantage over the per-event control.

### Speed: CPU chunking protects peers, not the busy service's own capacity

These are intermediate/long CPU profiles, not simulated I/O. Rejections and
deadline misses below are totals across four long-profile runs.

| Implementation | Intermediate worst-peer queue maximum, ms | Long worst-peer queue maximum, ms | Long rejected sends | Long deadline misses, all classes |
| --- | ---: | ---: | ---: | ---: |
| NuttX C | 5.630 | 10.955 | 0 | 10 |
| NuttX Rust, native threads | 11.449 | 25.163 | 120 | 727 |
| Zephyr C | 2.832 | 10.035 | 0 | 0 |
| Embassy, per-event control | 11.600 | 192.556 | 1,560 | 8,731 |
| Embassy, natural waits | 12.098 | 185.643 | 2,376 | 10,992 |
| Embassy, budgeted loop | 11.980 | 95.114 | 108 | 5,906 |
| Embassy, budgeted/chunked | 1.829 | 1.979 | 108 | 437 |

Chunking reduces the observed long-profile peer queue maximum from 185.643 ms
with natural waits to 1.979 ms. It also reduces peer control deadline misses
from 1,456 to 24 across four runs. However, peers' scheduled-release-to-handler
maximum is still 35.996 ms: service zero only returns to its own publication
calendar after finishing the current handler. A message can be posted late
and then processed promptly. Queue latency alone would hide this problem.

The chunked variant accepts and completes 90 of the 117 offered CPU jobs per
run, rejecting 27; its own queue response reaches about 186 ms. It therefore
fails the long profile's capacity/deadline qualification. Budgeting between
whole handlers helps, but does not preempt a 21.7 ms arithmetic loop. Chunking
lets peers run during the computation, yet increases its longest wall-clock
completion from about 21.7 to 27.3 ms, including those intervening tasks. It
does not create more CPU capacity or allow this service to handle another
event while the current one is unfinished.

NuttX C and Zephyr C complete all 117 jobs per run; Zephyr also meets every
tested long deadline. NuttX Rust completes 87, and its longest handler takes
29.9 ms. This profile combines handler code generation, queue capacity and
scheduling: it is not a pure scheduler benchmark. The earlier
[compiled-loop analysis](CONTROLS.md#longer-handlers-small-tasks-work-unbounded-synchronous-work-does-not)
identifies a C/Rust Xtensa code-generation difference in this arithmetic.
Chunking does not fix that compiler issue, and `std::thread` is absent here.

### Image size

The image columns compare code + initialized data with flash binary size.[^flash-size]

| Implementation | Code + initialized data (bytes) | Flash binary size (bytes) |
| --- | ---: | ---: |
| NuttX C | 176,884 | 214,508 |
| NuttX Rust, native threads | 177,708 | 214,532 |
| Zephyr C | 88,167 | 142,312 |
| Embassy Rust, four policies | 69,189–69,613 | 182,224–182,656 |

The matched NuttX Rust app adds 824 bytes of code + initialized data and 24 bytes
to the flash binary over C. This includes the revised handler, I/O/control code
and diagnostic output; it is not a `std::thread` measurement. Embassy's natural,
budgeted and chunked variants use 69,461 / 69,613 / 69,601 bytes of code + initialized data,
respectively. The largest policy difference is 424 bytes, not a large new runtime.

Whole-image platform differences include different feature sets: NuttX retains
its shell, VFS, libc and board services, while Zephyr uses native queues and
Embassy uses a heap-free executor. These are application configurations, not
equal-feature kernel-only comparisons. All images include profile-selected
work, GPIO support and reporting code, including profiles not run in this matrix.

### RAM

Whole RAM below is resident ELF RAM plus the largest observed NuttX allocator
high-water mark across all six profiles. Static heaps, buffers and stacks in
Zephyr/Embassy are already in resident RAM and are counted only once.

| Implementation | Resident RAM bytes | Observed extra peak heap bytes | Whole RAM bytes |
| --- | ---: | ---: | ---: |
| NuttX C | 104,384 | 118,032 | 222,416 |
| NuttX Rust, native threads | 104,392 | 119,864 | 224,256 |
| Zephyr C | 231,208 | 0 | 231,208 |
| Embassy Rust, all policies | 87,264 | 0 | 87,264 |

The NuttX resident C/Rust difference is eight bytes. The observed heap
difference depends on message occupancy, scheduling and allocator behavior;
it is not a measured per-thread Rust allocation cost.

All four Embassy policies reserve 87,264 bytes, with no heap. Their complete
480-slot payload storage is already counted: 30,720 bytes of slots and 1,920
bytes of queue metadata. The service future pool occupies 4,320 bytes, with an
88-byte clock future pool, in addition to the 8 KiB shared stack. Async state
storage is real; it is not another twenty preemptive stacks.

The common test diagnostics contain 30,472 nominal bytes, including the two
276-byte work/scheduling observation blocks. Alignment and ownership-slot tags
also occupy storage. These are measurement costs, not business state, and
subtracting them does not produce a tested lean firmware image.

NuttX traffic heap peaks are observations, not full-capacity RAM bounds.
Zephyr/Embassy reserve their queue buffers and execution storage in the ELF;
those must not be added again. The earlier [full-queue control](CONTROLS.md#ram-full-capacity-changes-the-nuttx-budget)
exceeded 250 kB for this NuttX 60-queue configuration. A lower traffic peak here
does not qualify a 250 kB target. Common native service stacks reserve 81,920
bytes in both C and Rust; they are execution-model cost, not Rust wrapper or
messaging overhead.

## What to use, and what remains to test

For mostly waiting, small-handler services, natural async waits are a valid
default; an unconditional yield per event is not required by this evidence.
Keep a cooperative budget for sustained ready traffic, and explicitly bound
CPU work where a handler cannot wait naturally. This adds little image space
and no measured RAM compared with the other policies in this fixture.

Those safeguards are not overload handling. A service must have enough capacity
for its own workload and publish on time. Reduce/offload expensive computation,
or separate a time-critical publisher from a long handler, then characterize
the revised design rather than assuming chunking guarantees every deadline.
Preemptive RTOS threads provide an execution option for blocking or long
handlers, but do not remove compiler costs or finite-queue limits either.

The next useful qualification is a real interrupt-driven asynchronous HAL wait
under concurrent traffic, with per-service publication and completion deadlines.
Measure CPU/idle power as well as latency, and retain a separate high-ready-load
test. The current timer simulation shows scheduling behavior, not that every
future driver or service has an efficient async implementation.

## What `std::thread` adds

The scheduling matrix uses native POSIX threads for NuttX Rust, not
`std::thread`. Its C/Rust delta cannot answer the wrapper-cost question.
The following numbers instead come from the separate, controlled October 3
[thread-wrapper measurements](results/thread-wrapper-costs-2026-10-03.json).
Their archived ELF hashes and section sizes were checked again for this report;
these fixtures were not reflashed as part of the scheduling matrix.

Both Rust variants keep the same embedded entry, kernel, stack reservations,
queues and HAL. Only the thread wrapper changes. The twenty-thread idle fixture
has no queues, and has ten invocations per variant.

| Measured `std::thread` minus native wrapper | Extra code + initialized data | Extra RAM |
| --- | ---: | ---: |
| Silent LED fixture | 7,802 bytes | Not isolated by this fixture |
| Twenty-thread fixture | 10,808 bytes | 4,792 bytes observed peak heap; 24 bytes fixed DRAM |

The twenty-thread stable live-heap difference is 4,808 bytes. Its peak difference
averages about 240 bytes per thread. A simple planning extrapolation is about
2.4 kB for ten threads or 3.6 kB for fifteen, excluding the common OS stacks.
Those two figures are estimates, not measured ten/fifteen-thread growth curves
or an exact allocation inventory. Captures, return types, naming, initialization
and allocator lifetimes can change the result.

The flash difference is largely shared wrapper/runtime code linked on first
use, not 7.8 kB multiplied by the number of threads. New closure and return
types can still add specialized code. Per-live-thread ownership, result and
lifecycle bookkeeping is a recurring RAM cost. There is no controlled evidence
here that the wrapper itself makes steady-state messaging slower; both paths
ultimately use the same OS thread scheduler.

The native wrapper has a narrower lifecycle contract. It is not a fully
hardened replacement for standard thread handles, IDs, parking, hooks and
detach behavior. In this abort-on-panic firmware, standard threads also do not
provide recoverable panic handling. Choose the standard wrapper when its API
and lifecycle guarantees are useful, rather than assuming that removing it is
always worth the maintenance burden.

## Reproducing and checking the comparison

`build.py` accepts `--embassy-scheduling event|natural|budget` and
`--work-mode monolithic|chunked`. Chunked work requires the budget policy;
native platforms reject cooperative-policy options. Build the seven named
directories listed in `scheduling_matrix.py`, all with `--layout three` and
`--timer-ms 1`, existing pinned tools and fresh output directories. For example:

```sh
python3 tests/event-services-comparison/build.py \
  --platform embassy --layout three --timer-ms 1 \
  --embassy-scheduling budget --work-mode chunked \
  --readelf /path/to/xtensa-readelf --espflash /path/to/espflash \
  --out target/event-scheduling/embassy-three-chunked
```

Prepare the matching NuttX 1 ms tree/baseline and supply each platform's paths
as described in the existing build controls. Freeze all images before flashing.
Protect a fresh full 16 MiB device backup and keep it and raw logs private.

```sh
python3 tests/event-services-comparison/scheduling_matrix.py \
  --artifacts target/event-scheduling --out target/event-scheduling-measured \
  --backup /private/path/device-before.bin --port /dev/cu.YOUR_BOARD \
  --flasher /path/to/esptool --runs 2 --blocks 2 \
  --profile normal --profile burst --profile work-short \
  --profile work-medium --profile work-long --profile io-wait

python3 tests/event-services-comparison/scheduling_report.py \
  --matrix target/event-scheduling-measured/scheduling-matrix.json \
  --out /path/to/fresh-public-summary.json
```

The runner checks artifact hashes and matched firmware inputs, rotates case and
profile order, and restores/verifies the original flash in `finally`. The report
reparses private serial evidence, checks hashes and numeric projections, and
exports no raw logs or private paths. It refuses an incomplete or unverified
matrix. Host tests cover C/Rust equivalence, chunk equivalence, budget reset/
counter wrap, exclusive state leases and report/restore failure paths.

These short runs do not qualify hard real-time bounds, CPU utilization, power,
multiple/priority executors, actual asynchronous drivers or every allocation
failure. Finite queues still need an arrival/burst budget and a full-queue policy.

[^flash-size]: Code + initialized data counts firmware sections stored in
    flash. Flash binary size also includes boot components, image headers and
    offset/alignment padding. Debug symbols and uninitialized RAM sections
    such as `.bss` are excluded from both. Debug symbols enlarge the separate
    ELF artifact, not the gap between these columns.
