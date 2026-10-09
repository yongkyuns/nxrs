# Event-driven services on NuttX, Zephyr and Embassy

Twenty services and sixty bounded queues can run correctly on all four tested
implementations. Their main differences are execution storage, platform
features and responsiveness during CPU-heavy work—not simply C versus Rust.

Rust on NuttX can add little overhead when it shares C's native queue/thread
boundary. Embassy uses much less RAM here, but synchronous work blocks its
single cooperative executor. Zephyr's native C configuration handles the tested
heavy work well. None of these results qualifies a finished 250 kB product.

## Workload and fairness

Each service owns an event loop, receives from multiple senders and has one
wait point for its inboxes and publication calendar. There are no reply
round trips or sequential queue-poll timeouts.

![Multiple senders, bounded inboxes and one service wait point](assets/rtos-comparison/service-loop.svg)

The main layout is 20 services × 3 queues × 8 slots × 64 bytes: **30,720 bytes
of payload capacity**. A 20-mailbox alternative preserves capacity by combining
each service's inboxes into one 24-slot FIFO. Normal traffic offers 3,900
deliveries in two seconds; burst traffic offers 6,240, then a 500 ms drain.

Accepted events are checked for delivery, order and payload correctness.
Nonblocking sends can reject overload; correct queues alone cannot guarantee
that every offered event fits or meets its deadline.

| Held equal | Different by design |
| --- | --- |
| One 240 MHz core, flash/cache settings, routing, payloads, calendar and application `-O2` | Native POSIX, Zephyr or Embassy facilities |
| Same NuttX kernel configuration and C/Rust native adapter | NuttX retains shell, VFS, libc and board services; other images have narrower APIs |
| 20 × 4 KiB native worker stacks in C and Rust | Embassy uses an 8 KiB shared stack plus saved task state |

![Preemptive threads versus cooperative tasks](assets/rtos-comparison/execution-models.svg)

NuttX Rust here does **not** use `std::thread`, std MPSC, Crossbeam or
`println!`. This isolates application-language cost, not full std convenience.
Zephyr Rust integration and other Embassy executor arrangements were not tested.

## RAM: count provisioned capacity

NuttX allocates queued messages on demand; Zephyr and Embassy reserve buffers
statically. Comparing a quiet NuttX run with a fully reserved async image would
understate NuttX's capacity requirement. The capacity control repeatedly fills
and drains every slot, including a deliberately rejected overflow send.

![Full-capacity RAM with execution stacks and diagnostics separated](assets/rtos-comparison/ram-capacity.svg)

| Full-capacity control | 60 class queues | 20 combined mailboxes |
| --- | ---: | ---: |
| NuttX C | 257,184 B | 245,280 B |
| NuttX Rust, native threads | 257,576 B | 245,360 B |
| Zephyr C | 230,936 B | 228,056 B |
| Embassy Rust | 84,864 B | 83,584 B |

Totals count resident RAM plus allocator high-water, or static buffers, stacks
and arenas once. All 32 capacity invocations passed their fill/drain checks.

The reference budget is **250,000 bytes**, not 250 KiB. The 60-queue NuttX
fixture exceeds it; one mailbox per service barely fits. Zephyr also leaves
limited margin. Embassy leaves much more room for drivers and application state.

These are whole-fixture totals, not messaging costs: native service stacks
reserve 81,920 bytes and nominal test diagnostics account for 30,196 bytes.
Common stacks are not a Rust tax. Subtracting diagnostics is not qualification
of a rebuilt lean image. Queue payload/metadata and owner-specific buffers
remain recurring costs; async buffers held across an await also consume RAM.

## Image size: distinguish code from packaging

The chart uses the original October 4 scheduling images, not the separate
capacity builds or later compiler cohorts.[^flash-size]

![Code + initialized data and flash binary size](assets/rtos-comparison/image-size.svg)

NuttX Rust adds **824 B of code + initialized data and 24 B to the flash
binary** over matched C. A later NuttX-only aligned-compiler check adds
844 B and 24 B respectively. Padding absorbs part of an increase until an
alignment boundary is crossed; the extra code still exists.

All tested binaries fit comfortably within 2 MB. Smaller Zephyr/Embassy code
totals include different platform features and do not identify a NuttX kernel
inefficiency. Embassy has less code but a larger binary than Zephyr because
their boot/image layouts differ. Budget the actual application partition, not
just a language delta.

For formatting, startup, thread wrappers and containers, see the separate
[Rust footprint analysis](rust-std-footprint.md). Those choices should not be
charged to this native-API fixture.

## Speed: responsiveness is not queue-operation time

![Publication lateness, queue response and handler duration](assets/rtos-comparison/latency-path.svg)

Release-to-handler response includes late publication and queue response.
Handler duration includes sleeps and preemption; it is not CPU utilization.
Means alone can hide a stalled service, so the checks also retain tail
latency, deadline misses and rejected-send counts.

![Matched publication wake controls](assets/rtos-comparison/timer-response.svg)

Changing publication wake resolution from 10 ms to 1 ms reduces median
per-run release-to-handler p99 from roughly 12–22 ms to 1.5–2.2 ms.
The native 10 ms timeslice is unchanged. Timer resolution can dominate a
small-handler benchmark; the CPU/power cost of finer wakes was not measured.

### I/O and CPU work need different treatment

In the original scheduling matrix, all 140 invocations outside the long-CPU
profile had no rejected sends or deadline misses. Every simulated-I/O run
completed 117 operations. Embassy's natural, budgeted and chunked policies
needed **no explicit handoffs** during that I/O profile: the timer-backed
future suspended naturally.

An await that is immediately ready does not suspend. A cooperative service
therefore needs bounded ready-loop work, and a long synchronous handler must
be split if peers need timely service. A between-handler budget cannot
interrupt the handler itself.

The latest matched **four-platform** CPU cohort is the October 5 six-patch
compiler matrix: all 312,000 offered messages arrived correctly, with no
rejections. Normal, medium-work and simulated-I/O profiles met their tested
deadlines. The long profile adds 400,000 arithmetic iterations to one handler;
only the chunked policy splits them into 10,000-iteration pieces.

| Long-CPU profile, four runs each | Longest handler | Peer queue maximum | Deadline misses |
| --- | ---: | ---: | ---: |
| NuttX C | 11.133 ms | 10.920 ms | 11 |
| NuttX Rust, patched compiler | 13.759 ms | 11.228 ms | 50 |
| Zephyr C | 10.879 ms | 10.035 ms | 0 |
| Embassy natural, patched compiler | 10.032 ms | 21.255 ms | 16 |
| Embassy chunked, patched compiler | 13.615 ms | 1.251 ms | 28 |

Chunking protects peers but adds elapsed time to its own handler. Loss-free
delivery is not deadline qualification. Maxima are observations, not hard
bounds; this table does not isolate queue overhead.

A later NuttX-only alignment check received all 124,800 messages; longest
C/Rust handlers were 11.311/11.762 ms, with 1/16 deadline misses. It is not a
new four-platform ranking. The [compiler assessment](../tests/arithmetic-parity/RESULTS.md)
explains the code-generation fixes and remaining gaps. Its frozen 29-patch
candidate is opt-in, not default SDK or CI activation.

## A lean NuttX application check

The [LED-service demo](../tests/service-qualification/README.md) uses real GPIO
level readback, native MQ/threads, one wait point and retained outbound events
under backpressure. Its 16-byte events and 1/8/8 queue capacities differ from
the multicast fixture above. It retains ordinary Rust startup.

| October 9, twenty-service demo | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Code + initialized data | 174,536 B | 184,770 B | +10,234 B |
| Flash binary size | 213,380 B | 214,040 B | +660 B |
| Full-capacity total RAM | 193,596 B | 193,924 B | +328 B |
| Observed peak total RAM | 193,984 B | 194,312 B | +328 B |

The RAM delta is also 328 B at three services; worker/queue allocation growth
matches C. Twenty-service medians of per-run mean LED latency are
327.3 µs C / 311.0 µs Rust; worst observations are 953.7 / 1,032.5 µs.
There were 0 / 2 events over 1 ms out of 3,000 per language.

This fits the demo budget, not a complete product. Zephyr/Embassy were not
tested with this lean workload. PSRAM-enabled builds do not qualify a
no-PSRAM product, and physical interrupt latency remains untested without a
jumper. Earlier latency differences were sensitive to flash/code layout;
no production padding or IRAM workaround was selected.

## Development choice

| Approach | Benefit | Cost or constraint |
| --- | --- | --- |
| nxrs / Rust on NuttX | Rust app types/ownership, existing drivers and POSIX/std options, preemptive blocking work | Broader platform footprint, FFI boundaries, deliberate API and RAM budgeting |
| C on NuttX | Same native facilities; small matched application | Manual ownership/lifetime discipline; common OS costs remain |
| Zephyr C | Compact tested image, native wait-any queues, good tested CPU-load response | Different driver/configuration ecosystem; threaded RAM remains substantial |
| Embassy Rust | Much lower execution RAM, bounded channels/tasks, naturally suspending I/O | Async drivers and bounded synchronous work; no in-handler preemption on this executor |

The evidence supports continuing with Rust on NuttX, not calling its overhead
zero. For a hard 250 kB budget with many mostly-waiting services, async execution
deserves consideration. The next useful qualification is actual interrupt-driven
I/O under concurrent traffic, with publication/completion deadlines and a full
driver/buffer budget—not more synthetic compiler tuning.

## Evidence and reproduction

- [Shared event-service contract and local tools](../tests/event-services-comparison/README.md)
  and [numeric evidence index](../tests/event-services-comparison/results/README.md).
- [nxrs before/after demo](../tests/service-footprint/README.md) and
  [current LED qualification](../tests/service-qualification/README.md).
- [Compiler coverage and assessment](../tests/arithmetic-parity/RESULTS.md);
  [figure regeneration](assets/rtos-comparison/README.md).

Separate workloads and compiler cohorts are not pooled. Reports retain numeric
runs and artifact/configuration provenance, without private backups or raw
device identifiers. Zephyr/Embassy are isolated experiments, not production
workspace dependencies.

[^flash-size]: Code + initialized data counts stored firmware ELF sections,
    including read-only data and initial RAM values. Flash binary size includes
    boot components, headers and offset/alignment padding. Debug symbols enlarge
    the separate ELF artifact, not either figure; uninitialized RAM such as
    `.bss` is excluded from both.
