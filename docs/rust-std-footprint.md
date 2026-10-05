# Rust std footprint on NuttX

Rust std is a viable candidate for nxrs application and service code, provided
we choose its runtime, messaging, and logging features deliberately. The tests
do not establish a universal Rust size penalty or prove that a finished product
will fit in 250 kB of RAM.

This summary covers the local ESP32-S3 measurements through October 3, 2026.
The [matched demo](../tests/service-footprint/README.md) contains C and Rust
implementations and regression tests. Its [recorded results](../tests/service-footprint/results/esp32s3-2026-10-03.json)
preserve the latest sizes, timings, heap accounting, and artifact hashes.
The cleaned source is not byte-identical to the historical firmware; rerun the
measurements before attributing these exact numbers to a new build.

For the subsequent event-loop architecture, capacity tests and async/preemptive
tradeoffs, see the independent [RTOS comparison](rtos-comparison.md).
The [development walkthrough](../tests/service-footprint/DEVELOPMENT.md) maps
the before/after choices below to the existing demo's features and build tools.

## What makes the comparison fair

The first navigation skeleton was not a fair language comparison: C did less
work. We replaced it with matched LED services using the real device driver,
a worker, commands, replies, readback, and shutdown. Later tests used identical
synthetic packet processing in both languages.

The latest workload has four producers, fifteen workers, and one collector.
Workers each wait on three input queues. There are 45 capacity-one input
queues and 15 capacity-four reply queues: 60 physical queues and 20 spawned
threads, plus the shell command. Both languages validate 5,760 event/reply
pairs. Events are 248 bytes; replies are 16 bytes for transport alone or
28 bytes for the packet workload.

The packet contains 32 XYZ sample frames. Each worker validates the packet,
updates three persistent fixed-point filter states per stream, and returns
the final states. The collector independently computes and verifies them.
The earlier artificial rolling checksum is not the packet-processing work.

C and Rust share the NuttX configuration, POSIX mqueues, poll selection,
pthread stack reservations, clock helper, and synchronized release gate.
On the device, Rust uses a small experimental pthread adapter and shared
byte-level queue helpers, not Crossbeam or ordinary std thread creation.
Host tests exercise a different transport and cannot replace device tests.

## Image size

### Most of the initial increase was avoidable

An early roughly 230 kB file-size increase was not 230 kB of extra executable
code. Debug information and ELF metadata do not automatically go into the
flash image. Size optimization, fat LTO, and checking final load-bearing
sections gave a more useful comparison. The relocatable Rust input must keep
symbols until NuttX's final link; stripping that input prematurely breaks it.

The measurements below are separate controlled experiments, not successive
steps in one identical configuration.

| Matched workload and Rust choice | Extra linked flash content versus C |
| --- | ---: |
| Concurrent LED and payload with custom one-slot MPSC | 19,750 B |
| Same application with std MPSC | 40,130 B |
| Same application with Crossbeam | 42,062 B |
| Later silent LED with ordinary Rust entry and std worker | 13,042 B |
| Same silent LED with embedded entry and native pthread adapter | 1,956 B |
| Shared-queue silent LED growth study, one service | 1,484 B |
| Same growth study, LED plus status and telemetry | 2,428 B |

MPSC means several producers can send to one consumer. Crossbeam also supports
waiting on several independent queues, so its extra functionality is not free.
These are whole-image increments, not charges for each queue instance.

### Formatting, entry, and thread creation were important roots

Formatting converts values into text, such as turning an integer into
`count=42`. `println!` also uses synchronized stdout, buffering, and error
handling. Removing application output in the later LED comparison saved
3,788 bytes in Rust but only 180 bytes in C, whose output support was already
used by the shell. Even a constant Rust print can retain output machinery.

Ordinary Rust `main` retained startup and cleanup for file descriptors,
signals, arguments, and stdout. A C-compatible embedded entry removed
3,284 bytes in that controlled silent build by leaving task lifecycle to
NuttX. This also removes runtime services; it is not an interchangeable
entry for every Rust application.

Ordinary std thread creation retained naming and identity, parking, spawn
hooks, thread-local state, and join/result/panic bookkeeping. Those
dependencies also appeared as `core`, `alloc`, and inlined application code.
A small native pthread adapter omitted those features and left the
1,956-byte silent LED gap. An experimental minimum-stack-query patch saved
1,588 bytes in a different combined std-thread variant by removing a cached
query's synchronization dependencies. It did not reduce any stack reservation.
That Rust std experiment is not applied by this demo.

### The latest diagnostic pipeline is larger than the silent LED

| Full packet workload | C | Rust | Rust minus C |
| --- | ---: | ---: | ---: |
| Linked flash content | 135,400 B | 155,418 B | 20,018 B |
| Unpadded flash binary | 214,440 B | 215,116 B | 676 B |

The linked difference is exactly 19,262 bytes of flash instructions,
676 bytes of read-only data, and 80 bytes of initialized DRAM data.
Another 104 bytes of BSS affect RAM, not stored flash data. These builds
retain formatted diagnostic reports; the small silent LED result is not a
prediction for this larger reporting fixture.

Flash-segment alignment gaps absorbed much of the extra linked content in
this pair of binaries. The code is still present, and a later change can
cross an alignment boundary. Do not treat the 676-byte file increase as the
complete code cost or count debug sections as flash.

### Shared costs are not the same as free growth

Runtime routines, output engines, and reused channel algorithms generally
enter the image once. A second call does not copy the entire standard
library. New services still add their own logic, strings, error paths, and
specialized generic code for message, closure, result, or container types.
Inlining and LTO can alter how much is shared.

Adding the matched packet computation to the 28-byte-reply transport control
added 972 bytes in C and 1,104 bytes in Rust: the gap grew by 132 bytes,
not another 20 kB. The silent three-service growth test also reused most
support code. Neither experiment proves that every future service grows
at the same rate.

A separate container ladder found first-use increments of 316 bytes for a
bounded Vec, 4,684 bytes for a default HashMap, 1,556 bytes for decimal
formatting, and 224 bytes for decimal parsing. These are context-dependent
feature increments, not prices per container. Additional element/key types
and operations can introduce more specialized code. Reserve capacities and
measure the actual linked application rather than banning all containers.

## RAM

### The roughly 206 kB figure is a stress-fixture total

It is not the memory used by messaging alone, a minimum nxrs budget, or a
Rust-only cost. This deliberately large topology reserves 83,968 bytes for
its twenty spawned thread stacks in both languages. Stack reservation is
controlled separately from the messaging comparison.

| Full packet RAM accounting | C | Rust |
| --- | ---: | ---: |
| IRAM code and vectors | 40,448 B | 40,448 B |
| Static DRAM and noinit | 25,232 B | 25,416 B |
| Spawned thread stack reservations | 83,968 B | 83,968 B |
| Other idle spawned-thread resources | 4,640 B | 5,056 B |
| Baseline and command heap resources | 15,284 B | 16,396 B |
| Peak heap above the live idle-thread baseline | 36,220 B | 38,244 B |
| Accounted section RAM plus peak heap | 205,792 B | 209,528 B |

The last incremental heap row includes queues, application state, diagnostics,
and transient work or cleanup; it is not a pure queue allocation count.
Subtracting the live idle-thread baseline also excludes the shell command's
stack. Subtracting only spawned stacks would incorrectly charge command
resources to messaging.

At the synchronized ready point, the workload increment above idle threads
was 20,816 bytes in C and 22,936 bytes in Rust: 2,120 bytes more. The peak
increment differed by 2,024 bytes. Repeated commands retained no additional
heap after the first initialization in the accepted runs.

### Buffers, queue capacity, and threads are recurring costs

The target's nominal allocated message blocks are 264 bytes for an event,
32 bytes for a 16-byte reply, and 48 bytes for a 28-byte reply. Sixty queue
metadata objects account for a nominal 6,720 bytes before names, inodes,
descriptors, and poll state. Filling all packet-workload queue slots accounts
for 14,760 bytes of nominal message blocks.

NuttX may allocate a send buffer before waiting for space. In-flight calls
therefore consume memory beyond configured queue slots. The layout ledger
allows another nominal 5,064 bytes for active calls; this is not an allocation
trace or a guaranteed fragmentation bound. A 6,272-byte fixed kernel pool is
already in BSS and must not be counted again. Two latency-sample arrays add
5,760 bytes of diagnostic storage.

A product can use fewer dedicated threads, smaller measured stack
reservations, fewer queues, shallower capacities, and fewer diagnostic
samples. For example, 2 kB producer stacks and 3 kB worker stacks would save
23,552 bytes with the collector unchanged. That sizing is an unvalidated
example, not a safe configuration established by these tests. Changes must
be matched in C and Rust, then tested under worst-case workload. They reduce
the absolute product budget without explaining away a language delta.

## Speed

Timing starts after all twenty roles finish local setup and ends when the
collector validates the last reply. Queue/thread construction, report
printing, and final teardown are outside that interval. Worker cleanup can
still overlap traffic. The clock is the 240 MHz cycle counter with a wrap
guard, not the earlier coarse elapsed clock.

Separately flashed packet images had medians of 599 ms in C and 819 ms in
Rust, but that large gap was not stable. Alternating C and Rust in the same
image produced the following command-level paired median ratios across ten
commands, each containing four alternating pairs.

| Alternating workload | Rust time relative to C |
| --- | ---: |
| Transport only | 1.125 times |
| Packet processing | 1.055 times |
| Packet processing with direct sample filling | 1.068 times |

The packet path was about 5 to 7 percent slower in these better-controlled
runs; equal speed has not been established. Queue scheduling and placement
still contribute variation, and the remaining difference is not fully
attributed. The transport-only ratios varied substantially between commands.

Code analysis disproved an extra 248-byte Rust wire-copy hypothesis: those
copies were already optimized away. Rust did retain 192-byte temporary sample
arrays at packet construction sites. Filling the final arrays directly
removed the copies and reduced producer stack high-water use by 192 bytes,
without changing reservations or demonstrating a reliable end-to-end speed
win. GCC and LLVM also generated different sample-generation loops. Applying
speed optimization indiscriminately increased Rust image size without a
consistent transport speed improvement.

## Practical guidance

Use bounded queues and reserve container capacity. Reuse byte-level transport
helpers where many typed queues would duplicate code. Prefer a simple
single-inbox implementation when selection is not required. Keep rich
`println!` diagnostics optional, and verify their cost in the final image.

An embedded entry and narrow native thread layer are promising, but their
contracts must be explicit. The demo lacks complete cancellation, disconnect,
peer-failure, and partial-startup recovery. Finite success and repeat-heap
tests do not make it production-ready messaging infrastructure.

The next product assessment should size actual services and buffers against
the 2 MB flash and 250 kB RAM target, with stacks measured separately. Include
driver memory, worst-case queue occupancy, failure recovery, and timing tails.
The current evidence supports continuing with Rust; it does not justify
calling its overhead zero or treating this large stress fixture as a product
memory floor.
