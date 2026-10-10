# Event-driven services on NuttX, Zephyr and Embassy

Twenty services and sixty bounded queues run correctly on all four tested
implementations. Native Rust on NuttX adds little over matched C. Embassy saves
execution RAM, but synchronous work blocks its single cooperative executor.
Zephyr C handles the tested heavy work well. These are fixture results,
not finished-product qualification.

## Workload and fairness

Each service owns an event loop, receives from multiple senders and has one
wait point for its inboxes and publication calendar. There are no reply
round trips or sequential queue-poll timeouts.

![Multiple senders, bounded inboxes and one service wait point](assets/rtos-comparison/service-loop.svg)

The layout is 20 services × 3 queues × 8 slots × 64 bytes: **30,720 bytes
of event storage**. Each test event has 44 dummy payload bytes and 20 routing,
timing and validation bytes; production chooses its own schema/capacity.
A 20-mailbox alternative preserves capacity by combining
each service's inboxes into one 24-slot FIFO. Normal traffic offers 3,900
deliveries in two seconds; burst traffic offers 6,240, then a 500 ms drain.

Accepted events are checked for delivery, order and payload correctness.
Nonblocking sends can reject overload; correct queues alone cannot guarantee
that every offered event fits or meets its deadline.

| Held equal | Different by design |
| --- | --- |
| One 240 MHz core, flash/cache settings, routing, payloads, calendar and application `-O2` | Native POSIX, Zephyr or Embassy facilities |
| Same minimal NuttX kernel configuration and C/Rust native adapter; NuttX/Zephyr kernel `-Os`, applications `-O2` | NuttX retains POSIX/VFS/libc; Zephyr and Embassy expose different native APIs |
| 20 × 4 KiB native worker stacks in C and Rust | Embassy uses an 8 KiB shared stack plus saved task state |

![Preemptive threads versus cooperative tasks](assets/rtos-comparison/execution-models.svg)

NuttX Rust here does **not** use `std::thread`, std MPSC, Crossbeam or
`println!`. This isolates application-language cost, not full std convenience.
Zephyr Rust integration and other Embassy executor arrangements were not tested.

## RAM: count provisioned capacity

NuttX allocates messages on demand; Zephyr/Embassy reserve buffers statically.
Compare full capacity, not quiet-run heap: the control fills/drains every slot
and deliberately rejects overflow.

![Matched RAM: execution, queue capacity, adapter controls, fixture application state, checker overhead, code and runtime](assets/rtos-comparison/ram-capacity.svg)

| October 10 matched capacity control | Full telemetry | Lean qualification | Saved |
| --- | ---: | ---: | ---: |
| NuttX C, minimal | 247,204 B | 221,092 B | 26,112 B |
| NuttX Rust, minimal / native threads | 247,220 B | 221,108 B | 26,112 B |
| Zephyr C | 231,492 B | 205,380 B | 26,112 B |
| Embassy Rust, natural waits | 87,284 B | 61,172 B | 26,112 B |

All 48 invocations passed without PSRAM; original flash was restored/verified.
Rebuilt lean images remove only 102 × 64 × 4 histogram bytes, retaining
scalar/delivery checks—not latency distributions or a production minimum.

### What is required, chosen, or added by the test?

Bytes below reconcile to the **lean** totals. “Required” means required by this
implementation, not an irreducible cost of the language or RTOS.

| RAM portion | NuttX C / Rust | Zephyr C | Embassy | How to interpret it |
| --- | ---: | ---: | ---: | --- |
| Service execution workspace | 81,920 | 81,920 | 12,512 | Native: 20 × 4 KiB stacks. Async: 8 KiB shared stack + 4,320 B service futures. Required state; application-sized. |
| Chosen event capacity | 30,720 | 30,720 | 30,720 | Artificial: 21,120 dummy payload + 9,600 routing/timing/check bytes. Choose real messages/burst capacity. |
| Queue/adapter controls | 18,512 | 7,040 | 1,932 | Selected metadata/lifecycle bookkeeping, including test choices; not payload or a universal minimum. |
| Fixture application state | 3,040 | 3,040 | 3,040 | Synthetic flow/digest state, not messaging infrastructure. Real services need their own state. |
| Checker/coordinator overhead | 4,424 | 4,432 | 5,059 | Diagnostics, gates and padding; not required by nxrs. Full telemetry adds 26,112 B. |
| RAM code/vectors | 37,732 | 49,576 | 5,912 | Instructions/vectors, not heap. Shared here; changes with features. |
| Other OS stacks/arena | 7,168 | 18,432 | 0 | Configuration-selected. Zephyr includes a 4,096 B heap arena; zero does not mean no runtime state. |
| Runtime storage not individually split | 37,576 / 37,592 | 10,220 | 1,997 | Linked data/padding; NuttX also includes live heap and transient high-water. Mixed, **not certified unavoidable**. |

NuttX's controls comprise 5,200 B adapter objects, 7,552 B dynamic message
headers/allocator alignment, and 5,760 B static-pool spare/headers. Eight events
use the static pool; 472 heap messages cost 80 B each for 64 B of contents.
Pool spare includes unused IRQ/System-V reservations and oversized preallocated
messages—configuration costs, not application requirements. The remaining
37,576 B is 9,360 B linked storage + 27,792 B live heap beyond worker stacks
+ 424 B peak margin. That heap mixes main-task stack, thread/queue/VFS objects
and runtime/test allocations; its fixed-versus-per-service split is unmeasured.
The [ELF-bound ledger](../tests/event-services-comparison/README.md#ram-attribution)
retains object sizes and avoids pool/payload and arena/heap double counting.

### Conclusions a developer can use

Native Rust adds **16 B RAM** here; C shares execution costs. Embassy saves
**69,408 B execution workspace**, plus platform/runtime differences—not event
capacity. Futures grow with locals held across awaits. Four slots halve event
storage, not all queue costs.
Execution/metadata recur with services/queues. RAM code/platform reservations
are shared only for this configuration.

Lean NuttX/Zephyr leave **29/45 kB against 250,000 B**, before product drivers,
buffers and state. The total is not “messaging overhead”; subtracting test bytes
does not qualify a product. Rebuild with real workloads. Image/timing cohorts differ.

## Image size: distinguish contents from address span

October 9 image measurements use minimal NuttX and frozen Zephyr/Embassy builds.[^flash-size]

![Code + initialized data and gap-free package size](assets/rtos-comparison/image-size.svg)

The minimal profile removes shell/board utilities, unused UART/random/C++/float
printing, environment/child-task bookkeeping and PSRAM. Native queues, `poll`,
pthreads, LED readback, assertions, stack coloration, timing, TLS and ABI remain.
Kernel/libc use `-Os` like Zephyr; handlers remain `-O2`. This workload-specific
profile is not a general-purpose std configuration.

| Current image | Stored contents, no gaps | Gap-free package | Flash address span |
| --- | ---: | ---: | ---: |
| NuttX C | 114,764 B | 115,835 B | 138,992 B |
| NuttX Rust | 115,580 B | 116,651 B | 139,008 B |
| Zephyr C | 88,248 B | 89,319 B | 142,312 B |
| Embassy Rust | 91,012 B | 92,362 B | 182,656 B |

The matched NuttX comparison across the earlier NSH/board baseline and minimal
benchmark was:

| Matched NuttX profile | C code + initialized data | Rust code + initialized data | C / Rust flat flash file |
| --- | ---: | ---: | ---: |
| Earlier NSH/board baseline | 176,884 B | 177,708 B | 214,508 / 214,532 B |
| Minimal benchmark | 114,667 B | 115,483 B | 138,992 / 139,008 B |

NuttX C's code + initialized data falls by **62,217 B (35%)** and its binary
by **75,516 B (35%)**. These are combined configuration/build-policy savings,
not an attribution to any single subsystem. Rust adds **816 B of code +
initialized data**, also **816 B to the gap-free package**, over matched C.
The flat `.bin` grows only 16 B because the extra code consumes existing padding.

Uncompressed ZIP packages omit validated gaps, retaining actual zero-filled
data. Manifest-driven reconstruction reproduces each measured SHA-256. Packages
are not bootable: flashing uses reconstructed `.bin` files with required
alignment. The on-device address span does **not** shrink.

Embassy additionally stores a 21,072 B bootloader and 128 B partition records;
NuttX/Zephyr embed simple-boot loading. Its 91,644 B gaps are not debug data.
Smallest application need not mean smallest bootable system. All spans fit
2 MB; budget partitions by span, not ZIP size. Boot/linker policy is unchanged.

Remaining NuttX C bytes, from its ELF and retained link-map intervals:

| Component | Bytes |
| --- | ---: |
| Architecture/HAL, startup and board code | 42,663 |
| Scheduler, threads, signals and queues | 20,024 |
| VFS code | 8,385 |
| libc, drivers and heap code | 15,850 |
| Benchmark code | 8,938 |
| In-section padding/unattributed code bytes | 1,867 |
| Constants and initialized data | 16,940 |
| Total | 114,667 |

Architecture/HAL includes boot and flash initialization. These platform costs
are shared by C and Rust; they are not a Rust tax or debug information.

For formatting, startup, thread wrappers and containers, see the separate
[Rust footprint analysis](rust-std-footprint.md). Those choices should not be
charged to this native-API fixture.

## Speed: responsiveness is not queue-operation time

![Publication lateness, queue response and handler duration](assets/rtos-comparison/latency-path.svg)

Release-to-handler includes publication lateness and queue response. Handler
duration includes sleeps/preemption, not just CPU work. Tail latency, deadline
misses and rejected sends expose stalls that means hide.

![Matched publication wake controls](assets/rtos-comparison/timer-response.svg)

Changing wake resolution from 10 ms to 1 ms reduces median per-run response
p99 from 12–22 ms to 1.5–2.2 ms, without changing the native 10 ms timeslice.
Finer wakes' CPU/power cost was not measured.

### I/O and CPU work need different treatment

October 9 delivered all 205,920 messages without rejection. All 40 traffic
invocations outside long-CPU met deadlines; each simulated-I/O run completed
117 operations. Embassy's timer-backed I/O suspended naturally, without explicit
handoffs, as in earlier budgeted/chunked controls.

An await that is immediately ready does not suspend. A cooperative service
therefore needs bounded ready-loop work, and a long synchronous handler must
be split if peers need timely service. A between-handler budget cannot
interrupt the handler itself.

The October 9 cohort retains the frozen six-patch Rust compiler, without
further tuning. The long profile adds 400,000 arithmetic iterations to one
handler; all four implementations process it monolithically.

| Long-CPU profile, two runs each | Longest handler | Peer queue maximum | Deadline misses |
| --- | ---: | ---: | ---: |
| NuttX C, minimal | 11.267 ms | 10.993 ms | 2 |
| NuttX Rust, minimal / patched compiler | 12.839 ms | 10.929 ms | 23 |
| Zephyr C | 10.879 ms | 10.035 ms | 0 |
| Embassy natural, patched compiler | 10.032 ms | 21.002 ms | 4 |

Earlier chunked Embassy controls reduced peer queue maximum to 1.251 ms but
increased elapsed handler time; samples stay separate. Loss-free delivery is
not deadline qualification. Observed maxima/two runs establish neither hard
bounds nor stable speed rankings. The
[compiler assessment](../tests/arithmetic-parity/RESULTS.md) covers remaining gaps;
its 29-patch candidate is opt-in, not SDK/CI activation.

## Lean NuttX application check

The separate [LED qualification](../tests/service-qualification/README.md) uses
16-byte events, 1/8/8 queue capacities, GPIO readback and ordinary Rust startup
without PSRAM. It includes footprint, restart/fault and sustained/pressure
checks. No Zephyr/Embassy comparison or IRQ-latency qualification applies there.

## Development choice

| Approach | Benefit | Cost or constraint |
| --- | --- | --- |
| nxrs / Rust on NuttX | Rust app types/ownership, existing drivers and POSIX/std options, preemptive blocking work | Broader platform footprint, FFI boundaries, deliberate API and RAM budgeting |
| C on NuttX | Same native facilities; small matched application | Manual ownership/lifetime discipline; common OS costs remain |
| Zephyr C | Compact tested image, native wait-any queues, good tested CPU-load response | Different driver/configuration ecosystem; threaded RAM remains substantial |
| Embassy Rust | Much lower execution RAM, bounded channels/tasks, naturally suspending I/O | Async drivers and bounded synchronous work; no in-handler preemption on this executor |

Rust on NuttX remains viable; async execution deserves consideration for tight
RAM budgets and mostly-waiting services. Next, qualify interrupt-driven I/O
under concurrent traffic, with deadlines and a full driver/buffer budget.

## Evidence and reproduction

- [Shared event-service contract and local tools](../tests/event-services-comparison/README.md)
  and [numeric evidence index](../tests/event-services-comparison/results/README.md).
- [nxrs before/after demo](../tests/service-footprint/README.md) and
  [current LED qualification](../tests/service-qualification/README.md).
- [Compiler coverage and assessment](../tests/arithmetic-parity/RESULTS.md);
  [figure regeneration](assets/rtos-comparison/README.md).

Reports retain numeric runs and artifact/configuration provenance, not private
backups/device identifiers. Cohorts stay separate; Zephyr/Embassy are optional
experiments, not production dependencies.

[^flash-size]: Code + initialized data counts stored firmware ELF sections,
    including read-only data and initial RAM values. Stored image contents add
    boot components/headers but exclude explicit gaps; packages add ZIP/manifest
    overhead without compression. Flash address span includes gaps needed by
    the chosen boot/flash layout. Debug symbols stay in the separate ELF;
    uninitialized RAM such as `.bss` is not stored in these files.
