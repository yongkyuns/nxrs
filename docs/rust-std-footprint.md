# Rust std footprint on NuttX

Rust std is a viable candidate for nxrs services, provided runtime APIs,
logging and buffers are chosen deliberately. Most support code is shared,
but adding new API families, generic types or concurrent owners still adds
code or storage. The measurements do not establish zero overhead or qualify
a complete 250 kB product.

This analysis covers the historical ESP32-S3 footprint fixture through
October 3, 2026. The [demo and before/after recipes](../tests/service-footprint/README.md)
preserve the choices to try. [Recorded evidence](../tests/service-footprint/results/esp32s3-2026-10-03.json)
contains accounting and hashes; cleaned sources are not byte-identical to
those firmware images. The later [RTOS comparison](rtos-comparison.md) uses
different workloads and compiler cohorts, not additional samples of this test.

## Matched work, not just matching names

The first skeleton was unfair because C did less. The replacement LED service
matched HAL control/readback, a worker, commands, replies and shutdown.
The larger packet fixture matches four producers, fifteen workers, one
collector, sixty POSIX queues and twenty spawned threads. Both languages
validate 5,760 event/reply pairs.

Each 248-byte event carries 32 XYZ sample frames. Workers validate packets
and update three fixed-point filter states per stream; the collector checks
their replies independently. This is not the earlier artificial checksum.
C/Rust share the kernel configuration, poll selection, stack reservations,
clock and release gate. Host transports are different and cannot establish
device performance.

## Image size

### Which choices mattered?

The early roughly 230 kB increase was an **ELF file-size** difference, not
230 kB of deployed code. Debug symbols and metadata need not enter the flash
binary. Inspect final code + initialized data after optimization/LTO;
do not strip the relocatable Rust input before NuttX's final link.

These are separate controlled builds, not an additive optimization ladder:

| Matched application and Rust choice | Extra code + initialized data over C |
| --- | ---: |
| Concurrent LED/payload, custom one-slot MPSC | 19,750 B |
| Same application, std MPSC | 40,130 B |
| Same application, Crossbeam | 42,062 B |
| Silent LED, ordinary Rust entry and std worker | 13,042 B |
| Same silent LED, embedded entry and native pthread adapter | 1,956 B |

Crossbeam's multi-queue selection and channel semantics provide more than
a narrow one-slot mailbox. Choose a transport for its contract, then measure
the linked cost; these figures are not prices per queue.

Formatting means converting values into text. `println!` also retains stdout
locking, buffering and error paths. Removing output saved 3,788 B in the
controlled Rust LED build versus 180 B in C, whose shell already retained
output support. Even a constant print can bring in that machinery.

Ordinary Rust entry retained process startup/cleanup for descriptors, signals,
arguments and stdout. An embedded entry removed 3,284 B in that silent build,
but omits services some applications need. The native pthread adapter similarly
omits std naming, parking, thread-local and join/result/panic bookkeeping;
it is not a drop-in replacement for every `std::thread` use.

An independent twenty-idle-thread control, with equal kernel, entry and stacks,
found **10,808 B additional code + initialized data and 4,792 B peak heap**
for std threads. Common worker stacks are excluded. That is a measured
twenty-thread point, not a universal per-thread charge or messaging speed tax.
The [thread evidence](../tests/event-services-comparison/results/thread-wrapper-costs-2026-10-03.json)
records the narrower adapter's contract.

### Final packet fixture

| Diagnostic packet application | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Code + initialized data | 135,400 B | 155,418 B | +20,018 B |
| Flash binary size[^flash-size] | 214,440 B | 215,116 B | +676 B |

The code/data delta comprises 19,262 B instructions, 676 B read-only data and
80 B initialized RAM data. Another 104 B of BSS affects RAM only. This fixture
retains formatted reports; its result is not the silent LED's runtime floor.

Binary offset/alignment padding absorbs much of the extra code in this pair.
Those bytes still exist and later growth can cross the boundary. Debug symbols
are in neither column.

### One-time versus recurring growth

Reused runtime, output and transport routines normally link once. New
handlers, strings, error paths and generic message/closure/container types can
retain additional specializations; inlining and LTO affect sharing.

Adding matched packet computation increased C by 972 B and Rust by 1,104 B:
the gap grew by **132 B**, not another 20 kB. In a separate silent growth
study, the Rust delta rose from 1,484 B for one service to 2,428 B for three.
Neither is a universal growth rate.

First-use container probes added 316 B for the tested `Vec`, 4,684 B for a
default `HashMap`, 1,556 B for decimal formatting and 224 B for parsing.
These depend on types and operations, not instance count. Reserve capacities
and check the final application's retained symbols rather than banning std
containers.

## RAM

The packet stress fixture's **205,792 B C / 209,528 B Rust** totals include
kernel/static memory, heap, diagnostics and 83,968 B of common spawned-thread
stack reservations. They are not messaging-only costs or minimum nxrs budgets.

| Increment above live idle threads | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Workload ready-point heap | 20,816 B | 22,936 B | +2,120 B |
| Peak workload heap | 36,220 B | 38,244 B | +2,024 B |

This increment still includes queues, application state, diagnostics and
transient activity. The total Rust/C difference is larger because their idle
and resident resources also differ. Accepted repeated runs showed no further
retained heap after initialization.

Queue payloads/metadata, active send buffers and owner-specific buffers are
recurring costs. NuttX can allocate a message before waiting for a slot, so
configured queue capacity alone does not bound concurrent sender storage.
Existing static pools must not be counted again as heap.

The newer lean LED demo has a different event size, queue layout and startup;
its much smaller language RAM delta is documented in the RTOS guide.
Do not substitute it for this stress workload. Common stack sizing is a
separate product decision, not an explanation for Rust messaging overhead.

## Speed

The timed interval starts after setup and ends at the last validated reply;
construction, printing and final teardown are excluded. Separately flashed
C/Rust images gave a large but unstable difference. Alternating languages
inside the same image gave paired median Rust/C ratios of **1.125 transport,
1.055 packet processing and 1.068 direct-fill packet processing**.

Thus the packet path was about 5–7% slower in those historical controls,
not proven equal. Scheduling and placement still affect the result; it is
not an isolated queue-operation measurement or a prediction for newer images.

The suspected extra 248-byte Rust wire copy was already optimized away.
Directly filling packet samples removed a real 192-byte temporary and reduced
producer stack high-water, not its reservation. It did not demonstrate a
reliable end-to-end speed win.

## What to adapt

Use bounded capacities, reuse buffers and shared byte-level transport helpers,
and keep rich diagnostic output optional. Use std lifecycle APIs when their
behavior is needed; use the narrow native layer only within its explicit
contract. Measure code/data, actual binary, full-capacity RAM and timing tails
separately.

The historical demo does not qualify cancellation, disconnect, peer failure
or partial-startup recovery. The newer [service qualification](../tests/service-qualification/README.md)
adds bounded lifecycle evidence, not universal production guarantees.
Driver memory, worst-case concurrency and business logic still need a product
budget.

[^flash-size]: The flash binary includes boot components, headers and
    offset/alignment padding. Debug symbols enlarge the separate ELF artifact,
    not either column; uninitialized RAM such as `.bss` is also excluded.
