# Rust std channels versus Zephyr

The ESP32-S3 comparison pins NuttX at
`2b5c0de5e933281c209f954a63b99cf10c132346` and Zephyr 4.5.0 at
`41d10b59730406bbab4ca9f1fbbe9e96aafb4966`, its manifest-pinned modules and
SDK 1.0.1. Both kernels use the same pinned ESP 1.90 Rust compiler and measurement
engine. Zephyr calls native `k_msgq` from a core-only Rust fixture; this is not a
Zephyr std port.

Fair placement means equivalent executed hot operations reside in IRAM on both
kernels, rather than equal compiled byte counts. Apply the same
[fixture fragment](../tests/nuttx-std/channels/mpsc-fixture-iram.ld) to both:
workers, channel adapters, clock/gate wrappers and scheduled-release callbacks.
Audit linked targets and native queue, wait and scheduler callees, including
Xtensa literal-loaded calls. Memory-copy primitives use the same SoC ROM entries.
Setup, reporting, retirement diagnostics and joined reclamation keep normal
placement; native SDK IRAM requirements remain intact.

The final audit caught extra Zephyr queue wrappers still in flash, plus three
ordinary NuttX semaphore pending-signal helpers. The shared fixture fragment and
synchronization profile now cover those paths. Rust archives, workloads and OS
configurations are unchanged. NuttX relinks byte-identical native inputs; Zephyr
uses its normal native build to regenerate address-dependent ISR tables.
Earlier measurements remain controls; the results below use corrected placement.

Four runtime changes address the measured slowdown:

- Match hot code placement in internal instruction RAM. Flash-cache misses and
  alignment explained large earlier variations. Startup, peripheral setup and
  diagnostics retain normal SDK placement; native IRAM requirements remain intact.
- Select a Rust-owned NuttX bounded queue/waiter transaction and native
  single-permit semaphore parking through std's OS module wiring. Applications
  retain ordinary `std::sync::mpsc::sync_channel` APIs.
- Build the full voluntary-switch context directly on ESP32-S3 flat, single-core,
  windowed-ABI builds. Preserve register/window, TLS, loop and coprocessor state,
  scheduler commit and native restoration; other modes retain syscall switching.
- Keep unbounded and rendezvous operation boundaries out of line. Flavor dispatch
  otherwise copies their algorithms into bounded callers, enlarging hot code and
  stack frames. Channel algorithms and APIs remain unchanged.

Two other fixture corrections are separate from those runtime improvements. The
[entry-selection patch](../tests/nuttx-std/channels/mpsc-fixture-entry.patch)
selects one transport per image. Carrying an unused native-control workload in
the NuttX image inflated its memory cost; that diagnostic remains in a separate
retained image. The
[phase-ordering patch](../tests/nuttx-std/channels/mpsc-fixture-phase.patch)
moves endpoint priority/policy queries after the existing retirement barrier,
once timing and CPU counters have closed. Validation and joined cleanup remain
required. These test-only queries previously delayed delivery of producers' final
messages. The diagnostic-only control reduces the four-producer paced std maximum
from 25.1875 to 8.0 µs. The current matched-placement repeats give 8.0 µs
on all three std boots and 10.0625 µs on all three Zephyr boots.
The queue, SDK, arrival schedule and message timestamps did not change.

The current images passed 64 replayed fresh-boot captures covering all 16 matched
cases per kernel in a primary matrix and a separate hardware-counter matrix.
Six additional captures repeat the four-producer paced case three times per
kernel. Both kernels have identical complete source inventories within each
observer. Samples were not pooled.

| Primary observation | Rust std / NuttX | Zephyr |
| --- | ---: | ---: |
| Sustained messages/s, one producer | 323,246 | 265,050 |
| Sustained messages/s, four producers | 273,677 | 234,836 |
| 64-byte/four-producer delivery p99 | 84.500 µs | 85.3125 µs |
| IRAM text, including OS and fixture | 61,492 bytes | 53,392 bytes |

Delivery median and p99 are never higher with std across matched primary
cases, including successful/rejected admission and release-lateness p99.
Scheduled-release p99 and exploratory threshold miss counts are also never
higher; both have zero 100 µs misses in the 64-byte/four-producer case. Tail
curves cross: single-producer/16-byte stream p99.9 is 34.0 versus 33.3125 µs,
and maximum is 34.4375 versus 33.9375 µs. The 64-byte stream p99.9 is 60.0625
versus 59.125 µs. These wall delays remain in the results; no quantile-universal
claim or latency subtraction is made. Normal cases have zero loss; payload,
per-producer FIFO and joined reclamation pass. Overload admits the same message
identities, and every admitted message is delivered.

Rust/std uses 3.8–18.0% less whole-core active CPU per delivered message. Every
std upper observation bound is below the corresponding Zephyr lower bound.
These counters include IRQ, scheduler and idle-loop work; native thread counters
have incompatible attribution and must not be compared directly.

Outlining alternate flavors saves 6,304 IRAM text bytes and 6,144 bytes of shared
SRAM reservation, without added thread storage or live heap use. Selecting only
the measured transport removes another 7,332 IRAM bytes from the mixed fixture.
Removing extra startup/peripheral/diagnostic promotions saves 4,760 text bytes
and 4,864 bytes of shared SRAM reservation in a separate relink control with all
74 frozen build inputs unchanged. The endpoint correction also changes code
generation; its smaller image is not another queue optimization.

The IRAM text difference is 8,100 bytes: native runtime/fixture code is 1,723
bytes smaller with NuttX, Rust synchronization/runtime adds 5,245, workload/adapter
code adds 4,455, and alignment adds 123. Map-origin categories include inlined
channel code; these are full-image costs, not isolated queue storage. Including
vectors, padding and Zephyr's separate loader, runtime spans are 62,720 versus
55,248 bytes: a 7,472-byte difference. Equivalent hot placement is the acceptance
criterion; identical OS image sizes are not required.

Whole-image RAM also includes different runtime provisioning. Accounting for
IRAM/DRAM aliasing once, linked reservations plus captured live heap are:

| Stream case, capacity eight | Rust std / NuttX | Zephyr |
| --- | ---: | ---: |
| One producer, 16-byte payload | 244,584 bytes | 259,432 bytes |
| Four producers, 64-byte payload | 295,432 bytes | 259,432 bytes |

These exclude free heap and cache/ROM/RTC reservations, and are live occupancy,
not qualified startup peaks. Zephyr always reserves five worker stacks and a
4,096-byte queue buffer. NuttX allocates for the selected case. For the four-
producer/64-byte case, NuttX's queue-plus-worker live allocation is 84,728 bytes
above baseline; capacity-normalized Zephyr objects total 83,216 bytes. That
1,512-byte difference includes thread/runtime metadata; the normalized number
is a derived object sum, not a measured smaller Zephyr image. Increasing queue
capacity or payload has exactly the payload-storage cost in the captured std
cases. The larger whole-image difference is predominantly fixed image/runtime
cost, not per-message storage. Requested controller/worker stacks are 32/16 KiB;
observed workload highwaters fit both implementations' usable stacks.

Zephyr's zero heap records are an unsupported reader, not evidence of zero heap
cost: its linked system-heap buffer/control reserve 4,180/24 bytes. The pinned
NuttX high-water counter counts its 368-byte allocator context twice, confirmed
by source, compiled DWARF size and the captures. Raw records remain unchanged;
that counter is not used as a qualified peak. SoC caches match: 16/32 KiB I/D
cache, eight ways and 32-byte hardware lines. The four-producer/64-byte std
case retains 142,376 bytes of free heap; the additional IRAM does not exhaust
shared SRAM in this fixture. This is not a camera-product memory qualification.

The `esp32s3-sync-iram` product profile selects synchronization placement through
`iram-layout`. The frontend stages the platform-owned fragment and records its
source and staged hashes. Applications keep ordinary std APIs; optional channel
flavors remain compiled, with their out-of-line helpers in flash. The comparison
additionally places workload functions in IRAM; that fixture placement is not
part of the product profile. An earlier normal frontend build checks unchanged
wiring and target ABI. The layout-only change passes frozen-input native
relinking and the focused staging test. This is not an ordinary-app physical
performance result. `ESP32S3_RUN_IRAM` instead loads the entire image.

Validation retains native state/preemption/signal/TLS tests, ordinary Rust
thread/channel checks, seven unchanged P1 primitive cases, and the
[host ownership controls](../tests/nuttx-std/channels/README.md). P1 does not
qualify an arbitrary-priority three-class transport. ARM32 compilation and both
SDK host suites are separate from ESP32-S3 evidence. The semaphore ABI gate
checks storage/alignment and the header-owned protocol value.

Measurements retain fixture `a8a1b1baa05aeca76264e882ed31c3b2baa9973d`, pinned
Zephyr/CPU-observer overlays and the entry/phase patches. Exact source snapshots,
compiler, ELF, image and placement receipts are retained privately with raw
sessions and backups. Every flash campaign restored
and independently verified the original image. Campaigns reverified an existing
full backup after fresh-read transport failures; no successful new full read is
claimed. Private digest-image preparation does not fix normal frontend packaging.

Matched hot placement is not an ordinary-placement or cross-build claim. Native
GCC 14.2/14.3 differ, clocks lack external calibration, and three boots establish
descriptive repeatability rather than statistical confidence. Full matrices have
one paired pass per observer. The Zephyr upgrade itself did not improve the
matched fixture; its compiler changed too. `physical_timing_qualified` and
`production_qualified` remain false: these are nominal-clock physical comparisons,
not calibrated product-deadline or general SMP qualification.
