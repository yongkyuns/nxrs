# NuttX heap and stack qualification

This qualification complements the host Rust `GlobalAlloc` measurements in
`docs/memory-qualification.md`. It uses NuttX's own procfs data on the
Cortex-M33/MPS2 qualification image; it does not reinterpret Rust allocator
counters as operating-system memory.

## Diagnostic builds

Add this to a flat NuttX product profile that uses the default allocator.
The option is off by default and is independent of the application.

```toml
[build]
diagnostics = true
# Optional: 32768 bytes by default; multiples of 4 from 4096 through 4194304.
diagnostics-trace-bytes = 65536
# Optional: 0 for task identity only, or 1..16 captured allocation frames.
diagnostics-heap-backtrace-depth = 4
```

The recorder is a fixed native buffer. Choose its size for the target's available
RAM and the duration/event density of the reproduction. Recording preserves the
start by default and stops admitting notes when full; explicit overwrite mode
can retain the tail instead. Neither mode guarantees a complete workload.
Without the backtrace-depth option, the target's existing allocator setting is
preserved. Captured frames add allocator metadata and work and require a working
target unwinder. These two options require `diagnostics = true`.

Diagnostic builds retain the final map, native debug symbols, and Rust line
tables for host-side source resolution. They also enable application markers and
select the existing `probe` implementation for consumed IMU/GNSS providers. These
providers record canonical device names with wake, drain, bus-transfer, or
read/parse/deliver work in task context. They add no recorder thread or heap
allocation per marker. A shared IRQ handler still does not establish which
attached sensor caused the interrupt.

ESP32-S3 diagnostics currently require effective `CONFIG_SMP=n`. Its hardware
performance counter belongs to each CPU, while the pinned overflow correction
keeps one shared epoch; counters from different CPUs cannot be treated as one
clock. Explicit `SMP=y` requests fail before native preparation, and a resolved
SMP configuration fails validation. Enabling diagnostics does not turn off SMP
in an existing product profile. A shared, qualified clock is required before
admitting ESP32-S3 SMP diagnostics.

Motion Tracker's `esp32s3-qemu-tracker-diagnostics` fixture profile enables the
option. For physical CoreS3 builds, use `cores3-tracker-diagnostics`.
Diagnostic builds with the default NSH init allow background jobs, so you can
run the app with `&` and inspect live workers from the same console. Take a
baseline, then capture a short reproduction. Save the console output as text.

```text
cat /proc/meminfo
ps
cat /proc/<pid>/stack
trace mode -o +w +i +d
trace start
trace stop
trace dump
cat /proc/meminfo
```

Run the workload between `trace start` and `trace stop`. The default 32 KiB trace
buffer retains the earliest events with `-o`; use `+o` deliberately to retain
the newest events instead. Each `trace` invocation needs a 4 KiB task stack.
Keep `+d` for named device scopes. Use `-w -i` to preserve more heap history when investigating
allocations. A `requests <heap> <count>` row
in `/proc/meminfo` counts allocator entries since heap initialization, even
when tracing is stopped. It includes failed requests, retries, and backing
allocations within other allocator calls. Taking a snapshot can itself use OS
memory. A slot taken from an already reserved application pool does not enter
the OS allocator.

```sh
python3 tools/nuttx-trace-to-perfetto.py console.txt diagnostic-trace.json \
  --elf target/build/<app>/<profile>/nuttx/nuttx
```

Open the JSON in [Memory Explorer](../tools/memory-explorer/README.md) or
[Perfetto](https://ui.perfetto.dev/) to correlate heap requests and usage with
scheduler switches, IRQ handlers, and named application scopes. Supplying the
ELF resolves IRQ/caller addresses to function and file/line, records resolution
coverage, and binds the converted trace to that host ELF's SHA-256. Pass
`--addr2line /path/to/target-addr2line` if the tool is not discoverable. The host
ELF still needs to be the one actually flashed; conversion does not attest that.
The caller can be a libc wrapper such as `malloc`. A request records entry,
including failed attempts; it is not proof of either success or failure.
Separate heap events retain successful operations and observed used-byte counts.
Use a matched ordinary image to measure production throughput.

The converter also accepts native watchdog events when a qualified producer
records them. They are excluded from the diagnostic preset because the pinned
native watchdog hooks bypass the runtime enabled filter. Enabling those hooks
manually can retain notes after `trace stop`.

Package each recording with `tools/memory-explorer/prepare_capture.py` and open
the resulting `session.json` in the viewer. Normal build receipts let the tool
find the linker map and select a bundled BSP memory layout automatically after
checking the ELF and configuration hashes. The layout catalog covers CoreS3,
ESP32-S3 DevKit/WROOM, Pico 2/2W, and the MPS2 QEMU fixture. It keeps external
memory mappings unknown when the build supplies only capacity. The BSP source
definitions and their provenance are checked in, so opening sessions works
offline. See [automatic board layouts](../tools/memory-explorer/README.md#automatic-board-layouts)
for selection, overrides, and source refresh.

Use a separate [recording tab](../tools/memory-explorer/README.md#keep-several-recordings-open)
for each run. Repeat `--session` or use **+ New** to keep a baseline and a
diagnostic capture open together. Each tab retains its own time axis, filters,
memory position, and validated evidence; switching tabs does not combine runs.

## What is measured

The MPS2 profile explicitly requires `CONFIG_FS_PROCFS=y` and
`CONFIG_STACK_COLORATION=y`, and keeps process/meminfo procfs entries enabled.

For both `std-demo` and `ao-stress`, the QEMU harness reads
`/proc/meminfo`:

1. before the first measured application lifecycle,
2. after one warm-up lifecycle,
3. after each of five checked lifecycles.

NuttX's meminfo implementation reports total, used, free, historical maximum
used, largest free block, used-block count and free-block count for each
ordinary registered heap. Reading meminfo also asks NuttX to reclaim delayed
frees before reporting the heap values.

The first application lifecycle is reported separately because one-time runtime
initialization may legitimately change the baseline. After warm-up, every
checked lifecycle must return `used` and `free` exactly to the warm baseline.
The largest free block may stay equal or improve, but may not shrink. This is a
strict repeated-lifecycle fragmentation/reclamation check; there is no arbitrary
byte tolerance.

## Active-object stack high-water

`ao-stress --stack-report` uses the unchanged workload and an observation-only
completion barrier. Each producer, worker and collector finishes its real
handler loop, then remains alive at the barrier. While every owner is paused,
the main task reads:

- `/proc/<pid>/status` for the NuttX task/thread name;
- `/proc/<pid>/stack` for `StackSize` and `StackUsed`.

NuttX stack coloration retains each owner's lifetime high-water use, so the
measurement happens after the workload without needing a sampling thread in the
hot path. The observer requires exactly the configured owner inventory and
rejects missing/duplicate owners, missing coloration data, zero sizes, or
`StackUsed > StackSize`.

Every detail is emitted as `NUTTX_STACK_RESULT` JSON followed by an independently
checked `NUTTX_STACK_SUMMARY`. The external harness validates all four scenarios
and five repeated runs, then records the worst observed owner usage.

The barrier exists only when `run_observed` is selected. Normal
`stress::run` uses no completion-ready channel/gate and the standard CLI path
is still executed twice in the same boot after the diagnostic cycles.

## Run

The existing firmware qualification invokes this automatically:

```sh
cargo nxrs build --app std-demo --profile mps2-an521-mock
cargo nxrs build --app ao-stress --profile mps2-an521-mock

python3 tests/host/run-std-apps-nuttx.py \
  --qemu "$(command -v qemu-system-arm)" \
  --app ao-stress \
  --image target/build/ao-stress/mps2-an521-mock/nuttx/nuttx \
  --log target/build/ao-stress/mps2-an521-mock/console.log
```

Parser and false-success controls run independently on the host:

```sh
python3 -m unittest discover -s tests/host -p test_nuttx_memory.py -v
```

## Interpretation limits

These values characterize the exact NuttX configuration and QEMU-executed
Cortex-M33 image. QEMU timing is not physical-MCU timing. Stack coloration
measures high-water bytes touched, not a proof that every future call path fits.
The heap lifecycle check can detect retained bytes or degradation of the largest
free block across the exercised cycles, but it is not a proof that arbitrary
future allocation patterns cannot fragment the heap.

The procfs reads themselves have implementation cost. Heap snapshots are taken
only after application termination and use the same command each time; stack
reads occur while owners are intentionally paused after their measured work.
None of these diagnostic observations is used as a throughput or latency
benchmark.

## Inspect addressed snapshots

[Memory Explorer](../tools/memory-explorer/README.md) opens the final ELF and GNU
map alongside addressed heap/task-stack observations in an egui viewer. Its
read-only GDB exporter records raw default-allocator chunk extents and stack
reservations. Unlike the procfs meminfo read above, it does not drain delayed
frees. It also does not sample stack coloration or measure requested payload
sizes, so keep using this qualification for lifecycle reclamation and stack
high-water evidence.

The viewer plots per-heap usage and observed peaks across independently captured
lifecycle points, with unknown runtime state between them. A host ELF hash prevents accidental artifact mixing;
it does not verify the running device image or prove allocator quiescence.
The firmware overview can use named linker memory regions without a descriptor;
physical backing requires an explicit board layout. The tool's guide describes
capture prerequisites, file semantics, controls, and the remaining qualification
work in issue #442.

### Real QEMU capture witness

The [repeatable capture commands](../tools/memory-explorer/README.md#qualify-a-real-qemu-capture)
reuse the existing `std-demo` / `mps2-an521-mock` build. The
[harness](../tools/memory-explorer/tests/qualify_qemu.py) boots that exact ELF,
runs `std_demo --case channels`, and compares every exported chunk boundary and
state, plus represented stack bounds, with an independent raw target-memory
walk. Captured allocator/task metadata must remain unchanged across each export.
The [Rust import test](../tools/memory-explorer/tests/qemu_capture.rs) then checks
the real observations against those raw records, including per-arena accounting
and unknown state between points. That test is explicitly ignored unless its
evidence directory and exact ELF are supplied.

The initial witness used QEMU 10.0.13 and GDB 16.3 with ELF SHA-256
`c08b9a62c164948cb348bf85d546f1a96b66143ba18306722c739158f596435f`:

| Stopped point | Raw chunks | Represented task stacks |
| --- | ---: | ---: |
| NSH ready | 27 | 3 |
| `std_demo_main` entry | 32 | 4 |
| Return after the channels case | 35 | 3 |

The workload passed and all three raw/export comparisons found no change in the
captured metadata during export. These counts describe this image and run, not
fixed future acceptance thresholds. The final capture recorded four deferred
frees; the exporter leaves them untouched, so this is not a leak finding or a
replacement for the drained, repeated-lifecycle gate above. Coordinates `0`,
`1000000000`, and `2000000000` label observation order, not measured durations.
The entry capture precedes the channels workload and does not measure peak
allocation or worker-stack use. No physical backing map is inferred.

The [std-apps workflow](../.github/workflows/std-apps-self-hosted.yml) runs this
additional capture/import gate for its existing `std-demo` ARM build after the
ordinary lifecycle checks. Its firmware artifact retains the exact ELF/map,
capture and independent raw JSON, configuration, hashes, tool versions, and
logs. QEMU execution validates the adapter on this Cortex-M33 runtime proxy;
physical boards and other allocator configurations still require their own
qualification.

### QEMU timing witness

The stopped heap observations above retain their supplied ordering coordinates.
Measured scheduling intervals come from a separate, continuously running native
NuttX test, [memory-timing](../tests/nuttx-std/memory-timing/). It records native
noteram scheduler switches, IRQ entry/exit notes, nested application B/E markers,
and a round counter. A worker performs four semaphore-driven busy/sleep rounds;
capture stops before the test prints its buffered notes over UART. This test
qualifies the capture/conversion/import path using an actual Cortex-M33 QEMU
image. It is not an NXRS `std-demo` benchmark or a qualification of every board's
diagnostic clock. Production diagnostic configuration uses
[`[build] diagnostics = true`](#diagnostic-builds); the clock option below belongs
only to this native test fixture.

The initial admitted timing witness used QEMU 10.0.13, ARM GCC 14.2.1, and ELF
SHA-256 `613479a51d53b2ced2ba0589fee20e5086d2624fd2a1415866982b95b2782b7b`.
Its source-clock observations ran from 20,057,500 to 55,574,900 ns. The converter
preserves that origin and presents a relative range of 0 to 35,517,400 ns.

| Observation | Initial witness |
| --- | ---: |
| Native scheduler switches | 17 |
| Native IRQ entries / exits | 34 / 34 |
| Native application marker records | 38 |
| Measured application / IRQ intervals | 17 / 34 |
| CPU / per-task residency intervals | 16 / 16 |
| Counter samples | 4 |
| Instant observations | 3 |
| Reported lost records | 0 |

These are recorded results, not fixed acceptance thresholds for emulator
interrupt counts or elapsed time. The
[ignored Rust gate](../tools/memory-explorer/tests/timing_qemu.rs) independently
parses the original UART note frame and compares every measured scheduler, IRQ,
and application endpoint, counter sample, and remaining note with the imported
timing model. It also requires the matching ELF, configuration and build receipt,
including the current fixture's source hashes. Ordinary host fixture tests do
not satisfy this gate.

The raw noteram dump has `complete=1`, no-overwrite mode, and no reported overflow.
The first and last scheduler boundaries lack one endpoint, so the converted
history correctly has `complete=false` and two unknown-duration markers. All
83 closed intervals have observed endpoints. CPU and per-task tracks describe
the same scheduled residency; IRQs and application scopes overlap them and must
not be added as disjoint CPU time. Application wall intervals can include
preemption or sleep.

#### Clock evidence and rejected attempts

The pinned ARMv8-M implementation's
[perf backend](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/arch/arm/src/armv8-m/arm_perf.c)
reads DWT cycle-counter state. In the unmodified local QEMU witness the counter
stayed at zero; the native test rejected it. A second qualification-only adapter
using SysTick-derived time advanced in task context but produced backward
timestamps at IRQ boundaries. Its raw failure evidence was retained and the
converter rejected it. Sorting, clamping, or debugger stop coordinates did not
replace those invalid timestamps.

The admitted adapter explicitly owns otherwise-unused CMSDK APB **timer1**, using
its secure address `0x50001000`. QEMU's
[Arm SSE mapping](https://github.com/qemu/qemu/blob/v10.0.13/hw/arm/armsse.c),
[AN521 MAINCLK](https://github.com/qemu/qemu/blob/v10.0.13/hw/arm/mps2-tz.c), and
[timer model](https://github.com/qemu/qemu/blob/v10.0.13/hw/timer/cmsdk-apb-timer.c)
establish the addressed peripheral and 20 MHz rate. The adapter checks the
peripheral ID and requires the timer to be stopped before taking ownership. It
enables a 32-bit free-running counter with interrupts disabled and resets the
epoch for each fixture invocation. The counter wraps after 214.7483648 seconds;
native success requires capture inside five seconds, and the host deadline is
capped at 120 seconds. No rollover correction or cross-capture clock alignment is
inferred. This is an emulator peripheral clock, not a physical CPU cycle cost.

The ordinary scheduler still uses SysTick. The pinned NuttX board configuration
assumes 32 MHz for that timer while QEMU AN521 supplies 20 MHz, so the fixture's
requested sleep lengths are not independent duration oracles. The trace displays
the intervals actually observed on the declared CMSDK clock. This adapter does
not alter the production board clock or establish a general MPS2 timing backend.

#### Reproduce from disposable pinned sources

Run from the repository root on a POSIX host with `arm-none-eabi-gcc`, ARM
binutils/newlib, `make`, `kconfig-conf`, `bison`, `flex`, `gperf`, and `genromfs`
installed and available on `PATH`. Use a QEMU release with the corrected MPS2 UART
wiring, as required by the ARM workflow. The initial witness used QEMU 10.0.13.
The helper requires these exact clean tracked source revisions:

```text
NuttX: 2b5c0de5e933281c209f954a63b99cf10c132346
apps:  3c772ea35b9245972e18999828f7b533e6fc57cc
```

The following uses the checked-out pinned submodules as shared Git object stores.
It builds in disposable copies and leaves the submodule worktrees unchanged.
Keep the source repositories while the shared clones are in use. Use fresh
source/output directories for each replay; evidence directories are never
overwritten.

```sh
git submodule update --init external/nuttx external/nuttx-apps
timing_sources="$PWD/target/memory-timing-sources"
timing_output="$PWD/target/memory-timing"
mkdir -p "$timing_sources" "$timing_output"
git clone --shared --no-checkout external/nuttx "$timing_sources/nuttx"
git clone --shared --no-checkout external/nuttx-apps "$timing_sources/apps"
git -C "$timing_sources/nuttx" checkout --detach 2b5c0de5e933281c209f954a63b99cf10c132346
git -C "$timing_sources/apps" checkout --detach 3c772ea35b9245972e18999828f7b533e6fc57cc

python3 tools/memory-explorer/tests/build_timing_nuttx.py \
  --nuttx "$timing_sources/nuttx" --apps "$timing_sources/apps" \
  --output "$timing_output/build" --qemu-timer-clock
python3 tools/memory-explorer/tests/qualify_timing_qemu.py \
  --qemu "$(command -v qemu-system-arm)" \
  --image "$timing_output/build/nuttx" \
  --config "$timing_output/build/resolved.config" \
  --output "$timing_output/capture" --timeout 60
python3 tools/nuttx-trace-to-perfetto.py \
  "$timing_output/capture/console.log" "$timing_output/capture/timing.json" \
  --source qemu-mps2-an521 --elf "$timing_output/build/nuttx"
NXRS_MEMORY_TIMING_EVIDENCE="$timing_output/capture" \
NXRS_MEMORY_TIMING_ELF="$timing_output/build/nuttx" \
  cargo +1.90.0 test --locked -p nxrs-memory-explorer \
    --test timing_qemu -- --ignored --nocapture
```

The builder rejects tracked source changes and records untracked inputs,
including the installed fixture. It resolves normal NuttX Kconfig and checks
every requested effective setting, including disabled and numeric settings.
`build/` retains the ELF/map, resolved configuration, compiler identity, exact
link flags, fixture hashes, configuration checks, and source/clock receipts.
`capture/` retains the original UART log, QEMU invocation/version, matching
hashes, clock/loss/completeness observations, and converted timing JSON. ELF
hashes and timings can differ on a later build; each capture must match its own
exact ELF and receipts.

The ARM `std-demo` CI job runs this native witness after its existing heap and
board-projection gates. It reuses the installed cross compiler and QEMU, clones
the pristine pinned submodules, and retains the whole
`memory-timing/{build,capture}` evidence directory in the firmware artifact.
The native timing ELF and the separate Rust `std-demo` heap ELF have different
identities; they are separate observations and are not one synchronized runtime
history.

### Linked QEMU lifecycle witness

The native timing fixture also supports a stopped-checkpoint mode that supplies
memory and timing from one firmware invocation. It qualifies the viewer's
[linked checkpoint navigation](../tools/memory-explorer/README.md#link-memory-checkpoints-to-timing),
including an actual free hole between live allocations. This mode retains the
ordinary continuously running timing witness above as a separate regression.

The [lifecycle harness](../tools/memory-explorer/tests/qualify_lifecycle_qemu.py)
starts the exact ELF in QEMU and supplies a fresh 128-bit recording nonce to
`memory_timing --checkpoints`. At each of four checkpoints, the single-core
fixture enters a native critical section, masks interrupts with PRIMASK, emits
`memory/checkpoint/<id>`, and reaches a hardware breakpoint. The admitted sole
note driver writes a fixed RAM ring and has no poll subscriber that could wake
another task. This preserves heap topology and task-stack reservations from the
marker to the debugger stop; it does not establish current stack usage.

The debugger checks those conditions, independently decodes the last native note
from the ring, and compares raw allocator headers and task metadata before and
after the read-only exporter. The dedicated timer must remain unchanged while
stopped. After the workload finishes, the host requires the ring note's exact
marker, CPU, task, and timestamp to match the raw UART note. Only then does it
write a correlated snapshot. Original exporter observations remain in separate
`unbound-N.json` files, alongside the raw observations and note bytes.

The first qualified interior-hole run used QEMU 10.0.13, GDB 16.3, and ELF SHA-256
`77f86bbabdd4c829ecd1d97ea4b11b84539460a65fc270edf44edd8599716242`.
Its recording ID was `32c749812a07eafbe1f65ac4dcd4402e` and the trace source origin
was 20,655,250 ns. These are observations from that run, not future thresholds.

| Checkpoint | Source time (ns) | Raw chunks | Task stacks | Allocated chunk bytes |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 21,290,100 | 37 | 4 | 21,528 |
| Three allocations | 27,382,900 | 40 | 4 | 87,208 |
| Middle allocation freed | 48,796,150 | 42 | 5 | 62,896 |
| Workload released | 84,967,650 | 40 | 4 | 30,032 |

The controlled requests are 16 KiB, 32 KiB, and 16 KiB. Each observed chunk has
48 bytes of allocator overhead. The three chunks occupy adjacent ranges
`[0x38012788, 0x380167b8)`, `[0x380167b8, 0x3801e7e8)`, and
`[0x3801e7e8, 0x38022818)`. At checkpoint 3, the middle range is explicitly free
and both neighboring allocation extents are unchanged. At checkpoint 4, all three
controlled payload ranges lie in observed free chunks. Thread creation and
retirement also affect the heap; the higher final total is not classified as a
leak. The gate checks addresses and states, not just the totals in this table.

The same recording contains 103 closed timing intervals, seven instants, and
four counter samples, with no reported record loss. Two scheduler boundaries
remain unknown-duration markers. Debugger stops pause the admitted peripheral
clock and perturb execution, so these intervals characterize this instrumented
QEMU run. They do not establish uninterrupted scheduling costs or physical-MCU
performance. Memory remains unknown between the four checkpoints; there is no
allocation-event replay or inferred lifetime continuity.

#### Capture and open the linked observations

First build the current native fixture using the disposable-source procedure
above. Keep `timing_sources` and `timing_output` set to those directories. Install
a Python-enabled `gdb-multiarch` and the pinned NuttX `pynuttx` dependencies as
required by the [memory capture prerequisites](../tools/memory-explorer/README.md#capture-a-nuttx-lifecycle-point).
Run from the repository root, using a new output directory:

```sh
lifecycle_output="$timing_output/lifecycle"
python3 tools/memory-explorer/tests/qualify_lifecycle_qemu.py \
  --qemu "$(command -v qemu-system-arm)" \
  --gdb "$(command -v gdb-multiarch)" \
  --image "$timing_output/build/nuttx" \
  --config "$timing_output/build/resolved.config" \
  --pynuttx "$timing_sources/nuttx/tools/pynuttx" \
  --output "$lifecycle_output"

NXRS_MEMORY_LIFECYCLE_EVIDENCE="$lifecycle_output" \
NXRS_MEMORY_LIFECYCLE_ELF="$timing_output/build/nuttx" \
  cargo +1.90.0 test --locked -p nxrs-memory-explorer \
    --test lifecycle_qemu -- --ignored --nocapture

cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf "$timing_output/build/nuttx" \
  --map "$timing_output/build/nuttx.map" \
  --layout tools/memory-explorer/layouts/mps2-an521-qemu.json \
  --snapshot "$lifecycle_output/capture-1.json" \
  --snapshot "$lifecycle_output/capture-2.json" \
  --snapshot "$lifecycle_output/capture-3.json" \
  --snapshot "$lifecycle_output/capture-4.json" \
  --trace "$lifecycle_output/timing.json" --view timing
```

Select checkpoint 3 and **Open captured memory**. Double-click its free middle
chunk to inspect the exact boundaries, then select **Show in timing** to return
to its measured marker. Select a time between checkpoints to inspect the
explicit evidence gap.

The [ignored Rust gate](../tools/memory-explorer/tests/lifecycle_qemu.rs) decodes
the saved native note bytes independently of the Python collector, parses the
UART markers, compares every chunk and represented stack, and checks the
ELF/configuration/current-fixture receipts. It also checks the exact interior
hole and final payload release. The ARM CI job reuses its native timing build
for this second invocation and retains `memory-timing/lifecycle/` beside the
ordinary timing capture. No production build switch, NuttX patch, or extra
firmware worker is introduced by the viewer's correlation support.

### Isolated heap topology replay witness

The [heap replay fixture](../tests/nuttx-std/heap-replay/replay_main.c) qualifies
ordered, committed chunk geometry for one fixed NuttX heap. It complements the
point observations above and leaves other heaps and task stacks unobserved.
The host contract and egui controls are documented in the
[memory explorer guide](../tools/memory-explorer/README.md#replay-one-heap).

The admitted configuration uses pinned NuttX
`2b5c0de5e933281c209f954a63b99cf10c132346`, one Cortex-M33 CPU in MPS2-AN521
QEMU, the flat default allocator, one region, and a single fixture owner. Pools,
SMP, interrupt-context mutation, heap extension, and heap lifecycle changes
during collection are outside this admission. The 20 MHz CMSDK clock resets
with each fixture invocation; the capture must stay below five source seconds.

The fixture uses a 64 KiB static arena. Link wrappers observe every admitted
`mm_lock` acquisition and `mm_unlock` release, including failed/no-op phases and
the temporary backing allocation inside `mm_memalign`. Each begin/commit probe
checks task context and native mutex ownership. Read-only GDB observations retain
raw header bytes under interrupt exclusion. Separate application safe points use
an independent raw walker after the public allocation operation returns.

Admission checks the actual final ELF branch destinations, including calls to
geometry helpers. It rejects direct lock calls that bypass the wrappers and
unexpected callers of the admitted helpers. Source/configuration hashes, a
strict target probe counter, transaction counters, and an independent UART
footer close the recording. The observer disables the probe breakpoint while
single-stepping past its entry; duplicate stops are rejected rather than
deduplicated by timestamp. No target function is called by the debugger.

The retained QEMU 10.0.13 / GDB 16.3 run uses ELF SHA-256
`aa44326577792359b3f89edcd2bf2dbd4a46337405658779ffda102a86fde0a6`, effective
configuration SHA-256
`70bdfda4e2c5b9c56be7d653c5c3b7b9dbc54aaeb146c7e88ae6d8de06b9b95e`, and recording
`ed2ec40d65493e4cd543ffd4f8549c45`.

| Observation | Measured result |
| --- | ---: |
| Committed allocator phases | 29 |
| Independent application safe points | 24 |
| Strictly ordered target probe stops | 84 |
| Source-clock coverage | 1,125,800–155,305,750 ns |
| Host capture elapsed time | 1.855069 s |
| Static fixture arena | 65,536 B |
| Allocator control prefix | 1,024 B |
| Journal chunk region, including both 48-byte guards | 64,512 B |
| Named linked fixture/clock storage | 65,636 B |
| Named linked fixture code | 2,996 B |
| Configured fixture task stack reservation | 8,192 B |
| Raw observations / journal / derived timing JSON | 181,211 / 53,210 / 21,697 B |

The code/storage counts sum named linked fixture symbols; they are not a
whole-firmware size delta and exclude linker padding. The stack number is a
reservation, not a measured high-water result. Host elapsed time and source
intervals describe this instrumented run. Debugger stops and wrapper execution
perturb it, so these numbers do not establish production allocation latency or
physical-MCU throughput.

The workload demonstrates splitting, an interior free hole, coalescing, realloc
shrink and in-place growth, realloc move and failure, aligned allocation's
intermediate state, pointer reuse, and delayed reclamation. Its final raw chunk
geometry matches the initial geometry. Deferred releases remain allocated until
the next actual reclamation. PID/backtrace fields written after unlock are not
used to invent application allocation identities or source ownership.

The retained `journal-loss-control.json` deliberately removes the real cancelling
allocation/free sequences 23 and 24 and their safe-point records from a copy of
the complete journal. Its equal endpoint geometry cannot hide the sequence gap.
The selected heap is unknown at source time **132,833,150 ns** and becomes known
again only at the full recovery checkpoint at **136,256,200 ns**, sequence 25.
This is an injected host-side negative control, not measured target data loss.

#### Build, capture, and inspect replay

Use the disposable pinned source trees from the timing instructions above.
Complete and retain the timing/checkpoint captures before reconfiguring those
trees for this separate native fixture:

```sh
python3 tools/memory-explorer/tests/build_replay_nuttx.py \
  --nuttx target/memory-timing-sources/nuttx \
  --apps target/memory-timing-sources/apps \
  --output target/heap-replay/build
python3 tools/memory-explorer/tests/qualify_replay_qemu.py \
  --qemu "$(command -v qemu-system-arm)" \
  --gdb "$(command -v gdb-multiarch)" \
  --build target/heap-replay/build/build.json \
  --output target/heap-replay/capture
NXRS_HEAP_REPLAY_BUILD="$PWD/target/heap-replay/build" \
NXRS_HEAP_REPLAY_EVIDENCE="$PWD/target/heap-replay/capture" \
  cargo test --locked -p nxrs-memory-explorer \
    --test replay_qemu -- --ignored --nocapture
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/heap-replay/build/nuttx \
  --map target/heap-replay/build/nuttx.map \
  --layout tools/memory-explorer/layouts/mps2-an521-qemu.json \
  --heap-journal target/heap-replay/capture/journal.json \
  --trace target/heap-replay/capture/timing.json --view memory
```

The derived timing file uses the same exact clock coordinates as the journal.
It shows instrumented mutation windows and committed/safe observations; it does
not add scheduler measurements. **Show in timing** links a covered heap coordinate
to that trace. A source time outside the imported trace clears its timing cursor
and disables the link. Select the loss-control journal to inspect missing
evidence and checkpoint recovery with the same physical address mapping.

The ARM std-demo workflow runs the collector and an independent Rust gate that
decodes retained raw headers and compares the imported geometry. Cross-platform
host tests exercise invalid replacements, sequence loss/order, equal-clock-tick
ambiguity, identity/configuration mismatches, physical aliases, and recovery.
The native viewer workflow also renders an explicitly synthetic heap journal.

This fixture adds no production allocator patch or worker. Continuous product
recording still requires a bounded recorder with qualified mutation coverage,
ordering, resource costs, loss detection, and recovery. Keep the product entry
point as `[build] diagnostics = true`; the existing request notes alone do not
supply that additional topology evidence. Track production qualification under
[#442](https://github.com/yongkyuns/nxrs/issues/442).

## Flat-build pthread key patchset

The pinned NuttX submodule and fork are not modified. At build time,
`tools/apply-nuttx-patches.py` applies the ordered, ordinary unified diff in
`upstream/nuttx/patches/` to the freshly archived `$OUT/nuttx` copy. It checks
the patch against the exact source context, fails closed on a source mismatch
or repeat application, and writes `nuttx-patches.json` with the NuttX revision,
patch digest, and before/after digests of every changed file. When moving to a
newer NuttX version, either the patch still applies cleanly or the build stops
so its semantics can be reviewed; there is no fuzzy application. The same
series is applied to standalone matched C firmware, preserving the kernel
configuration comparison.

The opt-in `CONFIG_TLS_GLOBAL_KEYS` change uses an image-wide pthread key
namespace and destructor table in flat builds. Per-thread TLS values remain
per-thread. Rust std's key-based TLS guard defers `thread_cleanup` until a
later destructor pass, so the build also sets `CONFIG_TLS_DTOR_ITERATIONS=4`
and clears each TLS value immediately before invoking a real destructor.
Keys without destructors keep their values until all passes finish; Rust's
`CURRENT` handle must remain visible to the deferred cleanup guard. The
global key table therefore tracks whether a key is allocated separately
from its optional destructor. Key scope defaults group-local and destructor
passes default to one when left at the upstream default. Nxrs flat Rust std
builds require these settings. The global key limit is
`CONFIG_TLS_NELEM` for the whole image, which is appropriate for the cached
keys in this linked runtime but should be considered before upstreaming.

The [protected CoreS3 qualification](../tests/nuttx-std/imu-protected/README.md#rust-runtime-witness)
uses one direct-init Rust task group with group-local pthread keys and four
destructor passes. Its target runtime witness checks thread cleanup across
repeated worker starts and joins.

The host patch tests exercise application, provenance, source drift, and
reapplication. The MPS2 QEMU check remains the behavioral gate for destructor
execution and complete heap reclamation.

## Previous repeated-launch failure

The [exact-head MPS2 run](https://github.com/yongkyuns/nxrs/actions/runs/36666402422)
passes host tests but fails the strict heap gate for both apps. After warm-up,
`std-demo` rises from 15,160 to 16,656 used bytes on the first checked launch;
`ao-stress` rises from 19,104 to 28,384 bytes. Further launches keep growing.
The assertions remain exact and have not been relaxed.

The failed-run `/proc/memdump` traces identify retained allocations in
`std::thread::Builder::spawn_unchecked_` (`Arc` thread handles) and Rust
thread-local initialization. In this flat NuttX image, Rust `std` caches a
`pthread_key_t` in an image-wide `LazyKey`; NuttX stores key destructors in
each task group's `task_info_s`. A later NSH launch makes a fresh task group
without recreating the cached key, so the Rust thread-cleanup destructor is
absent. That leaves a worker's current-thread handle and other TLS allocations
retained after join. The MPS2 diagnostic image enables NuttX backtraces and
Rust unwind tables to make these allocation sites visible; ordinary Pico 2
build flags are unchanged.

The product model of one Rust `main()` per MCU boot does not exercise this
relaunch mismatch. The exact repeated-launch gate remains unchanged; a larger
leak allowance or one-boot substitute would test a different contract.

Primary pinned NuttX implementation references:

- `fs/procfs/fs_procfsmeminfo.c` at the nxrs-pinned NuttX commit;
- `fs/procfs/fs_procfsproc.c` for `StackSize` / `StackUsed`;
- the nxrs MPS2 platform profile for coloration/procfs qualification.
