# Rust std channels versus Zephyr

The ESP32-S3 comparison uses Zephyr 4.5.0 at
`41d10b59730406bbab4ca9f1fbbe9e96aafb4966`, its manifest-pinned modules and
SDK 1.0.1. Rust uses the same pinned ESP 1.90 compiler and workload on both
kernels. Zephyr calls native `k_msgq` from a core-only Rust test fixture;
this is not a Zephyr std port.

Four changes address the measured slowdown:

- Match hot code placement in internal instruction RAM. Flash-cache misses and
  alignment explained large earlier variations.
- Select a Rust-owned NuttX bounded queue/waiter transaction and native
  single-permit semaphore parking through std's OS module wiring. Applications
  retain the ordinary `std::sync::mpsc::sync_channel` API.
- Build the full voluntary-switch context directly on ESP32-S3 flat, single-core,
  windowed-ABI builds. Preserve register/window, TLS, loop and coprocessor state,
  scheduler commit and native restoration; other targets/modes retain syscall
  switching.
- Keep unbounded and rendezvous operation boundaries out of line. Otherwise,
  flavor dispatch copies their algorithms into bounded callers, increasing hot
  code size and stack frames. The channel algorithms and APIs remain unchanged.

The final single-transport images passed one paired primary matrix and one
separate hardware-counter matrix: 64 replayed fresh-boot captures covering all
16 matched cases per kernel. Nine additional captures compare three fresh boots
each of the mixed NuttX image, std-only NuttX image and Zephyr. Samples were not pooled.
In matched hot placement, sustained throughput is 321k versus 264k messages/s with one producer,
and 272k versus 234k with four. Rust/std uses 3–18% less whole-core active CPU.
The same hardware counters include IRQ and scheduler work; native thread counters
have incompatible attribution and must not be compared directly.

Native switching reduced the 64-byte/four-producer p99 from 86.250 to
84.875 µs. Keeping alternate flavors out of line reduces it to 84.6875 µs,
matching Zephyr. The std-only fixture measures 84.625 µs with the same SDK;
that additional timer tick accompanies a different fixture image, not another
queue algorithm change. Other normal p99 cases are lower with std. Exploratory threshold
miss counts are never higher; both have zero 100 µs misses in the focal case.
Some smaller-message and paced maximum samples remain higher with std; this is
not dominance at every quantile. Normal cases have zero loss and preserve
payload, per-producer FIFO and joined reclamation. Overload admits the same
message identities, and all admitted messages are delivered.

The std-only image uses 65,644 IRAM text bytes versus Zephyr's 52,684.
The mixed image uses 72,976 bytes. Outlining alternate
flavors saves 6,304 text bytes and 6,144 bytes of shared SRAM reservation, with
no added thread storage or live heap use. Total savings from the original hot
placement are 34,712 text bytes and 34,560 bytes of shared SRAM reservation.
These include OS, runtime and fixture code, not isolated queue storage. The
mixed image retains an additional native-control workload and native queue code;
selecting only the measured transport removes 7,332 more IRAM bytes. This is a
fixture correction, separate from the SDK optimization.

The remaining 12,960-byte difference comprises 2,903 bytes of native runtime,
5,245 bytes of Rust synchronization/runtime, 4,627 bytes of workload/adapter
difference and 185 bytes of alignment. The
[entry-selection patch](../tests/nuttx-std/channels/mpsc-fixture-entry.patch)
selects the existing transport before execution. The timed engine, arrival
schedules, metrics and native adapters are unchanged; the native diagnostic
remains available in its retained separate image.
Matched placement is not an ordinary-placement, cross-build or resource-parity
claim. Native GCC14.2/14.3
differ, clocks lack external calibration, and three boots establish descriptive
repeatability rather than statistical confidence; the final full matrices have
one paired pass per observer. The upgrade itself did not improve this matched
Zephyr fixture; its compiler changed too.

The `esp32s3-sync-iram` product profile selects native synchronization and
bounded Rust channel placement through `iram-layout`. The frontend stages its
platform-owned linker fragment and records both source and staged hashes. Applications keep ordinary
std APIs. Rendezvous/unbounded APIs remain compiled; their out-of-line helpers
stay in flash. The comparison additionally places its workload functions in IRAM;
that fixture-specific placement is not part of the product profile. A normal
frontend build checks the profile and target ABI; it is not an ordinary-app
physical performance result. `ESP32S3_RUN_IRAM` instead loads the entire image.

Validation includes native state/preemption/signal/TLS tests, ordinary Rust
thread/channel checks, seven unchanged P1 primitive cases, and the
[host ownership controls](../tests/nuttx-std/channels/README.md). P1 observations
do not qualify an arbitrary-priority three-class transport. ARM32 compilation
and both SDK host suites are separate from ESP32-S3 hardware evidence. The
semaphore ABI gate checks storage/alignment and the header-owned protocol value.

Measurements use the frozen comparison fixture at
`a8a1b1baa05aeca76264e882ed31c3b2baa9973d` plus the entry-selection patch, with retained source, compiler, ELF,
image and placement receipts. Raw device/session identifiers, consoles, SDKs and
flash backups remain private. Every flash campaign restored and verified the
original image. The P1 campaign reverified an existing full backup after fresh
read transport failures; no successful new full read is claimed. Its clean
digest test-image preparation does not fix normal frontend digest packaging.

The 12,960-byte full-image IRAM gap and higher extreme samples remain open.
The comparison's `physical_timing_qualified` and `production_qualified` flags remain false;
these are nominal-clock physical observations, not calibrated product-deadline
qualification. They do not establish universal parity or general SMP qualification.
