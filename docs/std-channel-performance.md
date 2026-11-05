# Rust std channels versus Zephyr

The ESP32-S3 comparison uses Zephyr 4.5.0 at
`41d10b59730406bbab4ca9f1fbbe9e96aafb4966`, its manifest-pinned modules and
SDK 1.0.1. Rust uses the same pinned ESP 1.90 compiler and workload on both
kernels. Zephyr calls native `k_msgq` from a core-only Rust test fixture;
this is not a Zephyr std port.

Three changes address the measured slowdown:

- Match hot code placement in internal instruction RAM. Flash-cache misses and
  alignment explained large earlier variations.
- Select a Rust-owned NuttX bounded queue/waiter transaction and native
  single-permit semaphore parking through std's OS module wiring. Applications
  retain the ordinary `std::sync::mpsc::sync_channel` API.
- Build the full voluntary-switch context directly on ESP32-S3 flat, single-core,
  windowed-ABI builds. Preserve register/window, TLS, loop and coprocessor state,
  scheduler commit and native restoration; other targets/modes retain syscall
  switching.

Three paired primary passes and three separate hardware-counter passes produced
198 replayed fresh-boot captures. Samples were not pooled. In matched hot
placement, sustained throughput is 321k versus 264k messages/s with one producer,
and 272k versus 234k with four. Rust/std uses 3–18% less whole-core active CPU.
The same hardware counters include IRQ and scheduler work; native thread counters
have incompatible attribution and must not be compared directly.

The native switch removes 88% of the final 64-byte/four-producer p99 gap:
86.250 → 84.875 µs, versus Zephyr's 84.6875 µs. The remaining 0.22% deficit is
repeatable. Both have zero exploratory 100 µs misses in that case. Other normal
p99 cases are lower with std; normal cases have zero loss and preserve payload,
per-producer FIFO and joined reclamation.

Complete-image IRAM text is 107,688 bytes versus 52,684; these include OS, runtime
and fixture code, not isolated queue storage. Matched placement is not an
ordinary-placement, cross-build or resource-parity claim. Native GCC14.2/14.3
differ, clocks lack external calibration, and three boots establish descriptive
repeatability rather than statistical confidence. The upgrade itself did not
improve this matched Zephyr fixture; its compiler changed too.

Validation includes native state/preemption/signal/TLS tests, ordinary Rust
thread/channel checks, seven unchanged P1 primitive cases, and the
[host ownership controls](../tests/nuttx-std/channels/README.md). P1 observations
do not qualify an arbitrary-priority three-class transport. ARM32 compilation
and both SDK host suites are separate from ESP32-S3 hardware evidence. The
semaphore ABI gate checks storage/alignment and the header-owned protocol value.

Measurements use the frozen comparison fixture at
`a8a1b1baa05aeca76264e882ed31c3b2baa9973d` with retained source, compiler, ELF,
image and placement receipts. Raw device/session identifiers, consoles, SDKs and
flash backups remain private. Every flash campaign restored and verified the
original image. The P1 campaign reverified an existing full backup after fresh
read transport failures; no successful new full read is claimed. Its clean
digest test-image preparation does not fix normal frontend digest packaging.

The residual tail difference and production placement qualification remain open;
these results do not establish universal parity or general SMP qualification.
