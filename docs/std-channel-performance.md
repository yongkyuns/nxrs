# Rust std channels versus Zephyr

The ESP32-S3 comparison uses Zephyr 4.5.0 at
`41d10b59730406bbab4ca9f1fbbe9e96aafb4966`, its manifest-pinned modules and
SDK 1.0.1. Both kernels use the same pinned ESP 1.90 Rust compiler and measurement
engine. Zephyr calls native `k_msgq` from a core-only Rust fixture; this is not a
Zephyr std port.

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

Two fixture corrections are separate from those runtime improvements. The
[entry-selection patch](../tests/nuttx-std/channels/mpsc-fixture-entry.patch)
selects one transport per image. Carrying an unused native-control workload in
the NuttX image inflated its memory cost; that diagnostic remains in a separate
retained image. The
[phase-ordering patch](../tests/nuttx-std/channels/mpsc-fixture-phase.patch)
moves endpoint priority/policy queries after the existing retirement barrier,
once timing and CPU counters have closed. Validation and joined cleanup remain
required. These test-only queries previously delayed delivery of producers' final
messages. On three corrected fresh boots, the four-producer paced maximum is
8.0 µs with std, down from 25.1875 µs; Zephyr measures 10.0625–10.125 µs.
The queue, SDK, arrival schedule and message timestamps did not change.

The current images passed 64 replayed fresh-boot captures covering all 16 matched
cases per kernel in a primary matrix and a separate hardware-counter matrix.
Six additional captures repeat the four-producer paced case three times per
kernel. Both kernels have identical complete source inventories within each
observer. Samples were not pooled.

| Primary observation | Rust std / NuttX | Zephyr |
| --- | ---: | ---: |
| Sustained messages/s, one producer | 322,970 | 263,874 |
| Sustained messages/s, four producers | 273,376 | 233,910 |
| 64-byte/four-producer delivery p99 | 84.500 µs | 85.4375 µs |
| IRAM text, including OS and fixture | 60,576 bytes | 52,552 bytes |

Delivery median, p99 and maximum are never higher with std across the matched
primary cases. Scheduled-release p99 and exploratory threshold miss counts are
also never higher; both have zero 100 µs misses in the 64-byte/four-producer case.
The curves still cross: single-producer backpressure p99.9 is 34.1875 versus
32.5 µs. This is not dominance at every quantile. Normal cases have zero loss;
payload, per-producer FIFO and joined reclamation pass. Overload admits the same
message identities, and every admitted message is delivered.

Rust/std uses 3.9–18.3% less whole-core active CPU per delivered message. Every
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

The remaining IRAM text gap is 8,024 bytes: native runtime is 1,797 bytes smaller,
Rust synchronization/runtime adds 5,245, workload/adapter code adds 4,451, and
alignment adds 125. Map-origin categories include inlined channel code; these
are full-image costs, not isolated queue storage. The runtime IRAM span from vectors through the end marker is 61,696 versus
54,408 bytes, including padding and Zephyr's intervening loader text. Heap and
thread storage are separate budgets. Full resource parity remains open.

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
