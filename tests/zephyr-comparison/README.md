# Temporary Zephyr comparison

This is an experiment branch, not a proposal to add Zephyr to nxrs. It adds
no production dependency, submodule, CI job, or upstream source modification.
Everything needed only for the experiment lives here or in ignored `target/`.
The comparison PR should remain separate from production changes.
The [measured evidence](results/esp32s3-2026-10-03.json) contains every run's
numeric results, artifact/source hashes, configuration identities and block
order. Private flash backups and raw serial logs stay local. Across five
matrices, all 320 traffic invocations and six baseline invocations passed;
each matrix restored and verified the original full flash.

## What is compared

The existing [C/Rust fixture](../service-footprint/README.md) runs on NuttX.
The Zephyr version uses native `k_msgq`, `k_poll`, and `k_thread`, not Zephyr's
POSIX compatibility layer. `generate_control.py` reuses the actual C workload;
it changes OS calls, queue naming, and fixed-resource allocation, not the
producer, worker, collector, packet processing, or validation algorithms.
Source-equivalence tests protect that boundary.

All three implementations exchange 5,760 event/reply pairs through 60 queues
and 20 spawned threads: four producers, 15 workers, and one collector. Each
worker waits for three input queues; the collector waits for 15 reply queues.
Input queues hold one 248-byte event; reply queues hold four replies. Sending
blocks when full. Each queue has one receiving owner, with round-robin
selection among ready inputs. This is a multiple-producer workload.

Two modes separate messaging from computation:

- **Wire:** sequence, routing, ordering, count, and checksum validation;
  16-byte replies.
- **Packet:** the same checks, plus a 200-byte encoded packet containing 32
  XYZ sample frames, persistent integer filtering per lane/producer, and an
  independent collector calculation that checks every result; 28-byte replies.

Both modes require exactly 5,760 replies and digest `441445568`. The digest
checks the routing token, not every payload byte; packet results are checked
separately. Timings start when all 20 roles are ready and end at the last
validated reply. They exclude construction, teardown, console output, and
NuttX's separate 64-trial wake test. Latency starts before a producer's send,
so it includes backpressure; it is not an uncontended kernel-call benchmark.

## Fairness and configuration

The board is the attached Freenove ESP32-S3-WROOM, with 16 MiB flash and 8 MiB
PSRAM. Zephyr's `esp32s3_devkitm/esp32s3/procpu` CPU target is overridden for
the actual flash size and USB Serial/JTAG console. Neither benchmark needs
LED, camera, Wi-Fi, Bluetooth, or networking.

| Setting | Primary NuttX C/Rust | Primary Zephyr C |
| --- | --- | --- |
| CPU / cores | 240 MHz / one | 240 MHz / one |
| Flash / caches | DIO 40 MHz; I-cache 16 KiB, D-cache 32 KiB; 8-way, 32-byte lines | Same |
| Tick / equal-priority scheduling | 100 Hz / 10 ms round-robin | 100 Hz / 10 ms time slicing |
| Spawned stacks | 19 × 4,096 + collector 6,144 = 83,968 bytes | Same |
| Command/main / interrupt stack | 8,192 / 4,096 bytes | Same |
| Kernel / app optimization | Kernel `-Os`; app `-Os` or `-O2`; Rust std size-optimized | Kernel `-Os`; app `-Os` or `-O2` |
| Checks | Assertions and stack coloration | Assertions, stack initialization, sentinel and monitoring |
| Messaging | POSIX mqueue + poll | Native message queues + poll |
| Application resource allocation | Heap-allocated queues, threads, scenario | Fixed static queues, threads, scenario |
| Other OS services | NSH, VFS, POSIX libc, USERLED; PSRAM initialized | Small command console, minimal libc; no PSRAM |

Equal scheduler settings do not make the scheduler implementations identical.
The NuttX profile selects PSRAM common-heap support, but `CONFIG_MM_REGIONS=1`
excludes the extra region: these measurements use internal SRAM, not an 8 MiB
escape hatch. Zephyr also uses internal SRAM. Cache SRAM, ROM/system-reserved
memory, stack guard implementation differences and inter-section alignment
are not converted into a claim about a chip's complete usable RAM.

The primary comparison gives Zephyr its normal lightweight native APIs. It
does not add a filesystem or POSIX emulation merely to make it resemble
NuttX. Conversely, its whole-image saving cannot be described as an
equal-feature replacement for NuttX's shell, libc, drivers and VFS. Minimal
console baselines for each OS separate fixed platform cost from the cost of
linking and running this workload; the baseline difference is still a
configuration difference, not a universal property of either RTOS.

Additional Zephyr packet controls use NuttX's GCC 14.2, the board's native
80 MHz flash setting, and an 80 MHz build without assertions, sentinel and
thread monitoring. These are reported separately, not substituted silently
for the matched 40 MHz/checking-enabled case. The lean control still checks
the complete application protocol.

## Results

Fresh measurements on 2026-10-03: the matched C/Zephyr packet pair runs in
two rotated blocks of ten invocations; the original C/Rust packet and GCC
control have three blocks of ten. The table pools all invocations of each
unchanged image: matched NuttX C has 20 runs, and primary Zephyr has 50
(30 from the original packet matrix plus 20 from the matched matrix).
Using only the matched matrix gives 20 runs for each OS and the same
Zephyr median, 393.157 ms. The evidence records the pooled counts,
per-matrix summaries and a separate matched-pair summary.
Every invocation passed routing, ordering, packet
and filter-result checks. These are local hardware results, not CI results
or the earlier study's historical measurements. Zephyr is C here; this does
not test a Rust `std` port to Zephyr.

| Packet workload, app `-O2` | Median traffic time | Load-bearing image bytes | Whole RAM bytes |
| --- | ---: | ---: | ---: |
| NuttX C, matched without auxiliary wake probe | 620.623 ms | 174,616 | 206,376 |
| Existing NuttX C, with auxiliary probe | 616.443 ms | 175,752 | 206,616 |
| Existing NuttX Rust, with auxiliary probe | 663.786 ms | 195,766 | 210,208 |
| Zephyr C, SDK GCC 12.2 | 393.157 ms | 86,639 | 186,840 |
| Zephyr C, matching GCC 14.2 control | 392.243 ms | 85,379 | 186,000 |

The extra NuttX probe runs **after** the timed workload, but its code still
counts toward image size. The matched C control omits just that call, using
the same generated-source boundary checks. It saves 1,136 load-bearing bytes;
it does not account for the main result. Existing C/Rust rows retain their
identical auxiliary probes so their language comparison remains matched.

Zephyr's primary packet case uses about 36.7% less elapsed time, 50.4% fewer
load-bearing image bytes and 9.5% less whole RAM than matched NuttX C. Changing its
compiler does not remove the speed advantage. This is a substantial native
messaging/platform advantage in this fixture; it is not evidence that C
always beats Rust, or that every Zephyr application beats every NuttX one.

### RAM

Whole RAM means allocated IRAM code/data, static DRAM/RTC data, BSS, noinit
and reserved stacks, plus NuttX's measured heap high-water mark. Zephyr's
static sections already contain its buffers/stacks and complete 4,096-byte
kernel heap arena: adding the runtime heap use again would double-count it.
That arena used 24 bytes after boot, with a 40-byte high-water mark. The
entire arena is counted, not just those 40 bytes. Matched NuttX C's heap peak
was 140,640 bytes; existing C/Rust peaks were 140,880 / 144,288.
Static resident sections add 65,736 / 65,920 for C / Rust.
Peak heap is an observed successful-run peak, not a proof of a worst-case
bound for every future schedule or error path.

The unchanged 83,968-byte spawned-stack reservation is separate from queue
cost. In Zephyr, the minimal console baseline reserves 74,244 bytes. The
packet fixture adds 112,596, of which 83,968 is those same stacks. The
remaining 28,628 bytes break down exactly as follows:

| Additional Zephyr packet RAM | Bytes |
| --- | ---: |
| 45 input slots + 60 reply slots | 12,840 |
| 60 native queue objects + open flags | 3,180 |
| 20 thread objects + entry records | 2,640 |
| Scenario state, including 5,760 bytes of latency samples | 6,344 |
| Gates, counters, initialized data and alignment remainder | 92 |
| Additional linked kernel code in IRAM | 3,532 |
| Total excluding spawned stacks | 28,628 |

NuttX's minimal-console whole RAM is 81,072 bytes (65,600 resident plus
15,472 peak heap). Matched C adds 125,304, including the same 83,968 bytes
of spawned stacks. Its remaining fixture cost is 41,336 versus Zephyr's
28,628: a 12,708-byte difference after removing both OS baseline and common
spawned stacks. That remainder includes messaging, thread metadata and
benchmark state; it is not all queue payload storage.

Filter state and per-call scratch arrays live inside the already-counted
thread stacks; they are not added a second time. The fixed queue buffers and
objects are lightweight, but not free. Static Zephyr reservations also remain
occupied when the demo is idle; NuttX releases its dynamically created
resources after a command. Whole-RAM comparison is of the running workload,
not a claim that the idle allocation models are identical.

Even 186,840 bytes is roughly three quarters of a 250 kB target's RAM budget
before adding useful product logic. This intentionally large fixture is not
an acceptable small-device product preset. Keep budgeting queues, payload
storage, diagnostics and service counts explicitly; changing RTOS does not
make a 20-thread/60-queue design free.

### Image size

The table counts **all allocated load-bearing ELF sections**, including code
copied into IRAM, initialized RAM data and metadata. It excludes debug
information, uninitialized storage and ESP memory-mapping padding. A dummy
DRAM section that aliases IRAM is excluded from RAM too. This definition
differs from the earlier report's flash-only subtotal; do not compare its
135 kB figure directly with Zephyr's 86 kB all-load-bearing figure.

Matched NuttX C's executable image is 214,232 bytes; existing C/Rust images
are 214,440 / 215,120, and Zephyr's packet binary is 142,112. These include headers and
flash alignment padding. A 16 MiB merged NuttX programming file is not a
16 MiB executable. Report both real binary length and load-bearing bytes;
padding can hide growth until an alignment boundary is crossed.

The minimal NuttX / Zephyr baselines contain 161,060 / 74,295 load-bearing
bytes. Adding the matched packet workload costs 13,556 / 12,344 bytes, respectively.
Thus 86,765 of the 87,977-byte matched-C-to-Zephyr image difference is already
present in the baselines. Most of the whole-image win comes from the leaner
fixed platform/configuration, not a dramatically smaller packet algorithm.
The baseline-subtracted increment includes newly needed kernel routines,
not just application instructions. See the [Rust analysis](../../docs/rust-std-footprint.md)
for the remaining C/Rust library and application-code distinction.

### Speed and why the native queue path helps

The wire controls each have 20 invocations, in two rotated blocks. They keep
the same queues, backpressure and validation without packet computation:

| Wire workload | Median traffic time | Median of each run's inbound p99 |
| --- | ---: | ---: |
| NuttX C `-Os` | 382.505 ms | 5,722.5 us |
| NuttX C `-O2` | 428.146 ms | 6,755.5 us |
| NuttX Rust app `-O2` | 416.397 ms | 5,029 us |
| Zephyr C `-Os` | 191.664 ms | 2,089 us |
| Zephyr C app `-O2` | 191.809 ms | 1,415 us |

The original NuttX wire controls also retain their out-of-band wake probe;
their timings exclude it. Zephyr's messaging-only workload takes about half
as long, so its advantage is not just the packet arithmetic. NuttX has
noticeable run-to-run variation; `-O2` is not universally faster on this
fixture. The evidence keeps every cycle count and block order, not just the
best run. The matched packet inbound p99 medians are 9,059 us for C and
3,114 us for primary Zephyr. These are medians of 720-sample **per-run** p99
values, not percentiles pooled across all invocations.

| Zephyr packet sensitivity, 20 runs each | Median traffic time | Load-bearing bytes | Whole RAM bytes |
| --- | ---: | ---: | ---: |
| App `-Os`, 40 MHz, checks enabled | 398.528 ms | 85,847 | 186,840 |
| App `-O2`, 80 MHz, checks enabled | 393.157 ms | 86,639 | 186,840 |
| App `-O2`, 80 MHz, lean checks | 327.881 ms | 68,583 | 174,620 |

80 MHz flash does not materially change this hot workload. Removing kernel
assertions, stack sentinel and thread monitoring does improve time and size;
that is a different checking policy, not the primary apples-to-apples result.
The 12,220-byte RAM saving is 9,376 bytes less IRAM code, 2,184 less initialized
DRAM, 656 less BSS and four bytes less heap metadata—not smaller user stacks.
These controls change settings in combination; they do not attribute a
specific percentage to any one check.

Both applications do the same work, but the OS paths differ. In the pinned
[NuttX send implementation](https://github.com/apache/nuttx/blob/2f3eb6d6774ab63b75788c27bde7644da48121b2/sched/mqueue/mq_send.c),
a send obtains a message from a shared pool or heap, copies the payload, and
then waits if the queue is full. Receive removes and releases that message.
POSIX descriptors also require file lookup/reference handling; multi-queue
poll uses filesystem-facing readiness machinery.

The pinned [Zephyr queue implementation](https://github.com/zephyrproject-rtos/zephyr/blob/75f67d766726351b30199f9a2bf55803d717a3be/kernel/msg_q.c)
uses the pre-reserved ring directly. When a producer blocks on a full queue,
it retains a pointer to the producer's still-live message rather than
allocating another heap message. This saves message bookkeeping and
allocator work. The fixture's receivers use `k_poll` followed by nonblocking
`k_msgq_get`, so Zephyr's separate direct-to-blocked-receiver handoff is
**not** claimed as the measured benefit here.

When receive frees a slot, Zephyr can also put a blocked sender's message
into that slot before waking the sender. NuttX instead wakes a sender to
continue its send path. This is another plausible source of lower overhead
under backpressure, not a measured count of saved context switches. Both
buffered paths still copy payloads into and out of queue storage; this is
not a zero-copy benchmark.

Code placement differs too, despite matched flash and cache settings.
In these actual ELFs, Zephyr's message-queue put/get and poll entry points
are in internal instruction RAM (`0x40376e80`, `0x40376e94`, `0x403792d8`).
NuttX's mq_send/mq_receive/poll entry points are in flash-mapped code
(`0x42027ec4`, `0x42028228`, `0x4201e838`). Internal RAM avoids instruction
cache misses on those routines. Placement is a plausible contributor, but
the experiment did not count cache misses or relocate either kernel to
isolate its contribution. The RAM ledger already counts that IRAM code.

There is also a concrete RAM-layout difference: the target-compiled NuttX
ledger gives a nominal 264-byte allocated event block for 248 payload bytes,
and a 48-byte block for a 28-byte reply. Its queue metadata request rounds
to 112 bytes per queue, before inode/name/descriptor/poll resources. Zephyr
uses exactly 248 / 28 bytes per slot and a 52-byte queue object. NuttX's
6,272-byte POSIX/SysV fixed message pools are already in its static RAM,
not another heap charge. Nominal layout totals are **not** allocation traces;
fixed-pool reuse, fragmentation and blocked calls affect the observed peak.

Together, the likely explanation is less queue/descriptor bookkeeping and
a different wait/wake path, with kernel code placement potentially helping.
This is not a Rust compiler problem: the NuttX C control is slower too.
Matched compiler and clock controls do not remove the difference.
These source and ELF facts explain plausible advantages. The elapsed-time difference
is measured; a precise percentage assigned to allocator versus poll versus
scheduler or cache work would require separate profiling. Do not turn the end-to-end
result into a claim that an individual API call is 36% faster.

The practical implication for nxrs is to investigate a bounded, typed
ring-buffer/multi-wait backend that avoids filesystem descriptors and
per-send allocation. That could preserve the Rust application layer while
recovering native messaging advantages. This experiment does not implement
that backend or decide an OS migration.

## Reproduce locally

Requires CMake, Ninja, dtc, Python 3.10+, Zephyr SDK 0.17.0 with its ESP32-S3
Xtensa toolchain, and the existing pinned NuttX/ESP Rust environment. Use
external checkouts or ignored `target/`; do not add Zephyr as a submodule.
Zephyr v4.3.1 is pinned to `75f67d766726351b30199f9a2bf55803d717a3be`.
Its two required west modules are pinned to:

- `hal_espressif`: `af6cfa2e3e7098b596062ab516b80a48a7ba7332`.
- `hal_xtensa`: `3cc9e3a9360be5c96c956dce84064b85439b6769`.

Initialize an isolated west workspace at that release and update just those
two modules. Install `scripts/requirements-base.txt` and `esptool==5.3.0`
in a local virtual environment for Zephyr builds. The build helper verifies
the full revisions, rejects modified tracked upstream inputs, selects the
pinned CMake package explicitly, preserves the virtual-environment path,
and validates the resolved hardware settings. No patch to Zephyr is needed.
For ESP flashing, the measurement helpers use an existing esptool v4 CLI
with underscore command spelling; pass its executable explicitly.

With `ZEPHYR_SRC`, `ESPRESSIF_HAL`, `XTENSA_HAL`, `ZEPHYR_SDK` and
`ZEPHYR_PYTHON` pointing to those external installations, from the repo root:

```sh
python3 -m unittest discover -s tests/zephyr-comparison -p 'test_*.py' -v
python3 tests/service-footprint/check_native.py

python3 tests/zephyr-comparison/build.py \
  --zephyr "$ZEPHYR_SRC" --espressif "$ESPRESSIF_HAL" --xtensa "$XTENSA_HAL" \
  --sdk "$ZEPHYR_SDK" --python "$ZEPHYR_PYTHON" \
  --mode packet --profile speed --out target/zephyr-matched-packet-2
```

For wire/size, wire/speed, packet/size and baseline/size, repeat with
`--mode`/`--profile` and fresh output folders named
`zephyr-matched-wire-size-v3`, `zephyr-matched-wire-speed-v3`,
`zephyr-matched-packet-size-v3`, and `zephyr-matched-baseline-size-v3`.
For packet/speed controls, use `--cross-compile "$ESP_GCC_PREFIX"` with
`zephyr-matched-gcc14-packet-2`, `--native-defaults` with
`zephyr-native80-packet-2`, or `--native-defaults --lean` with
`zephyr-lean80-packet-2`. `speed` changes only workload/processing files to
`-O2`; it does not speed-optimize the whole Zephyr kernel.

Prepare the NuttX tree as described in the existing fixture README, then use
its `run_transport_matrix.py --build` for `c-wire-os`, `c-wire-2`,
`rust-borrowed-2`, `c-packet-2` and `rust-packet-2`, with
`--out-root target/nuttx-matched`. It also builds the required Rust baseline.
Build the minimal NuttX console baseline with the same resolved configuration:

```sh
python3 tests/service-footprint/build_c.py \
  --nuttx "$NUTTX_TREE/nuttx" --apps "$NUTTX_TREE/apps" \
  --baseline-config target/nuttx-matched/transport-rust-owned-z-v3/resolved.config \
  --source tests/zephyr-comparison/nuttx_baseline.c \
  --platform esp32s3-service-footprint --command cq_c_scale --stack-size 8192 \
--out target/nuttx-matched/baseline --reuse-kernel
```

For the matched packet C control, generate a source copy:

```sh
python3 tests/zephyr-comparison/generate_control.py \
  tests/service-footprint/channel_scale_mq.c target/channel_scale_packet_no_wake.c \
  --nuttx-without-wake
```

Build that copy with the existing helper's `c-packet-2` flags/sources,
substituting `--source` and using output
`target/nuttx-matched/transport-c-packet-matched-2`.
The checked-in NuttX and Rust fixtures are not edited.

Before **any** flashing, identify the board/port and read its complete 16 MiB
flash into a private local backup, verify its length and record its SHA-256.
Use `chmod 600` on the backup. Never commit or upload it: firmware may contain
credentials. `measure.py` alone does **not** restore firmware. Prefer the
matrix helper, which verifies frozen artifact hashes before flashing and
restores/verifies the full original flash in `finally`, including on failure:

```sh
python3 tests/zephyr-comparison/run_matrix.py \
  --artifacts target --out target/packet-matrix \
  --backup "$PRIVATE_BACKUP" --port "$DEVICE_PORT" --flasher "$ESPTOOL_V4" \
  --case c-packet-2 --case rust-packet-2 --case zephyr-packet-2 \
  --case zephyr-gcc14-packet-2 --runs 10 --blocks 3
```

The start order rotates between blocks. Run wire cases, sensitivity controls,
both baselines, and a `c-packet-matched-2`/`zephyr-packet-2` pair into other
fresh matrix folders. If restoration fails,
stop and restore manually from the private backup; do not assume the board
has its original firmware. Output files are immutable evidence directories,
not reusable build folders. Generate the publishable report with `report.py`
and each completed `--matrix` directory:

```sh
python3 tests/zephyr-comparison/report.py \
  --artifacts target --matrix target/packet-matrix --matrix target/wire-matrix \
  --matrix target/sensitivity-matrix --matrix target/baseline-matrix \
  --matrix target/strict-packet-matrix --readelf "$ESP_READ_ELF" \
  --nuttx-compiler "$ESP_GCC" --out target/comparison-results.json
```

The report rejects partial runs, changed
images, mismatched workloads and unverified restorations, and omits private
serial identifiers, host paths and backup contents.

Firmware provenance is separate from tooling provenance. For the measured
Zephyr images, every applicable C/header/configuration input matches this
tree, and regenerating the adapted workload produces exactly the frozen
`matched_control.c` bytes. The report checks both and fails on drift.
The original build snapshots also hashed unused files and Python tools that
changed during harness cleanup. Those old hashes remain labeled historical;
current build/measurement/report tool hashes are recorded separately. The
retired `speed.conf` was not used by these builds. This verifies the firmware
inputs and regenerated code, not byte-for-byte reconstruction of every
historical Python tool version. New builds use explicit input lists and
separate tooling snapshots instead of hashing this whole directory.

This finite successful-run fixture is not a production cancellation/error
recovery API, an ISR messaging test, a power study, or a complete HAL/service
framework qualification. Retain those limits when applying its results.
