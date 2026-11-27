# Portable MPSC comparison

Build **`nxrs-mpsc-comparison`**, executable **`mpsc-comparison`**, firmware command
**`mpsc_perf`**. This qualification fixture uses one Rust workload with three
separately linked transport choices:

| Runtime / backend | Transport | Worker execution |
| --- | --- | --- |
| `nuttx` / `std` | Actual `std::sync::mpsc::sync_channel` | `std::thread` |
| `nuttx` / `native` | Public POSIX `mq_*` through a target-header adapter | The same `std::thread` implementation |
| `zephyr` / `native` | Native `k_msgq` through a target-header adapter | Native Zephyr threads |

The NuttX pair substitutes the queue under the same Rust workload, startup and
thread model. It is not a native-C baseline or a measurement of all NXRS layers.
The Zephyr arm does not port Rust std to Zephyr. The
[broader characterization](../../docs/rtos-characterization.md) explains the
other criteria and experiments.

`rust/` owns workload scheduling, message validation, endpoint retirement and
joined cleanup. `sdk_{nuttx,zephyr}.c` translate native operations;
`boards/` owns clocks, console setup and native startup. NuttX profiles are in
`tests/profiles/{esp32s3,pico2w}-mpsc.toml`. No production HAL API or
benchmark Cargo feature is added.

## Host capture admission comparison

The serial, harness-free
[capture fixture](rust/tests/capture.rs) source-includes the telemetry service's
current `Capture` and clock implementation used by Motion Tracker. It compares their observable
capture behavior with desktop `std::sync::mpsc` and pinned `heapless::spsc`
0.9.4 queue models. This is a host contract and storage comparison. It does not
measure timing or run the patched NuttX std backend. The source-included service
uses the same shared scalar transport exercised by the pool controls.

Run it from the repository root:

```sh
mkdir -p target/mpsc/capture-host
CARGO_TARGET_DIR=target/qualification-build-driver \
CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 \
CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=1 \
cargo test --manifest-path tests/mpsc-comparison/rust/Cargo.toml \
  --test capture --locked > target/mpsc/capture-host/results.log
python3 tests/mpsc-comparison/capture_peer.py target/mpsc/capture-host/results.log \
  --out target/mpsc/capture-host/python-peer.json
```

The executable emits `CAPTURE_METADATA`, `CAPTURE_COMPARE`, `CAPTURE_CHECK`,
`CAPTURE_PROTOCOL`, `CAPTURE_POOL`, `CAPTURE_POOL_MEMORY`, `CAPTURE_PEER` and `CAPTURE_GATES`
JSON records. Metadata hashes the compiled-in capture, clock, fixture, lifecycle
gate, reference adapter, pool facade, pool checks, manifest and lockfile
bytes using Git blob SHA-1. Separate fields bind the actual shared production
gate, pool implementation and capture budget policy; facade hashes alone would not
identify the compiled transport. Retain the
whole output and the command's exit status. The existing allocation probe
observes requested Rust heap bytes, not process RAM or native allocator use.
The harness is disabled because an ordinary libtest harness can allocate
concurrently with an allocation window. The source-included module's own
`#[test]` functions are not executed by this harness; they remain in the
telemetry service test suite.

The peer checker executes the unchanged `dashboard.py::decode_batch` function
against eight vectors emitted by the production telemetry encoder, including mixed-source
v1/v2/v3 batches, count encoding and float fallback, UTC, wrapping sequences and
timestamps, and overflow framing. It binds both compiled source blobs and the
current decoder source, and rejects six deliberately damaged controls. It loads
only that function's AST with `struct`, so no BLE/UI packages are required.
This is Python decoder execution; terminal framing has a separate oracle, and
it does not run Swift, an iPhone or TLS/BLE. Nonfinite scalar preservation is a
transport control; the Swift client intentionally rejects nonfinite readings.

Every variant has **512 usable record slots**:

| Variant | Admission capacity | Extra storage distinction |
| --- | --- | --- |
| Production pooled `Capture` | One 512-record pool across all sources | Reserves before acquisition, retains while inactive and releases at final endpoint teardown |
| Desktop std MPSC model | One 512-record pool across all sources | Reports actual requested setup allocation; this is the host standard library |
| Even SPSC model | IMU 1: 171, IMU 2: 171, GNSS: 170 | Three rings use 172, 172, and 171 physical slots |
| Rate SPSC model | IMU 1: 254, IMU 2: 254, GNSS: 4 | Three rings use 255, 255, and 5 physical slots |

The two partitions are experiment inputs, not accepted product budgets.
`heapless::spsc::Queue<T, N>` has `N - 1` usable slots. Defaults and
`portable-atomic` fallbacks are disabled; this host witness uses the crate's
native-width atomic load/store implementation. The fixture adds no unsafe
queue code. Source inspection establishes exclusive producer/consumer endpoint
ownership and release/acquire publication, but target atomic instructions and
scheduling still need native qualification.

Each matched trace withholds consumption while offering either two or three
seconds of nominal 100 Hz / 100 Hz / 2 Hz data, one active IMU, a GNSS burst,
or unequal source bursts. Canonical queue records retain exactly the scalar
fields exported by the real capture path. Independent wire decoding checks
source, sequence, timestamp truncation and payload bits; it makes no assumption
about the layout of Capture's private `Sample` type. The reported queue record
layout, inline object size and requested setup heap allocation are distinct
quantities. Inline size is not stack high-water measurement.

The current capture path runs its actual record, readiness, host unpark, batch,
overflow marker and finish operations. Queue models deliberately stop admission
at their first Full in the serial trace, return the unadmitted record, and
classify subsequent offers as closed. They are **not concurrent export
protocols**. Their rotating three-source drain preserves each source's FIFO
without waiting for an absent source; cross-source interleaving can change.
All rows reconcile offered = admitted + full + closed and admitted = delivered
after draining. They report unused slots at overflow, exposing capacity stranded
in other rings instead of treating equal total capacity as equal admission.

The production pool and encoder subsequently process each result in batches
of 16 outside allocation windows. Independent decoding verifies full-float v1,
v2 and v3 payloads and per-source FIFO; cross-source interleaving may change
again when records enter the pool. The reported post-processing wire sizes are
therefore not measurements of a replacement SPSC exporter. UTC anchoring,
native-count reconstruction, partial transport writes and client behavior remain
with their owning fixtures. Production setup and inactive-retention byte counts
include its separate clock/wake object, while the pool-only harness has a
different allocation scope.

Additional witnesses check non-Copy value ownership through Full, ring wrap and
retirement; three producers and a stalled consumer exchange 1,536 records across 32
full/drain rounds; and simultaneous overflow of the real Capture retains one
missing identity, drains its admitted prefix and restarts cleanly. Dropping a raw
SPSC consumer does not close its producer, unlike dropping an MPSC receiver.
Closure therefore needs an explicit capture protocol.

A deterministic negative control pauses an SPSC producer after its last epoch
check. The controller closes admission, observes an empty ring and starts a new
epoch before allowing the old commit. The old record then appears. This is an
expected failure of the **epoch-check-only protocol**, not of the SPSC queue.
Stable ring storage is retained throughout the test. Empty storage is not proof
that every admitted producer is quiescent, and a last epoch check is not an
atomic commit barrier.

### Concurrent capture lifecycle fixture

The [production lifecycle gate](../../shared/messaging/src/pooled/gate.rs) owns
admission, overflow identity, cancellation and retirement. Its explicit `probe`
feature exposes qualification boundaries to two independent payload paths. The [reference adapter](rust/tests/support/capture_protocol.rs) uses
real host threads, non-Copy payloads and a 512-slot desktop std MPSC queue. The
[pool facade](rust/tests/support/capture_pool.rs) calls `messaging::pooled`
without implementing storage or lifecycle again. Both use safe Rust. The
telemetry service uses messaging's `pooled` feature and receives the application's
traffic and storage budgets. The comparison compiles that service's production
capture and clock sources and binds their current paths and hashes to its output;
it adds no worker or separate runtime. The original serial comparisons and
epoch-only negative control remain independent reference checks. Reference
ownership witnesses and event accounting run outside the allocation windows;
they are not a product memory measurement.

One `AtomicU32` coordinates the following state:

| Field | Width | Meaning |
| --- | ---: | --- |
| Phase | 2 bits | Idle, open, overflow or cancelled |
| Occupied credits | 10 bits | Accepted records reserved or retained, up to 512 across all sources |
| Busy sources | 3 bits | One outstanding publication or closing obligation per source |
| Epoch | 17 bits | Session identity; exhaustion fails explicitly without wrapping |

Three exclusive producer endpoints reserve a shared credit and their source bit
in the same compare-and-exchange that validates the captured epoch and open
phase. The first offer at genuine capacity closes admission and holds its source
bit until the first-missing identity is published. A failed compare-and-exchange
requires reconsidering the new state; it is not itself evidence of overflow.
Rejected offers return their original values.

The owner can drain visible records while another source is paused. Closing
remains Pending while publication is outstanding; the overflow terminal and
restart wait until every credit and source obligation has retired. Terminal
state remains observable until the owner explicitly finishes the closed epoch.
Occupied credits and busy sources are independent: a consumer may release the
last queued record before its producer retires. Forgetting a permit strands its
source bit and keeps the epoch pending; it cannot overflow a counter or permit
unsafe reuse. Abandoning an accepted permit cancels the session, so discarded
work cannot be presented as a lossless overflow prefix.

The transport checks per-source FIFO and accepted-record accounting. Credit
reservation order is not global payload order: another source may publish while
an earlier reservation is paused. This differs from the previous mutex queue's
cross-source admission order. The production wire decoder keeps each source's
sequence and delta timestamp independently.

The 24 `CAPTURE_PROTOCOL` cases cover pooled admission under nominal and skewed
loads; simultaneous overflow and the last shared credit; a credit returned
between observing Full and attempting to close; paused admission, marker
publication and producer retirement; cancellation, stale epochs and reuse of
source sequence numbers; abandoned and forgotten permits; unwinding after
dequeue; epoch exhaustion; setup rollback before opening; and concurrent FIFO
with credit return. Non-Copy values have independent identity and exactly-once
drop witnesses. Each report reconciles admitted values with delivered,
explicitly discarded and still-retained values. The deliberate forgotten-permit
case retains one obligation and does not claim successful teardown.

Three of those cases use fresh owner threads with readiness checks and a 5 ms
bounded host poll. They check retained-data progress before a delayed unpark and
notification during unwind. They do not force publication into the exact final
recheck-to-park window or qualify the production batch deadline and wait loop.
The app test suite separately exercises the actual communications endpoint
handoff and export session. The host reference alone does not establish them.

These host checks establish behavior for the controlled schedules in the fixture.
They do not qualify desktop std MPSC as a nonblocking NuttX transport, prove
bounded compare-and-exchange retries, or measure acquisition latency. The epoch
limit is 131,071 successful opens per acquisition lifetime. The product returns
an exhaustion error rather than wrapping or implicitly replacing live endpoints.

### Shared pooled payload transport

The pooled adapter preallocates 512 shared payload slots, a free-slot bitmap and
three source-owned index rings. Any source can use all 512 payload slots; the
index rings do not partition payload capacity. A record stores its sequence and eight uninterpreted payload words as nine
atomic 32-bit words. The app maps its 64-bit time and six sample words into that
payload; the shared transport has no sensor or wire-format dependency. A release/acquire
cursor publishes the complete payload and its slot index. The sole owner checks
at most three source lanes and skips a producer that has not published yet.
This preserves per-source FIFO, without imposing the mutex queue's cross-source
order or proving a bound on the entire poll operation.

Physical slots return before logical credits, so an admitted producer has
backing storage available. Unwind cleanup follows the same order. A published
record belongs to the consumer even if its publisher later unwinds. Dropping
the owner closes admission; outstanding producers keep storage alive until they
retire. Dropping the last publisher closes an open epoch without replacing an
overflow terminal. Opening rechecks source lifetime: an empty disconnected epoch
rolls back, while accepted records remain available for explicit accounting.

The [22 pooled controls](rust/tests/support/capture_pool_checks.rs) check complete
payload bits, nominal and skewed admission, source FIFO, full-ring reuse,
cancellation/restart, unwind, owner drop and last-publisher races around opening.
Three controlled pauses occur after slot claim, midway through payload writes,
and before cursor publication. In each case another source publishes and the
owner drains 511 records while the paused producer retains one obligation.
The original 20 comparison rows, four controls and 24 reference lifecycle cases
remain in the same executable.

The [historical host run at `60ee9f07`](https://github.com/yongkyuns/nxrs/issues/446)
requested **21,712 B in five setup allocations**. Its first offer/read and 4,096 subsequent
round trips made zero Rust allocator calls. The raw storage components are:

| Component | Layout | Bytes |
| --- | --- | ---: |
| Shared payloads | 512 × 9 atomic 32-bit words | 18,432 |
| Source indices | 3 × 512 atomic 16-bit indices | 3,072 |
| Free-slot bitmap | 16 atomic 32-bit words | 64 |
| Published/consumed cursors | 3 × 2 atomic 32-bit words | 24 |
| Raw component total | Included in the measured setup allocation | 21,592 |

The remaining 120 requested bytes contain host structure/control metadata and
alignment. Native allocator overhead, process RAM and stack high-water are not
measured by that receipt. All **21,712 B remained allocated while inactive**;
normal final endpoint teardown reclaimed them. The previous mutex `Capture`
released its **20,480 B payload allocation** on finish. Those payload-only and
complete-arena numbers have different scope and lifetime; their subtraction is
not a full product heap comparison. Current allocator records must be collected
again because promotion adds peak accounting, configurable notification and a
feature-dependent probe counter.

Production ordered capture now uses this transport. `CAPTURE_GATES` reports
`candidate_integrated: true` while keeping native qualification separate.
Each callback owns one source publisher and `Telemetry` transfers the exclusive
consumer through the existing worker's failed-spawn and joined-reuse paths.
The app's 22 KiB requested-pool budget is checked before allocation. The fixed
arena remains reserved between exports; a headless run without BLE does not
allocate it. The two-second stall budget includes resolved IMU rates, a declared
2 Hz GNSS assumption and 64 burst records. Oversized IMU configurations fail
before sensor startup. The exported wire still has 512 shared admissions and
16-record batches.

Normal shared-library builds exclude the public pause hooks. The independent
allocator harness also runs without `probe`, checks that a too-small budget
performs no allocation, compares its requested-byte calculation with the actual
five setup allocations, and checks no first-use/steady allocation or inactive
release. Notifier-owned referents and target allocator metadata are outside that
pool budget. The four array allocations are fallible; the final small `Arc`
control allocation uses the standard allocator contract.

At source `60ee9f07`, the single-core MPS2 NuttX
[`capture` suite](../owner-qualification/README.md#pooled-capture-transport-witness)
passed `capture-pool-progress` and the same-image `std-mutex-inherit` control in
fresh QEMU boots; both retained captures passed independent replay. Build
`8c3b0380` checks the actual shared Gate/pool: a priority-120 producer pauses
after claiming a physical slot while a priority-180 owner publishes and drains
the other 511 records, then blocks until publication and obligation retirement
permit completion. This is evidence for that exact fixture revision and
controlled schedule, before the later production owner-registration integration.

The application suite owns wake/clear/recheck/sleep, batch deadlines, partial
writes, cancellation/reconnect and v1/v2/v3 compatibility checks. Target adoption
still requires matching evidence for each deployed target's atomics, scheduling,
active/idle heap and stack high-water. Bitmap scans and compare-and-exchange retries
have no per-call SMP bound. These controlled schedules and the separate host
windows with zero allocator calls do not establish WCET, ISR
suitability, physical acquisition latency or a whole-product deadline. See
[#446](https://github.com/yongkyuns/nxrs/issues/446) and the
[capture contract](../../docs/concurrency-event-communication.md#motion-tracker-observations-and-capture).

## Build

Use Python 3.11+, Cargo, CMake/Ninja, pyelftools for Pico placement and pyserial
for physical capture. Initialize the pinned NuttX/apps submodules. The normal
`cargo nxrs` frontend prepares its private patched Rust SDK; installed toolchains
are unchanged. Copy [toolchains.example.toml](toolchains.example.toml) outside the
checkout, then fill in your existing tool paths. Zephyr is pinned to 4.5.0;
module revisions and board contracts are in [boards.toml](boards.toml). The Rust
compiler must match the NuttX SDK: ESP 1.90.0.0 for S3, nightly-2025-09-15 for Pico.
The S3 `nuttx-tools` directory must include the normal frontend's
`downloads.sha256`; `path` can add existing native build tools such as genromfs.

From the repository root, using that Python environment:

```sh
python tests/mpsc-comparison/bench.py build --board pico2w --runtime nuttx \
  --backend std --tools /path/to/local-tools.toml --out target/mpsc/pico2w/nuttx-std
python tests/mpsc-comparison/bench.py build --board pico2w --runtime nuttx \
  --backend native --tools /path/to/local-tools.toml --out target/mpsc/pico2w/nuttx-native
python tests/mpsc-comparison/bench.py build --board pico2w --runtime zephyr \
  --tools /path/to/local-tools.toml --out target/mpsc/pico2w/zephyr
```

Use `--board esp32s3` and separate output paths for the S3 pair. Output directories
must be new. `build.json` identifies the exact image and ELF and binds sources,
resolved configuration, compiler versions and relevant native build evidence.
Failed builds remain failed receipts. Capture/replay reject changed inputs or
artifacts; replay historical captures from their matching source checkout.

Backend and observer selection are fixed at build time and bound into the v4 receipt. The
final ELF must retain the native queue adapter only in a native-backend image.
Both NuttX images use the same profile, including enabled POSIX queues and
support for every tested payload. This is a controlled substitution, not the
minimum configuration for a standalone std-only application.

The NuttX transport comparison deliberately retains its historical kernel-PI-off
baseline. Its marked qualification package requests
`--qualification-mutex-policy kernel-pi-disabled` with an explicit reason through
the normal frontend. Both transport builds use that same control; their profiles
no longer disable PI themselves. Ordinary NuttX builds, including direct builds
of these profiles without the control, use the
[default resource-mutex policy](../../docs/build-system.md#resource-mutex-policy).
The retained frontend configuration and policy evidence distinguish the
experiment from that default.

Before collecting the NuttX control pair, validate the shared substrate:

```sh
python tests/mpsc-comparison/bench.py check-control \
  --nuttx-build target/mpsc/pico2w/nuttx-std \
  --nuttx-native-build target/mpsc/pico2w/nuttx-native
```

This checks the resolved configuration, compiler identities, patch receipts and
every non-application kernel archive. Raw archive hashes are retained. A separate
per-member fingerprint strips debug sections only, preserving symbols,
relocations, code, data and member order, including every occurrence of duplicate
member names. The NuttX builds use the pinned NuttX commit time for
`SOURCE_DATE_EPOCH` and map their owned build directories to a common prefix
before compilation. Timestamp and assertion-path bytes therefore stay in the
objects with reproducible values; the comparison does not remove them.
Differing debug paths do not imply a different kernel implementation, but
runtime-byte or section-attribute differences still fail the control.
Application archives and final image addresses differ
by design; a successful check does not establish matched final hot-path placement.

Both kernels use one core, FIFO workers of equal effective priority, a 100 Hz
tick, no timeslicing, no radio or external RAM, an 8 KiB controller and 4 KiB
worker stacks. Pico uses Rust Oz/fat LTO; S3 uses O2/no LTO. Compare kernels on
the **same board**, not absolute numbers across these optimization policies.
Timing uses S3's 16 MHz SYSTIMER scaled to 240 MHz units, or Pico's 1 MHz TIMER0,
identically on both kernels; these are wall counters, not CPU-time measurements.
The full image includes the sample array (62,720 bytes), release records, stack reservations
and SDK resources; it is not the production channel's memory footprint.

Pico hot operations are relocated by changing input section names only, with
instruction-byte checks, then linked through each OS's startup SRAM copy path.
Early memory routines stay in flash on both Pico kernels. For NuttX, the
placement step redirects compiler-generated memory calls only in the reset
object and verifies the reset call targets in the final ELF. S3 uses the shared
fixture IRAM fragment alongside each kernel's native wait/scheduler placement. Build receipts
are not hardware qualification: inspect linked hot operations and indirect
callees before claiming a new cohort has matched placement. The linked-image
audit on both boards records direct paths, noninternal memory boundaries,
missing roots and unresolved or indirect calls. Existing Pico std and Zephyr
SRAM gates remain required. The newly exposed NuttX native path records its
additional dependencies as diagnostic evidence; it does not silently promote
all queue/allocator/descriptor code or declare placement matched.
The ESP ROM-prefix digest covers the visible RAM prefix, not hidden flash
segments, and is not firmware authentication. The carried NuttX loader fix
skips that declared digest before reading the next segment; flag-zero images
retain their existing layout.

## Run and replay

The entry point deliberately **does not flash, back up, reset or restore** boards.
Use the board's established tools to create a fresh private full-flash backup,
verify it, flash the receipt's `image_file`, independently verify flashing, and
reset into that image before every capture. Restore and verify the original
image when the campaign ends, including after failures. Pico USB recovery uses
[`tools/pico-bootloader.py`](../../tools/pico-bootloader.py); an interrupted Zephyr
report may need its CDC output drained while holding 1200 baud before recovery.

Create a separate private session file for each capture:

```json
{
  "schema": "mpsc-comparison-session-v1",
  "image_sha256": "SHA256_OF_THE_RECEIPT_IMAGE",
  "fresh_backup_verified": true,
  "flash_verified": true,
  "fresh_boot": true,
  "pass": 1
}
```

These are operator attestations, not measurements made by the capture tool.
The firmware accepts one workload per boot and repeats `MPSC_READY` while
waiting for its command; open capture within 120 seconds after reset. No NSH
prefix is needed: the standalone image starts `mpsc_perf` directly.

```sh
python tests/mpsc-comparison/bench.py plan --passes 3 --include-native-control \
  > /path/to/private-plan.json
python tests/mpsc-comparison/bench.py run \
  --build-directory target/mpsc/pico2w/nuttx-std --port /dev/your-console \
  --case stream-p4-b16-q8 --session /path/to/session.json \
  --output target/mpsc/captures/pass-1-nuttx-std-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py replay \
  --build-directory target/mpsc/pico2w/nuttx-std \
  --output target/mpsc/captures/pass-1-nuttx-std-stream-p4-b16-q8
python tests/mpsc-comparison/bench.py compare \
  --nuttx-build target/mpsc/pico2w/nuttx-std \
  --nuttx-native-build target/mpsc/pico2w/nuttx-native \
  --zephyr-build target/mpsc/pico2w/zephyr \
  target/mpsc/captures/pass-1-*
```

The plan has 16 cases per implementation: sustained throughput; ordinary stream; paced,
burst and overload arrivals; verified full-queue backpressure; 64-byte messages;
and a 64-slot stream control, each with one/four producers. With the native
control enabled, it rotates the three implementations across passes, giving
each one every position over three passes. The default two-implementation plan
still alternates order. Start with a pilot per implementation, then use fresh
boots. Capture every selected implementation before comparing a case/pass; the
single capture example above is only one member. Repeat a focal case as another
pass rather than pooling samples. Comparison accepts either native comparator
or both, and rejects missing or duplicate implementation/case/pass records.

Strict replay checks complete offered/admitted/rejected/delivered accounting,
payload checksum, per-producer FIFO, blocking proof, scheduling, stacks and
joined cleanup. Report throughput, delivery/admission/release tails and losses
together. Normal-case loss gates remain explicit. CPU accounting is unavailable
in these primary images and is reported as unavailable, never zero. Comparison
requires the same board, source inventory, contract and Rust/C compilers, pairs
each case within its pass and never pools distributions. Passing replay alone
does not establish a product deadline or physical timing qualification.

The wait observer distinguishes the evidence: native transports observe
the measured queue object; the NuttX std transport observes semaphore-blocked
state with the engine's phase protocol; host checks make no native scheduling
claim. The NuttX native observer also checks the appropriate send/receive wait
state. The native adapter uses public `mq_*`, preserving native error and
cancellation-point behavior, but this workload does not request cancellation
or qualify timed operations and endpoint disconnection. Nonblocking full sends
return the original payload; interruption and other failures are not hidden as
full-queue rejection or retried silently. Native message allocation and pools
remain part of the implementation cost.

## Separate CPU-accounted diagnostic

The default `--observer primary` keeps runtime accounting disabled. On
ESP32-S3, build separate images with `--observer cpu`, applying that option to
each selected runtime/backend and using new output directories:

```sh
python tests/mpsc-comparison/bench.py build --board esp32s3 --runtime nuttx \
  --backend std --observer cpu --tools /path/to/local-tools.toml \
  --out target/mpsc/esp32s3/cpu-nuttx-std
python tests/mpsc-comparison/bench.py plan --passes 3 --include-native-control \
  --observer cpu > /path/to/private-cpu-plan.json
```

The same `run`, `replay`, `compare` and `check-control` commands use the observer
bound to the build. Capture refuses a CPU image without complete CPU observations;
comparison refuses a mixture of primary, CPU and IRQ observer builds. Keep each
observer's passes and distributions separate. The CPU observer is unavailable
on Pico: its performance-counter startup has not been qualified, and the build
rejects this selection.

The CPU build enables NuttX hardware performance events with overflow correction
and per-thread runtime accounting, or Zephyr thread runtime statistics using its
timing counter. A 20 ms busy interval before warmup checks counter availability
and gross unit errors against the independent wall counter. Its record is
separate from workload accounting; it is not an external clock calibration.
The existing empty-receiver interval also records that receiver's CPU charge.

These are **participant thread charges**, including task-context kernel work.
The NuttX counter includes interrupt work attributed to a scheduled thread.
Zephyr's Xtensa hooks stop charging before ISR dispatch. Reports retain distinct
`cpu_scope` values and state whether their charge scopes match. The NuttX
std/native pair shares one accounting scope; cross-kernel values are not a
matched measurement of total MCU active time or energy. Accounting changes
kernel behavior and code placement, so CPU-image latency is diagnostic evidence
and does not replace the primary timing distribution.

Heap snapshots also have explicit availability and scope. NuttX reports the
allocator arena visible through `mallinfo`: current usage, peak since boot,
free space and largest free block. This is not a timed allocation count or a
channel-only footprint. Zephyr and host heap observation is unimplemented and
is emitted as `available: false`, `scope: "unavailable"` and null values. All
unavailable CPU durations are null as well; replay rejects fabricated zeros or
the former integer sentinel where an unavailable observation is required.

## Separate interrupt-response diagnostic

On ESP32-S3, `--observer irq` builds a separate image for each selected
runtime/backend. CPU accounting stays disabled. Pico rejects this observer.
Use the same workload plan with `plan --observer irq`; never pool its timings
with primary or CPU images. The diagnostic generates one normal, maskable,
priority-1 interrupt every nominal 100 microseconds. This deliberately adds
interrupt and sample-recording work to the workload.

The probe owns **SYSTIMER target1 on counter0** only in the pinned single-core
configuration. OS alarm target0 and ESP timer target2 remain untouched. It
refuses an active, pending, externally mapped or SDK-claimed target1; NuttX
also checks its IRQ handle registry. It reads the existing 16 MHz counter
without resetting it or changing its clock/stall policy. Allocation occurs
only when acquiring the SDK interrupt handle, before the measured phases.
The existing wall-clock fixture initializes its counter policy separately.
Zephyr's board supplies a 4096-byte system-heap minimum even though this fixture
requests `HEAP_MEM_POOL_SIZE=0`; the IRQ image verifies that minimum is enabled.
The absence of a Zephyr heap observation does not imply absence of a heap.

The measurement is **programmed hardware alarm deadline to the first probe
counter read after ISR dispatch**. It includes interrupt masking, pending IRQ
arbitration, dispatch and the probe's entry/read cost. It is not a measurement
of critical-section duration alone, an external GPIO edge, or a calibrated
external clock. The runtime validates CPU0, the actual priority-1 interrupt
line and the four probe root addresses. The ELF-bound `irq-audit.json` also
inspects direct dependencies without cold-path exclusions. Flash, other-memory,
compiler-helper and unresolved indirect boundaries remain explicit; root
placement alone never qualifies the complete interrupt path. No dependencies
are automatically promoted.

An idle control covers the existing 100 ms empty-receiver interval, then a
separate loaded phase covers the workload release through its completion.
The Rust protocol preserves absolute cadence, skips overdue periods without
an ISR catch-up loop, and records `missed_periods`. Before inspection or output,
the adapter disables and acknowledges only its alarm, quiesces delivery on
the single core, and releases its handle. It restores its saved disabled target
configuration before releasing ownership and leaves the SDK's disabled matrix
route. It performs no timer-register writes after freeing the handle. Release
failure invalidates the run; SDK descriptor caching is not reported as complete
allocator reclamation.

Each phase retains its **first 256 samples** in fixed storage. All serviced
events still contribute to the event count and maximum, including events whose
individual records were dropped. Reports therefore label percentiles as
`retained_first_sample_lateness_ticks` and the maximum as
`maximum_serviced_lateness_ticks`. `dropped`, `missed_periods`,
`trailing_periods` and `pending_at_stop` remain visible. Deadlines that passed
without an ISR entry before shutdown have no measured latency; a loaded phase
with no delivered interrupt reports null statistics, not zero. An idle control
with no delivery fails. The record includes its fixed sample bytes and total
Rust probe-state bytes; these exclude SDK interrupt-manager overhead. This
observer does not establish physical timing or matched-path qualification.

## Inspect a prepared cohort without hardware

`inspect` revalidates the build receipt, source/configuration identity and bound
image/ELF artifacts. `check-cohort` applies the same matching rules used by
comparison and also checks the exact NuttX std/native kernel substrate:

```sh
python tests/mpsc-comparison/bench.py inspect --build-directory target/mpsc/esp32s3/nuttx-std
python tests/mpsc-comparison/bench.py check-cohort \
  --nuttx-build target/mpsc/esp32s3/nuttx-std \
  --nuttx-native-build target/mpsc/esp32s3/nuttx-native \
  --zephyr-build target/mpsc/esp32s3/zephyr-native
```

All three builds must use the same board, sources, compiler cohort and observer.
A successful preflight means those compiled controls match. Physical timing,
matched final operation placement and product qualification remain false.

Keep raw console logs, tool paths, receipts and flash backups local; boot output
can contain device identifiers. The physical capture workflow uploads nothing. The historical
[ESP results](../../docs/std-channel-performance.md) retain their original
configuration and evidence; this packaged entry is a new build cohort.

## Host controls

```sh
python -m unittest discover -s tests/mpsc-comparison -p 'test_*.py'
```

These run the actual host engine across all workload modes and reject corrupted
work, scheduling claims, incomplete ownership, changed receipts and unverified
sessions. Host output is explicitly labelled non-native and is not timing data
for either board.

The [MPSC CI workflow](../../.github/workflows/mpsc-comparison.yml) runs these
controls, builds the std/native NuttX pair on both board targets and checks their
kernel identities. Its artifacts contain build and inspection evidence, not
board execution. Target compilation, host controls and physical measurements
remain separate evidence levels.
