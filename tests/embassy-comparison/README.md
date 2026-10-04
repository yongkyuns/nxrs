# ESP32 S3 Embassy comparison

This adds an isolated Embassy experiment to the [NuttX and Zephyr comparison](../zephyr-comparison/README.md).
It tests whether Rust with async tasks can support the same service traffic
within a small memory budget. It does not add Embassy to nxrs production,
change upstream sources, start CI jobs, or propose an OS migration.

The [numeric evidence](results/esp32s3-2026-10-04.json) records 180 passing
traffic invocations and 12 baseline invocations, with artifact/source hashes,
configurations, individual results and case order. All three accepted matrices
restored and verified the original full flash. Private backups and raw logs
remain in ignored local storage; failed preliminary matrices are not pooled.

Embassy is much smaller in this workload, but the comparison is not Rust
versus C with identical operating-system services. This is bare-metal Rust
`no_std` with cooperative tasks, compared with preemptive NuttX/Zephyr
threads. The scheduling policy also matters: yielding only when queues block
gives good throughput but can leave individual messages waiting a long time.
An explicit one-message-per-turn control tests that tradeoff separately.

## What stays the same

All implementations use four producers, 15 workers and one collector, 60
bounded queues, and 5,760 event/reply pairs per invocation. The 45 input queues
hold one 248-byte event each; 15 output queues hold four replies each. Workers
wait on three inputs and the collector waits on 15 outputs, choosing ready
queues round-robin. There are multiple concurrent sending roles, with one
receiving owner per queue. Full queues apply backpressure; messages are not
dropped or overwritten.

Wire mode checks routing, per-producer ordering, sequence, endpoints and
checksums, with 16-byte replies. Packet mode adds the same 200-byte packet,
32 XYZ sample frames, persistent integer filter state, and an independent
collector calculation checking every reply, with 28-byte replies. Embassy
imports the existing Rust payload/filter modules rather than inventing a
cheaper algorithm. Every invocation must finish with digest `441445568`;
packet results are validated separately from that digest.

Timing starts after every role has initialized and reached its start gate.
It ends after the last reply is validated, before sorting samples, printing
or teardown. The CPU cycle counter is the primary clock. There are 720
latency samples, starting immediately before a send: they include queue
backpressure and scheduling, not just an uncontended queue operation.

NuttX's USB/NSH input is paced at 5 ms per byte in an experiment-only host
adapter. This is outside the application's cycle-counter timing; the
firmware and benchmark commands are unchanged. An earlier matrix lost a
`free` command response and was rejected rather than pooled into the results.

## Configuration and limits

| Setting | Embassy experiment |
| --- | --- |
| Hardware | Same Freenove ESP32-S3-WROOM; one core at 240 MHz |
| Flash and caches | DIO 40 MHz; I-cache 16 KiB, D-cache 32 KiB; 8-way, 32-byte lines |
| Executor | `embassy-executor` 0.10.0, official `platform-spin` backend |
| Messaging | `embassy-sync` 0.7.2 bounded channels with critical-section mutex |
| HAL | `esp-hal` 1.0.0; no Wi-Fi, Bluetooth, network or PSRAM |
| Rust | ESP Rust 1.90.0.0; all dependencies frozen in this local Cargo.lock |
| Optimization | Dependencies `opt-level=s`; app `2` or `s`; fat LTO, one codegen unit |
| Storage | Static channels and task pools; no application heap allocator |
| Stack | One 8,192-byte main/executor/interrupt stack, not 20 thread stacks |

These versions are deliberately pinned to the installed compiler, not
presented as the newest available releases. The experiment has its own
workspace and lockfile. The app-owned `stack.x` bounds the HAL's default
all-remaining-RAM stack region; it does not modify the HAL registry sources.
The build verifies the actual ELF stack section and guard symbol.

The official [channel API](https://docs.embassy.dev/embassy-sync/0.7.2/default/channel/struct.Channel.html)
provides bounded buffering and async backpressure. `messaging.rs` polls all
eligible channels with the receiving task's waker; it does not busy-scan them
while the task is suspended. The coordinator uses the spin executor, so idle
power is **not** qualified by this experiment. An interrupt-driven sleeping
executor is a separate integration task.

The 20 Embassy roles are tasks, not threads. A ready queue operation can
complete without suspending its caller. Long synchronous work can therefore
delay other tasks. The separate `cooperative-yield` build yields after each
event/reply, including collector processing except the final reply. This
changes scheduling overhead, not payload work or validation. Neither mode
proves a hard real-time bound, sender fairness for every future workload,
multicore safety, or ISR-to-task behavior.

## RAM

The complete packet build reserves 49,172 bytes of resident internal RAM;
the yield control reserves 49,180. This includes IRAM, static data, task
futures and the entire 8 KiB shared stack. No heap or PSRAM is added later.
The task pools include values kept across awaits, validation/filter state,
latency sample arrays and executor task headers: async storage is not free.

| Packet implementation | Whole accounted RAM | Increment over console baseline, excluding spawned stacks |
| --- | ---: | ---: |
| NuttX C, matched | 205,776 bytes | 40,736 bytes |
| NuttX Rust `std` | 210,064 bytes | 45,024 bytes |
| Zephyr C | 186,840 bytes | 28,628 bytes |
| Embassy, queue waits only | 49,172 bytes | 33,328 bytes |
| Embassy, explicit yield | 49,180 bytes | 33,336 bytes |

NuttX whole RAM adds the observed heap high-water mark to static resident
sections. Zephyr and Embassy already reserve their storage statically, so
runtime use is not added a second time. The last column is a fixture delta,
not pure queue cost or a forecast of all future service resources.

The primary task pools use 18,096 bytes. The channel storage is exactly
14,760 bytes: 12,840 for slots and 1,920 for
metadata, or 32 metadata bytes per queue. Changing queue capacity or event
size grows slot storage. Adding task types and values held across awaits
can grow task storage. Most executor/HAL code is shared; this is not a
guarantee of a fixed total cost as the application grows.

After subtracting Embassy's 15,844-byte console baseline, the packet fixture
adds 33,328 bytes. Zephyr adds 28,628 after subtracting its baseline and the
83,968-byte spawned stacks. That does **not** mean Embassy's whole messaging
system is lighter: its queue metadata is smaller, but its futures hold more
explicit state. The residual comparison also mixes application/test state;
RTOS locals inside reserved stacks and async locals inside futures are not
the same accounting bucket. Whole-RAM savings mainly come from fewer stacks
and the smaller platform baseline, not zero-cost queues or application state.

The report also subtracts each platform's minimal console baseline. NuttX
and Zephyr's 83,968-byte spawned-stack reservation is removed separately,
so those stacks are not mislabeled as messaging overhead. Embassy's shared
stack is already present in its baseline. Static resident RAM excludes
`.rwdata_dummy`, an address alias of IRAM, and unused linker address gaps.
Allocated stack reservations are counted even when a run does not use every
byte. Successful repeated runs and a stack guard are not a complete
worst-case stack analysis for future error paths or interrupt nesting.

As in the existing comparison, cache SRAM and ROM/system-reserved regions
are not counted as application allocations. Include those chip-specific
reservations when budgeting a real 250 kB MCU; this ledger is not a claim
that every other byte of the ESP32-S3 is freely available to applications.

## Image size

The packet `opt-level=2` build has 45,505 load-bearing ELF bytes; the yield
control has 45,573. The app-size-optimized packet build has 41,445. These
counts exclude debug symbols and NOBITS sections, which are not programmed
code/data. No Rust `std`, allocator, POSIX/VFS layer or thread runtime is
linked into Embassy. It is a different feature set from NuttX, not evidence
that the existing Rust `std` tax has vanished in a like-for-like build.

Actual merged image lengths are also recorded. The primary Embassy image
is 160,448 bytes, including bootloader, partition layout and alignment;
it is **not** a 45,505-byte flash file. Boot/layout padding and linked
application bytes are different measures, and neither is the debug ELF's
file length. Future code growth can cross an image-alignment boundary.

For the packet controls, Zephyr's merged image is 142,112 bytes, versus
160,448 for primary Embassy. Embassy therefore has **fewer linked bytes but
a larger merged file than Zephyr** in this setup. The NuttX C/Rust unpadded
merged extents are 214,232 / 215,120 bytes; their local flashing artifacts
are padded to 16 MiB, which is not treated as application code size.

## Speed

Fresh packet measurements on 2026-10-04 use two rotated blocks of ten
invocations per case. Every one of these 120 invocations passed the complete
protocol and filter checks. NuttX Rust retains its existing auxiliary wake
probe outside the timed region; the matched C control has no such probe.

| Packet implementation | Median traffic time | Median inbound p99 | Largest sampled inbound wait |
| --- | ---: | ---: | ---: |
| NuttX C, matched `-O2` | 640.312 ms | 10,318.5 us | 109,752 us |
| NuttX Rust `std`, app `2` | 653.494 ms | 8,948.5 us | 150,366 us |
| Zephyr C, app `-O2` | 393.157 ms | 3,114 us | 5,411 us |
| Embassy Rust, app `2`, queue waits only | 282.009 ms | 3,875 us | 212,484 us |
| Embassy Rust, app `2`, explicit yield | 298.886 ms | 3,426 us | 3,750 us |
| Embassy Rust, app `s`, queue waits only | 327.023 ms | 4,584 us | 246,607 us |

Each invocation reports nearest-rank percentiles from its 720 samples.
The table's p99 is the median of those per-invocation p99 values, not a
percentile recomputed from all samples pooled together. The maximum column
is the largest **sampled** wait across invocations, not a proven deadline.

The primary Embassy build wins throughput but does not win message latency.
The yield control is about 6% slower overall, adds 68 linked bytes and eight
RAM bytes, and removes the large sampled inbound delay in these runs. It
still takes about 24% less total time than Zephyr. Size optimization saves
4,060 linked bytes but increases packet time by about 16% versus the primary
Embassy build. Optimizing for image size is not automatically best for speed.

The source explains the relevant scheduling distinction: `.await` need not
suspend when an operation is ready, and suspended senders compete for finite
queue capacity. The yield control demonstrates that the long sampled wait
is avoidable here; it does not isolate every contribution from waker handling,
task ordering or batching. No profiler-based percentage attribution is
claimed. Removing thread context switches/POSIX paths, using a different
executor and enabling whole-program optimization are plausible contributors
to throughput; this experiment does not rank their individual effects.

The wire-only controls also have 20 invocations each in two rotated blocks:
NuttX C `-O2` takes 429.744 ms, Zephyr C `-O2` 191.809 ms, and Embassy app `2`
57.460 ms. Embassy's largest sampled inbound wait is still 43,690 us, versus
Zephyr's 2,793 us. The no-yield wire result therefore reinforces the same
warning: fast aggregate messaging is not a guarantee of low tail latency.

For a 250 kB-class application budget, the async design is promising: it
leaves substantially more accounted RAM for business logic. It does not
provide Rust `std`, POSIX services, preemptive blocking threads or a measured
Wi-Fi/Bluetooth/driver stack. A C state-machine design could make similar
stack tradeoffs but is not implemented here. The next qualification should
add real asynchronous device I/O, deadlines, shutdown/cancellation, sustained
contention and idle-power measurements before selecting a production model.

## Reproducing locally

Use the pinned ESP Rust compiler as the `esp` toolchain, put the matching
Xtensa GCC linker on PATH, and provide espflash 4.6.0 and GNU readelf/nm.
The standalone experiment uses Cargo `build-std=core`; ordinary host Rust
cannot compile this Xtensa target. The existing NuttX/Zephyr builders produce
the control artifacts; this directory does not install either OS.

From the repository root, using absolute tool paths:

```sh
python3 -m unittest discover -s tests/embassy-comparison -p 'test_*.py'
(cd tests/embassy-comparison && cargo +1.90.0 test --locked \
  --target x86_64-apple-darwin --features packet,cooperative-yield)
python3 tests/embassy-comparison/build.py --out target/embassy-packet-2-v2 \
  --mode packet --profile release --cargo-target-dir target/embassy-cargo \
  --espflash /absolute/path/espflash --readelf /absolute/path/xtensa-esp32s3-elf-readelf
```

Build wire with `--mode wire --profile release`, packet size optimization
with `--mode packet --profile size`, and the yield control with
`--mode packet --profile release --cooperative-yield`. Build console
baselines with `--mode baseline` at both profiles. Output directories must
be fresh; the artifact names are listed in `run_matrix.py`.

Before flashing, make a **private full 16 MiB backup** with esptool
`read_flash 0x0 0x1000000`, validate its length/hash, and `chmod 600` it.
Use `run_matrix.py`, not the low-level measurement script, for device runs:

```sh
python3 tests/embassy-comparison/run_matrix.py --artifacts target \
  --out target/embassy-matrix --backup /private/path/device-before.bin \
  --port /dev/cu.usbmodemYOURPORT --flasher /absolute/path/esptool.py \
  --case c-packet-matched-2 --case zephyr-packet-2 \
  --case embassy-packet-2 --case embassy-packet-yield-2 --runs 10 --blocks 2
python3 tests/embassy-comparison/report.py --artifacts target \
  --matrix target/embassy-matrix --readelf /absolute/path/xtensa-esp32s3-elf-readelf \
  --nm /absolute/path/xtensa-esp32s3-elf-nm \
  --nuttx-compiler /absolute/path/xtensa-esp32s3-elf-gcc --out target/comparison.json
```

The matrix rotates case order, verifies frozen image hashes before flashing,
and restores/verifies the original full flash in `finally`, even if a run
fails. The report rejects failed/incomplete matrices and checks raw serial
markers against saved numeric results. Host tests cover payload validation,
ordering, distinct sender wakeups, backpressure, multi-queue waiting, image
accounting and evidence rejection. These are useful regression checks, not
a proof that the complete messaging system is ready for production.
