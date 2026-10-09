# Event-based service comparison

This is a temporary local ESP32-S3 experiment, not a production dependency or
a proposal to add another RTOS to nxrs. It tests independent service event
loops on NuttX C, NuttX Rust, Zephyr C and Embassy Rust. The earlier
[producer/worker/reply benchmark](../zephyr-comparison/README.md) remains
historical evidence; its timings are not pooled with this different workload.

Start with the independent [RTOS analysis](../../docs/rtos-comparison.md) for
conclusions, diagrams and development tradeoffs. This package retains the
reproducible contract, tools and dated evidence behind that summary. The
[scheduling follow-up](SCHEDULING.md) tests natural async I/O and bounded CPU
work; the initial per-event yield below is a historical control, not a
requirement for every Embassy service.

The [arithmetic follow-up](ARITHMETIC.md) now evaluates a private pinned
LLVM/Rust rebuild with unchanged portable apps. Its 80-run matrix accepts and
receives all 312,000 messages, removing the tested CPU overload; C speed and
deadline parity are not fully achieved. The dated tables below and in
`SCHEDULING.md` retain their original unpatched-compiler measurements. The
discarded assembly diagnostic is kept separate, not used as compiler evidence.
Normal builds still use the installed SDK; the compiler patchset remains
inactive pending broader loop/interrupt and product qualification.

The [compiler scheduling control](COMPILER_PROBE.md) isolates a multiply
scheduling penalty and removes it with a sixth backend patch. Its GNU-assembled
C/LLVM and Rust/LLVM controls match GCC's six-cycle loop. A subsequent
[native-object diagnostic](COMPILER_PROBE.md#native-object-alignment-follow-up)
finds a separate final-alignment penalty. The
[rebuilt-driver confirmation](COMPILER_PROBE.md#rebuilt-rust-driver-confirmation)
removes that repeated penalty with normal Cargo/native objects; its separate
32-run uninstrumented C/Rust matrix retains remaining deadline differences.
The automatic fix is an inactive proposal pending wider qualification. The
[compiler setup package](../../platform/rust-llvm/BUILDING.md) provides the
same opt-in command for local Linux and CI, without replacing an SDK.

## The application model

Twenty services each own their state and publish three event classes to three
other services. Each destination therefore has multiple sending services and
one receiving owner. There are no replies, acknowledgements, collector task
or sequential queue timeouts. A coordinator checks accepted/received counts,
ordering, payloads and per-flow digests after the service loops stop.

Both layouts have the same 480 slots of 64 bytes: 30,720 bytes of payload
capacity. The three-queue layout has 60 queues, each with eight slots; the
mailbox layout has 20 queues, each with 24 slots. Separate class queues isolate
capacity between event types. A mailbox shares capacity and FIFO ordering.
This is a layout comparison, not a priority-policy comparison.

| Implementation | Service execution | One wait point |
| --- | --- | --- |
| NuttX C / Rust | 20 POSIX threads | `poll()` over the service's mqueues and publication timeout |
| Zephyr C | 20 native threads | `k_poll()` over the service's msgqs and publication timeout |
| Embassy Rust | 20 async tasks | One future polls all inboxes and registers one publication deadline |

Ready class queues are visited round-robin. A service publishes at most one
due batch and handles at most one received event per iteration. Embassy's
wait future yields once per iteration even when an inbox stays ready, so one
busy service cannot keep the cooperative executor indefinitely. The executor
uses hardware-timer wakeups and WAITI between ready work, not a spin-only loop.
Async tasks do not have separate preemptive stacks; that is a genuine execution
model difference, not twenty threads with artificially reduced stacks.

## What the measurements mean

The [contract](DESIGN.md) specifies routing, payloads, handler work, calendars,
capacities and deadline budgets. Every handler validates the same 44 payload
bytes and updates the same small state value. Host differential tests compare
the C and Rust implementation for every generated event in all profiles.

Normal traffic attempts 3,900 multicast deliveries in two seconds; burst
traffic attempts 6,240. Releases follow an absolute calendar, followed by a
500 ms drain interval. Slow implementations cannot silently reduce their
offered load. Sends are nonblocking and never retry or overwrite a full queue.
Loss-free qualification requires zero rejected events. Delivery correctness,
capacity and timeliness are reported separately; `ES_PASS` alone does not mean
that every deadline was met.

Two latency measurements answer different questions:

- **Post to handler:** send-call start through queueing and scheduling to the
  beginning of the destination handler. This is the queue-response metric.
- **Scheduled release to handler:** also includes the publishing service's
  timer and scheduling delay. Publication lateness is reported separately.

Per-service observations, worst-service latency and control deadline misses
are retained; an aggregate average cannot hide a slow service. Percentiles are
histogram upper bounds capped at the observed maximum. Summary tables use the
median of per-run p99 bounds, not a reconstructed pooled p99.

## Fairness and limits

The matched cases use the same board, one 240 MHz core, DIO 40 MHz flash,
16 KiB instruction / 32 KiB data caches, application `-O2`, and 10 ms timer
resolution. NuttX and Zephyr retain size-optimized kernels and checking-enabled
profiles. All timestamps use the same 16 MHz system counter, enabled through
CPU idle and expressed in common units. Staging, printing and reconciliation
are outside the traffic interval.

NuttX C and Rust share the queue/thread adapter and diagnostic coordinator;
the service schedule, event construction and handler are actually compiled
from C and Rust respectively. Rust uses native POSIX interfaces through a small
FFI boundary, not `std::thread`, `std::mpsc` or `println!`. Thus its size delta
answers whether this architecture can have a small Rust application layer. It
does not predict the cost of every future Rust library or standard-library API.

Zephyr uses its native APIs without a filesystem or POSIX compatibility layer.
Embassy uses bounded channels and no heap. NuttX retains its shell, VFS, libc
and board services. Whole-image differences include those platform features;
they are not an equal-feature comparison of kernel implementations alone.

RAM is separated into queue capacity/metadata, execution storage, service state
and test-only diagnostics. NuttX message storage is demand-allocated; its heap
high-water mark is an observation, not proof that all 480 slots can be filled
within the same RAM bound. Static buffers, heaps and stacks already in the
Zephyr/Embassy ELF must not be counted again. The common 20 × 4 KiB thread
reservation is not described as messaging overhead. CPU utilization, idle
power, ISR-to-service latency, long handlers and an application with real device
drivers are not qualified by this test.

## Local ESP32-S3 results, 2026-10-04

The [numeric evidence](results/esp32s3-2026-10-04.json) records eight frozen
images, two rotated case blocks and six invocations per image/profile:
96 invocations in total. All 486,720 attempted events were accepted and
received, with zero protocol errors. The original full flash was restored and
verified after the matrix. All measurements below are from the connected
board, not CI.

### Image size and RAM

These are whole-firmware figures, not isolated queue-library costs. The image
columns compare code + initialized data with flash binary size.[^flash-size]
Whole RAM includes resident IRAM/data and all reserved stacks/arenas, plus
NuttX's observed heap high-water mark. The RAM column is the largest observation
across both traffic profiles; Zephyr and Embassy's static reservations do not
vary between runs.

| Three queues per service | Code + initialized data (bytes) | Flash binary size (bytes) | Whole RAM bytes |
| --- | ---: | ---: | ---: |
| NuttX C | 173,488 | 213,804 | 226,744 |
| NuttX Rust | 174,192 | 213,820 | 226,576 |
| Zephyr C | 84,955 | 141,416 | 230,640 |
| Embassy Rust | 52,653 | 167,232 | 84,636 |

Rust adds **704 bytes of code + initialized data and 16 bytes to the flash
binary** over the matched NuttX C app. Both reserve exactly 103,832 resident
RAM bytes. The small observed heap difference is schedule-dependent, not a
general Rust RAM saving. The same 704/16-byte image deltas occur with one mailbox per service.
Rust does not need a large application-layer tax for this design, but using
additional formatting, allocation or messaging APIs can still link more code.
This is not a measurement of `std::mpsc` or crossbeam.

One mailbox per service keeps the same payload capacity but removes queue
objects and wait registrations. Whole RAM becomes 214,520 / 213,960 / 227,760 /
83,356 bytes for NuttX C / Rust / Zephyr / Embassy respectively. Code + initialized
data becomes 173,304 / 174,008 / 84,823 / 51,285 bytes. This fixture does not show a
consistent latency winner between the two layouts; choose separate queues
for capacity isolation, not an assumed speed improvement.

The apparent 227–231 kB threaded footprint needs a breakdown:

| Identified allocation | NuttX C/Rust | Zephyr C | Embassy Rust |
| --- | ---: | ---: | ---: |
| Payload capacity | 30,720 bytes, demand-allocated | 30,720 bytes, static | 30,720 bytes, static |
| Service execution stacks | 81,920 bytes | 81,920 bytes | 8,192-byte shared stack |
| Test-only diagnostics | 29,920 bytes | 29,920 bytes | 29,920 bytes |
| Service state and delivery checks | 3,040 bytes | 3,040 bytes | 3,040 bytes |

These rows explain major costs, but do not sum to the whole image: platform
code/data, main/interrupt stacks, queue objects, thread objects and task
futures also count. In the three-queue Embassy ELF, queue metadata occupies
1,920 bytes and the service/clock future pools occupy 2,240 / 88 bytes.
Async execution storage is not free; it is already included in the 84,636-byte
total. Zephyr's queue/wait metadata occupies 4,400 bytes. NuttX's 4,880-byte
adapter metadata does not include heap-allocated kernel queue/thread objects.

Of the common diagnostics, 26,400 bytes are latency histograms; the rest
reconciles sent flows and stores counters. Subtracting all 29,920 diagnostic
bytes illustrates about 197 kB for NuttX, 201 kB for Zephyr and 55 kB for
Embassy. Those are arithmetic illustrations, not rebuilt lean images or
worst-case bounds. NuttX's observed peak does not reserve every possible queued
message. A 250 kB target still needs room for real drivers, application state,
chip-reserved memory and safety margin. Thread stacks remain separate from
messaging cost; no stack-size tuning is used to improve this comparison.

### Response time and deadlines

For the 60-queue layout, median per-run p99 bounds are:

| Implementation | Normal post→handler | Burst post→handler | Normal release→handler | Burst release→handler |
| --- | ---: | ---: | ---: | ---: |
| NuttX C | 2.68 ms | 3.95 ms | 19.37 ms | 22.09 ms |
| NuttX Rust | 2.02 ms | 3.26 ms | 18.79 ms | 22.20 ms |
| Zephyr C | 1.54 ms | 2.05 ms | 20.16 ms | 20.86 ms |
| Embassy Rust | 2.05 ms | 4.09 ms | 12.29 ms | 12.29 ms |

Zephyr has the shortest queue-response p99 here. Embassy has the shortest
scheduled-release response. NuttX C/Rust are comparable; six short runs per
profile and coarse histogram bounds do not establish a universal language
speed advantage. All implementations attempt the same fixed calendar, so
total run length is not a throughput ranking.

Publication lateness explains much of the gap between the two metrics:
normal median publication p99 is 18.47 / 18.38 / 19.53 / 10.24 ms respectively.
The 10 ms wake resolution and service scheduling matter more than a small
handler or queue cost here. This identifies publication timing as a tuning
target, not proof of one specific kernel bottleneck. A finer-timer control
would be needed to isolate that effect.

There were **262 missed control deadlines** across all 96 invocations; no
data or status deadline was missed. For the 60-queue layout, runs meeting all
deadlines were NuttX C 6/6 normal and 3/6 burst, Rust 6/6 and 5/6, Zephyr 0/6
and 6/6, and Embassy 6/6 and 6/6. Both Embassy layouts met every tested budget.
Zero deadline misses in these short runs is not a hard real-time guarantee.
The 20 ms control budget is deliberately distinct from delivery correctness;
we do not relax it or discard misses to make a platform pass.

### What this supports, and what remains open

The revised fixture matches independent event-based services, rather than a
reply pipeline. It shows loss-free delivery and one wait-any point at the
tested normal/burst rates. The native POSIX Rust layer is close to C in size,
RAM and response time. Embassy provides substantially more memory headroom
for small, cooperative handlers; that saving mainly comes from execution and
platform choices, not from Rust versus C. Zephyr's native queue response is
good, but its full static fixture is not smaller in RAM than NuttX's observed
peak. Neither comparison makes their different platform feature sets equal.

The original matrix qualified only normal and burst traffic. An earlier NuttX
overload pilot failed to attempt its complete calendar and drain accepted
events; it is not a valid speed result and is not pooled with these final
images. At that stage, sustainable maximum load and all-slot-full RAM were
unqualified.
Bounded queues cannot guarantee no overflow for arbitrary arrival rates.
Production services need a specified burst/rate budget and an explicit
full-queue policy; this test verifies zero rejections only for its calendars.

The follow-up below completes the finer-timer, longer-handler, full-queue RAM
and GPIO driver controls identified after the original matrix. External
interrupt-to-service latency and sensor integration remain future tests,
along with sustainable maximum load, CPU utilization and idle power.

### Follow-up controls completed

The [timing, full-queue RAM and GPIO controls](CONTROLS.md) now cover those
questions with separate, dated evidence: 160 additional board invocations.
At 1 ms resolution, normal/burst release-to-handler p99 is about 1.5–2.2 ms
and every tested normal/burst run meets its deadlines. Long synchronous work
still causes interference and, in the Rust cases, overload. All 480 queue
slots were filled and drained successfully in both layouts; the 60-queue
whole-RAM peak is 257,184 / 257,576 / 230,936 / 84,864 bytes for NuttX C /
Rust / Zephyr / Embassy. The NuttX traffic peak above was not a full-capacity
RAM bound. GPIO2 actuation and pad readback passed; external interrupt-driven
sensor latency remains untested. These results do not replace or pool with
the original 10 ms matrix above.

### Async scheduling and thread-wrapper follow-up

The [scheduling follow-up](SCHEDULING.md) adds 168 matched local invocations:
natural awaits, a bounded cooperative budget, chunked CPU work and simulated
I/O waits. Natural I/O waits pass without explicit yields. Chunking reduces
long-handler peer queue delay to about 2 ms, but does not make the busy service
loss-free or prevent all late publications. That report also separates the
historical `std::thread` wrapper cost from common OS stacks and this native
thread experiment. Its measurements are not pooled with the earlier matrices.

## Reproducing locally

`build.py` creates one fresh image directory for a platform/layout using the
existing pinned toolchains and prepared NuttX tree. Build both layouts for
`nuttx-c`, `nuttx-rust`, `zephyr-c` and `embassy`; preserve the resulting eight
directories and their `build-provenance.json` files. It verifies artifact
hashes, frozen source inputs, and the identical NuttX kernel configuration.
Only app selection is changed in the already-resolved NuttX configuration.

Before flashing, make and protect a full 16 MiB backup of the actual connected
board. Keep that backup and raw boot/serial logs private. The matrix checks
every artifact hash and restores and verifies the full backup in `finally`,
including after a failed run. Example, with locally supplied paths:

```sh
python3 tests/event-services-comparison/run_matrix.py \
  --artifacts target/event-services-final --out target/event-services-measured \
  --backup /private/path/device-before.bin --port /dev/cu.YOUR_BOARD \
  --flasher /path/to/esptool.py --runs 3 --blocks 2 \
  --profile normal --profile burst
python3 tests/event-services-comparison/report.py \
  --matrix target/event-services-measured/matrix.json \
  --out tests/event-services-comparison/results/esp32s3.json
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
```

Case order rotates between blocks and profile order between repetitions.
Failures are retained, never silently retried or dropped from a successful
matrix. An overload run that cannot attempt its full calendar or drain its
accepted traffic is a failed qualification, not a valid speed result.

[^flash-size]: Code + initialized data counts firmware sections stored in
    flash. Flash binary size also includes boot components, image headers and
    offset/alignment padding. Debug symbols and uninitialized RAM sections
    such as `.bss` are excluded from both. Debug symbols enlarge the separate
    ELF artifact, not the gap between these columns.
