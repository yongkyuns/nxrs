# Event-driven services on NuttX, Zephyr and Embassy

This study asks a practical question: what do we gain and give up when twenty
independent services communicate through bounded event queues on a small MCU?
It compares NuttX C, nxrs-style Rust on NuttX, Zephyr C and Embassy Rust on the
same ESP32-S3, using local builds and device measurements, not CI.

The results support Rust as a small NuttX application layer when it reuses
native OS facilities. They do **not** establish that a twenty-thread NuttX
configuration fits a 250 kB RAM device. Embassy uses much less execution
storage in this fixture, but a cooperative service must keep synchronous work
bounded. Zephyr's native C configuration handles the tested heavy work well;
its whole-image figures are not an equal-feature replacement for NuttX.

The current evidence is a useful architecture screen, not final product
qualification. There is no universal winner independent of drivers, workload,
API requirements and memory budget.

## What the application does

Each service owns its state, receives events from several other services and
publishes control, data and status events on an absolute calendar. There are
no replies or acknowledgement round trips. A service has one wait point for
all its inboxes and its next publication deadline; it does not wait on queues
one at a time with separate timeouts.

![Several senders feed bounded inboxes at one service wait point](assets/rtos-comparison/service-loop.svg)

The main layout has twenty services and sixty queues, each with eight 64-byte
slots: **30,720 bytes of payload capacity**. A second layout combines each
service's three class queues into one 24-slot mailbox, preserving total
capacity. Separate queues isolate capacity by event class; one mailbox shares
capacity and FIFO order. Neither layout makes a slow receiver faster.

Sends are nonblocking and never retry, overwrite or silently drop accepted
messages. The checker verifies accepted/received counts, ordering and payloads.
Zero rejections is a separate requirement: a correct queue cannot guarantee
loss-free delivery if producers offer more work than the receiver can process.
Our lighter profiles satisfy that requirement; some long-CPU profiles do not.

Normal traffic offers 3,900 multicast deliveries in two seconds and burst
traffic offers 6,240, followed by a 500 ms drain. Slow cases cannot pass by
quietly reducing the offered traffic. Additional profiles make just one data
handler do more arithmetic or wait at least 3 ms for simulated I/O. This is
not an IMU, radio or storage driver benchmark.

## What is held equal, and what is not

| Controlled application choices | Deliberate platform differences |
| --- | --- |
| One 240 MHz core, flash/cache settings, payloads, routing and release calendar | POSIX mqueues/pthreads, Zephyr msgqs/threads, or Embassy channels/tasks |
| Application `-O2`, common timestamps, handler algorithm and queue capacity | Native kernel configuration, boot layout and linked platform services |
| One wait-any point, round-robin ready inboxes, same deadline checks | Preemptive OS threads versus one cooperative executor |
| NuttX C/Rust share a kernel config and native queue/thread boundary | NuttX retains shell, VFS, libc and board services; other images use narrower APIs |
| Native service stacks are 20 × 4 KiB in both languages and both RTOSes | Embassy has an 8 KiB shared stack plus saved task state |

The Rust event-service image does not use `std::thread`, `std::mpsc`, Crossbeam
or `println!`. Its service logic is compiled from Rust, but it calls the same
native NuttX queue/thread adapter as C. That isolates a useful **application
language** comparison; it does not measure the convenience of a full std API.
We tested Zephyr in C, not its Rust integration.

![Preemptive service threads versus cooperative tasks with saved future state](assets/rtos-comparison/execution-models.svg)

Common thread stacks are execution-model storage, **not Rust or messaging
overhead**. We have not reduced them to improve the comparison. Embassy's
different storage model is real, but its saved futures and shared stack must
still be included. For broader platform capabilities, see the official
[NuttX overview](https://nuttx.apache.org/docs/latest/introduction/about.html),
[Zephyr polling](https://docs.zephyrproject.org/latest/kernel/services/polling.html)
and [Embassy executor documentation](https://docs.embassy.dev/embassy-executor/git/std/index.html).

## RAM: provision for capacity, not just the observed traffic

NuttX allocates queued message storage on demand. Zephyr and Embassy reserve
their queue buffers in the image. A quiet traffic run therefore understates
NuttX's provisioned-capacity requirement relative to a static implementation.
The full-capacity control fills and drains all 480 slots, repeatedly, and
deliberately tests one additional rejected send per queue.

![Full-capacity RAM, with common service stacks and test fields separated](assets/rtos-comparison/ram-capacity.svg)

| Full-capacity control | 60 class queues | 20 combined mailboxes |
| --- | ---: | ---: |
| NuttX C | 257,184 B | 245,280 B |
| NuttX Rust, native threads | 257,576 B | 245,360 B |
| Zephyr C | 230,936 B | 228,056 B |
| Embassy Rust | 84,864 B | 83,584 B |

NuttX totals are resident ELF RAM plus allocator high-water; statically
reserved buffers, stacks and arenas in Zephyr/Embassy are counted once.
All 32 full-capacity invocations qualified: 96 fill/drain cycles, 46,080
deliveries and 3,840 intentional overflow rejections. These establish the
fixture's exercised capacity, not a bound for future application allocations.

The 250 kB reference is 250,000 bytes, not 250 KiB and not the ESP32-S3's actual
usable RAM. The 60-queue NuttX fixture already exceeds it. A combined mailbox
barely comes under it, leaving too little margin to claim suitability for a
250 kB product. Zephyr also leaves limited headroom in this diagnostic build.
Embassy leaves substantially more, before adding real drivers and business
state. Chip-reserved memory and product safety margin still need a budget.

This does not mean queues alone consume 250 kB. Native service stacks reserve
81,920 bytes; the full-capacity fixture has 30,196 nominal bytes of test
diagnostics. The chart separates those two common costs from everything else,
not from a fully itemized kernel allocator. Subtracting test fields illustrates
where space goes; it is **not** a rebuilt or qualified lean image.

The final I/O/scheduling images have different instrumentation and task state:
Embassy reserves 87,264 bytes, including 30,720 payload bytes, 1,920 queue
metadata bytes, a 4,320-byte service-future pool and an 88-byte clock-future
pool. NuttX's traffic peaks there are 222,416 B in C and 224,256 B in Rust.
Those lower traffic peaks do not supersede the full-capacity result above.
The observed C/Rust heap difference depends on occupancy and scheduling; it
is not a measured per-thread Rust allocation cost.

For planning, budget payload storage and queue objects per queue; task state
or thread execution storage per service; and application buffers per owner.
An async future that holds a large buffer across an await also consumes RAM.
Static allocation makes that reservation visible, not free.

## Image size: the language delta is small in the native-API design

The final scheduling images compare the firmware's code + initialized data
with the size of the binary file used for flashing.[^flash-size]

![Firmware code + initialized data and flash binary size](assets/rtos-comparison/image-size.svg)

| Final scheduling image | Code + initialized data | Flash binary size |
| --- | ---: | ---: |
| NuttX C | 176,884 B | 214,508 B |
| NuttX Rust, native threads | 177,708 B | 214,532 B |
| Zephyr C | 88,167 B | 142,312 B |
| Embassy Rust, natural waits | 69,461 B | 182,496 B |

The matched Rust app adds **824 bytes of code + initialized data and 24 bytes
to the flash binary** over C. Padding can absorb a code increase until an
alignment boundary is crossed; it does not make that extra code free. The four
Embassy scheduling policies span only 424 bytes of code + initialized data and
have the same RAM reservation.

All these images are comfortably smaller than 2 MB of flash, but platform
totals include unequal features. Zephyr's smaller image does not identify a
specific inefficient NuttX kernel function. Embassy's flash binary is larger
than Zephyr's despite less code + initialized data because their layouts differ.
Compare code + initialized data to understand program costs, and the actual
partition layout to decide whether firmware fits.

### What the earlier nxrs optimizations teach

The [nxrs development walkthrough](../tests/service-footprint/DEVELOPMENT.md)
keeps before/after choices in one demo rather than copying each experiment
into another framework. The [Rust footprint analysis](rust-std-footprint.md)
contains their separate controlled measurements. They are not an additive
ladder: changing one fixture can change which shared code the linker retains.

| Choice | What to adapt or avoid | How cost grows |
| --- | --- | --- |
| Debug ELF versus programmed sections | Do not treat the initial roughly 230 kB ELF-file difference as deployable code | Debug/build artifacts are not flash load |
| Formatted logging | Use `println!` when its formatting/I/O behavior is needed; keep diagnostics out of hot paths and lean products | Mostly shared first-use support; new formatting types can retain more code |
| Rust `main` versus embedded entry | Use the app's required lifecycle; a C-compatible embedded entry omits ordinary process startup/cleanup | Shared runtime support, not a charge per message |
| `std::thread` versus native adapter | Keep std when its lifecycle/result/parking behavior is useful; the narrow adapter is not a complete substitute | Shared code plus per-thread objects and closure/result specializations |
| Queue transport | Reuse OS queues where their delivery/wait semantics meet the app; do not choose solely from a library's size | Payload/metadata per queue; shared operations plus generic/type-specific code |
| Borrowed buffers and shared byte helpers | Reuse storage and outline common I/O; verify the target result rather than assuming every generic API is free | Buffers per owner; specialized code can recur per type |
| Packet sample construction | Fill the destination array directly instead of making an unnecessary temporary | Less stack high-water; unchanged reserved stack size |

Linked std support is commonly a first-use cost, **not one copy per service**.
That does not make all future growth fixed: new API families, generic types,
business handlers, buffers and concurrent owners add code or storage. Earlier
container probes found modest `Vec` support and a larger first-use `HashMap`
cost in their fixture; that is a reason to measure actual use, not ban them.

In a separate twenty-idle-thread control with equal stacks/kernel/entry,
`std::thread` added 10,808 bytes of code + initialized data and 4,792 peak heap
bytes over the native adapter. Roughly 2.4 kB for ten threads or 3.6 kB for
fifteen is a planning interpolation, **not a measured scaling curve**. The common 4 KiB
stacks are excluded from that delta. Both variants use the same OS scheduler;
this control does not establish a steady-state messaging speed tax.
See the [thread control and its narrower lifecycle](../tests/event-services-comparison/SCHEDULING.md#what-stdthread-adds).

## Speed: measure the whole path and the receiver's capacity

![Publication lateness, queue response and handler duration are separate parts of delivery](assets/rtos-comparison/latency-path.svg)

The panels show consecutive intervals with shared endpoints, not timelines
drawn to scale. Release-to-handler response equals publication lateness plus
queue response.

Queue response starts when the send call starts. Release-to-handler response
also counts late publication. A receiver can respond promptly to a message
that should have been sent much earlier. Handler duration includes time asleep
or off CPU; it is not CPU utilization. We retain per-service deadlines and
rejected-send counts as well as latency, rather than letting an aggregate
average hide a stalled owner.

### Wake configuration can dominate a small-handler benchmark

![Matched 10 ms and 1 ms publication wake controls](assets/rtos-comparison/timer-response.svg)

With normal traffic, changing publication wake resolution from 10 ms to 1 ms
reduces median per-run release-to-handler p99 from about 12–22 ms to 1.5–2.2 ms.
This matched control is separate from the final I/O matrix. It explains why
the original headline latency was not simply queue-library speed. The native
10 ms preemptive timeslice is unchanged; a wake timer and a timeslice are
different settings. More frequent wakes may have CPU/power costs, which were
not measured here.

### Efficient async I/O does not require an unconditional yield

In the final matrix, all 140 invocations outside the long-CPU profile had
zero rejected sends and zero deadline misses. Every simulated-I/O run completed
117 operations. Embassy's natural, budgeted and chunked variants used **zero
explicit handoffs in that I/O profile**: the timer-backed I/O future returns
`Pending` and wakes later. Native threads sleep instead.

| Final scheduling matrix | Normal peer queue p99 | I/O peer queue maximum |
| --- | ---: | ---: |
| NuttX C | 0.399 ms | 0.468 ms |
| NuttX Rust, native threads | 0.857 ms | 0.908 ms |
| Zephyr C | 0.351 ms | 0.362 ms |
| Embassy, natural waits | 0.512 ms | 0.572 ms |
| Embassy, budgeted/chunked | 0.472 ms | 0.561 ms |

Peer figures exclude the one service doing extra work. p99 is the median of
four per-run worst-peer histogram upper bounds, not a pooled percentile.
Maximum is the largest observation in those runs, not a hard timing guarantee.
The differences include service code, publication and scheduler interactions;
they do not isolate queue operation cost. These short runs do not show that
removing explicit yields is always faster, or that NuttX Rust always matches C.

An await that completes immediately does not suspend a task. Sustained ready
traffic can therefore keep a cooperative poll running, even if the source uses
`async` throughout. A between-handler budget can hand off after four events
or 500 µs, but it cannot interrupt the handler itself. This follows the
[Rust future contract](https://doc.rust-lang.org/std/future/trait.Future.html);
Embassy's fairness between polls cannot preempt a synchronous CPU loop.

### Long CPU work exposes different limitations

The long profile adds 400,000 arithmetic iterations to one data handler.
Only the chunked policy splits those same iterations into 10,000-iteration
pieces. Counts below are totals across four runs; queue maxima are peer maxima.

| Long CPU profile | Peer queue maximum | Rejected sends | Deadline misses, all classes |
| --- | ---: | ---: | ---: |
| NuttX C | 10.955 ms | 0 | 10 |
| NuttX Rust, native threads | 25.163 ms | 120 | 727 |
| Zephyr C | 10.035 ms | 0 | 0 |
| Embassy, natural waits | 185.643 ms | 2,376 | 10,992 |
| Embassy, budgeted between handlers | 95.114 ms | 108 | 5,906 |
| Embassy, budgeted/chunked | 1.979 ms | 108 | 437 |

Chunking protects peer responsiveness, but it does **not** make the overloaded
service loss-free. It completes only 90 of 117 offered CPU jobs per run; its
own queue response reaches about 186 ms. Peer release-to-handler maximum is
still 35.996 ms because publication by the busy service waits for its current
handler. Chunking also increases that handler's longest wall-clock duration
from about 21.7 to 27.3 ms by allowing other tasks to run.

The C/Rust arithmetic is equivalent, but the Xtensa compiler emits different
code in this synthetic loop. The [compiled-loop control](../tests/event-services-comparison/CONTROLS.md#longer-handlers-small-tasks-work-unbounded-synchronous-work-does-not)
traces that difference; it is not a `std::thread` or queue overhead explanation.
This remains a concrete target-compiler risk for CPU-heavy code, not evidence
that every Rust service will be slower. Benchmark product algorithms and
inspect their assembly if compute time matters. Zephyr met every tested long
deadline here; this is still not an isolated scheduler or hard-real-time proof.

## Choosing a development approach

| Approach | What you gain | What you give up or must manage |
| --- | --- | --- |
| nxrs / Rust on NuttX | Rust ownership/types for app logic; familiar POSIX and std options; existing NuttX drivers, filesystem/network APIs; preemptive execution for blocking handlers | Broader platform footprint; queue/allocator overhead; target compiler validation; unsafe FFI boundaries and a product RAM budget |
| C on NuttX | Same native APIs and preemptive execution; smallest matched native app in this study; direct integration with existing C code | Manual ownership/lifetime discipline; no Rust type/ownership checks for application code; common OS resource costs remain |
| Zephyr C, native APIs | Compact tested image; native wait-any queues; preemptive threads; best tested long-profile completion/deadlines | Different driver/API integration and configuration; substantial threaded RAM in this fixture; std/POSIX parity and Rust integration were not tested |
| Embassy Rust, single executor | Much less execution RAM here; bounded static channels/tasks; efficient suspending I/O and Rust app types | Async-capable drivers; saved future state; no preemption within a synchronous handler; bounded ready-loop/CPU work and explicit overload planning |

These are development tradeoffs, not claims that one platform is secure,
memory-isolated or universally safer. Rust checks do not prove C drivers,
unsafe boundaries or delivery protocols correct. Embassy also supports other
executor/priority arrangements; this study qualifies only the single-executor
configuration above. Supporting another RTOS in nxrs is not implied by the
temporary experiments.

For nxrs, the evidence supports keeping Rust service/HAL logic and choosing
runtime APIs deliberately. A native adapter can make the language delta small,
but the normal std API can be a reasonable few-kilobyte trade when its behavior
is needed. Avoiding every std feature is not the objective. For a hard 250 kB
product budget with many mostly-waiting owners, an async design is worth serious
consideration; the current twenty-thread NuttX fixture is not qualified for it.

Before selecting a product architecture, the next useful test is a real
interrupt-driven I/O HAL under concurrent traffic, with publication **and
completion** deadlines, full-capacity memory, CPU/idle power and a high-ready-load
case. Neither a timer simulation nor this arithmetic loop substitutes for the
actual sensor, radio, storage and processing workload.

## Reproduce or inspect the packaged experiments

| Package | Purpose |
| --- | --- |
| [nxrs footprint demo and walkthrough](../tests/service-footprint/DEVELOPMENT.md) | Existing before/after entry, thread, queue and packet-construction variants; matched C controls |
| [Event-service comparison](../tests/event-services-comparison/README.md) | Shared workload contract, isolated platform implementations and local build/measure tools |
| [Capacity and timing controls](../tests/event-services-comparison/CONTROLS.md) | Full-queue checks, wake controls and compiled-handler investigation |
| [Scheduling follow-up](../tests/event-services-comparison/SCHEDULING.md) | Natural I/O, ready-loop budgets, chunking and thread-wrapper evidence |
| [Zephyr experiment](../tests/zephyr-comparison/README.md) / [Embassy experiment](../tests/embassy-comparison/README.md) | Pinned setup and original worker/reply controls; historical timings are not pooled here |
| [Figure sources](assets/rtos-comparison/README.md) | Editable D2 diagrams and evidence-driven SVG chart regeneration |

The public reports retain image/source hashes, configuration, rotated blocks,
per-run qualification and numeric summaries. They exclude private flash backups
and raw device identifiers. The full-capacity and scheduling tables use different
frozen images and are labeled separately. Earlier worker/reply results are
historical controls, not extra samples of this event-service workload.

The final scheduling evidence covers seven images, six profiles and four runs
each: 168 invocations, 720,720 attempted deliveries, zero protocol errors and
4,272 explicit rejections only in the long-CPU profile. The original device
flash was restored and verified after measurement. Build outputs, SDK trees,
private backups and raw serial logs stay outside the change; Zephyr/Embassy
remain local experimental dependencies, not production workspace members.

[^flash-size]: Code + initialized data counts firmware ELF sections stored in
    flash, including read-only data and initial values for RAM data. Flash
    binary size is the combined `.bin` file, also including boot components,
    image headers and offset/alignment padding. These packaging bytes account
    for the difference between the columns. Debug symbols are excluded from
    both; they enlarge the separate ELF artifact, not the flash binary.
    Uninitialized RAM sections such as `.bss` are also excluded from both.
