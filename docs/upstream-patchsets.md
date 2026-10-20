# Upstream patchsets and provenance

Dependency patches are reproducible build inputs, separate from nxrs platform
code and pinned source checkouts. Build tools modify private archived copies,
not Git submodules or installed SDKs. The applicator verifies each source
revision and patch preimage, then records patch hashes and before/after file
hashes in a build ledger. Preserve that ledger with firmware.

## NuttX

`external/nuttx` is pinned to NuttX 13.1.0
(`2b5c0de5e933281c209f954a63b99cf10c132346`).
The authoritative ordered lists for NuttX and NuttX Apps are in
[`tools/apply-nuttx-patches.py`](../tools/apply-nuttx-patches.py).
Patch headers retain their original source and rationale; build ledgers retain
the exact applied bytes. IDs intentionally have gaps: accepted upstream changes
and retired investigation tools are not silently renumbered.

| Area | Retained patch IDs | Purpose |
| --- | --- | --- |
| Original platform support | 0001–0004, 0007–0008 | ESP32-S3 BLE, Wi-Fi, camera, PSRAM validation and Freenove LED |
| POSIX/network integration | 0005–0006, 0022, 0042, 0077–0078 | UDP empty datagrams, flat-build pthread cleanup, TCP connect/retransmission progress, safe dispatch after link-layer packet consumption, and sequence validation before ACK/close progress |
| Pico board and radio assets | 0009, 0011–0012, 0021, 0028 | Pico 2 W wiring, firmware validation, CLM alignment, calibration and open-network setup |
| Pico shared Wi-Fi/Bluetooth transport | 0013–0015, 0027, 0029, 0036–0038, 0040–0041 | HCI rings and power ownership; bounded frames, backpressure, receive validation and error propagation |
| RP2350 transfer correctness | 0044–0045, 0047–0049 | PIO receive counts/request mapping, DMA acknowledgement/lifetime and invalid FIFO-read detection |
| Bluetooth command and connection ownership | 0017, 0035, 0039, 0051, 0065 | HCI waiter lifetime, quiescent callbacks, ACL credits, disconnect reason and retryable scan cleanup |
| Bluetooth interoperability | 0019–0020, 0023, 0026, 0030 | Host receive capacity, MTU negotiation, accurate advertising state/errors and normal-send log level |
| Portable GATT client | 0059–0063 | Pending-request completion, discovery bounds/status, negotiated MTU and correct short UUID expansion |
| ESP Bluetooth allocation | 0018, 0052 | Pair kernel allocations/deallocations in both single-heap and separate-heap builds |
| Development USB recovery | 0016, 0064 | Opt-in 1200-baud ROM entry with an optional picoboot-only interface |
| Standard camera capture | 0067–0071 | OV3660 sensor and Freenove registration; exact clock, bounded DMA/cache ownership, and capture lifecycle error propagation |
| JPEG capture | 0073–0075, 0079–0085 | Sensor-before-DMA sequencing, bounded JPEG/queue ownership, continuous capture synchronization, configurable VGA capacity, bounded cache work, standard quality control and initialized scalar-control wrappers |
| Xtensa Espressif interrupt ownership | 0072 | Release NuttX-owned callback adapters after successful interrupt detach; retain them on failure for retry |
| ESP32-S3 performance accounting | 0076 | Initialize hardware-counter conversion with the actual configured CPU frequency before scheduler accounting |

NuttX Apps retains three patches: the original ESP NimBLE transport, NSH
`dmesg` reset options, and correct DHCP pad-option parsing. The tracked empty
fork `build.log` is not part of a patch.

### Pico integration and limits

NuttX 13.1.0 already includes the common CYW43439 transport and firmware helper;
the old 0010 backport was retired. Patch 0011 supplies the Pico 2 W board
adaptation and rejects missing firmware instead of using a dummy image. The
pending board port is not claimed as accepted upstream.

The normal builder calls `tools/prepare-pico2w-radio.py` to stage hash-pinned
Pico SDK 2.3.1 radio assets on demand. It retains `LICENSE.RP` and records
source and binary hashes in `radio-firmware.json`. Bluetooth profiles use
`wb43439` WLAN firmware and the separate controller patch; Wi-Fi-only profiles
use `w43439`. No full Pico SDK checkout or runtime is required, and firmware
binaries are not committed to nxrs.

Patch 0014 is an experimental Bluetooth transport, not an upstream-accepted
implementation. It shares the existing radio daemon and backplane lock rather
than adding a second bus owner. Native tests cover ring bounds, backpressure,
ownership and shutdown; these do not prove radio reliability or power behavior.

Pico delivery failures also reproduced with pristine Pico SDK C code, outside
nxrs and NuttX. Further platform/network RCA is deferred. See the concise
[qualification results and known issues](../tests/nuttx-std/radio/README.md#known-issues)
for the observed limit and the remaining BLE/TCP caveats. The retained patches
fix specific integration or correctness defects, not the unexplained radio
loss. Default-off packet/SPI observers, fault readbacks and investigation-only
timing APIs have been removed.

### Validation and recovery

`python3 tests/nuttx-std/test-patches.py` applies the series to isolated source
fixtures and compiles the affected functions into native regression tests.
Where practical, a baseline witness reproduces the defect and sanitizers check
the fix, including HCI/ATT callback lifetime, DMA lifetime, credits and scan
cleanup. Host fixtures are not substitutes for device qualification. Preserve
the build ledger and use the [radio fixture](../tests/nuttx-std/radio/README.md)
for device checks; a failed case is not converted into a pass by retrying.

The 1200-baud recovery hook is development-only, enabled by the Pico radio
profiles. It requires working USB and timer interrupts and is not a hardware
watchdog. The picoboot-only option avoids mounting a mass-storage disk during
software recovery; manual BOOTSEL/UF2 entry remains available. Anyone with USB
access can request this reset, so production images should leave it disabled
unless that maintenance access is intended. See
[USB recovery](../tests/nuttx-std/wifi/README.md#usb-recovery).

## Espressif SDK compatibility

ESP32-S3 builds also patch their isolated `esp-hal-3rdparty` copy after NuttX
context setup, before compilation. The source cache is never patched. NuttX
13.1.0 selects `b90b1837cb5ad24747deb4c895246037cc206ce5`; the build records
the actual revision, patch hash and source pre/postimages in `esp-hal-patches.json`.

| Patch | Purpose |
| --- | --- |
| [0001](../upstream/esp-hal-3rdparty/patches/0001-mbedtls-pair-configured-allocators.patch) | Pair mbedTLS's fallback libc `calloc` with `free`; keep capability allocations paired with `heap_caps_free` and preserve memory placement |
| [0002](../upstream/esp-hal-3rdparty/patches/0002-intr-alloc-pair-capability-allocators.patch) | Pair interrupt handles, shared vectors and trace arguments with `heap_caps_free`, including error paths; avoid cross-heap teardown in split-heap builds |

The mbedTLS pairing fixed by 0001 triggered the NuttX adapter's cross-heap warning and
fallback on ESP SoftAP startup. The resulting log flood interrupted shell
completion markers. This correction is not a Pico Bluetooth timeout fix.
With the same AP configuration, captured warnings fell from 437 to zero and
the strict console completion check passed. Four subsequent UDP/TCP C/Rust
coexistence cases completed 4,000 exact echoes and 400 GATT pairs in total.
Host tests compile the applied source across allocation modes, thresholds,
failure/NULL cases and custom-allocator compile-out without downloading the SDK.

Patch 0002 fixes the opposite mismatch: capability-allocated interrupt handles
were passed to libc `free`, asserting during camera close with a PSRAM user
heap. NuttX patch 0072 separately releases the caller-owned callback adapter;
freeing the SDK handle does not free that argument. Its native regression
exercises repeated setup/teardown and failed-detach retry with sanitizers.

## Rust, LLVM and std

Normal firmware builds do not use an active Rust compiler fork. The opt-in
[Xtensa LLVM series](../upstream/rust-llvm/README.md) pins compiler and Rust
revisions, patch bytes and affected-source preimages. Its six-patch default
and named compiler candidates are built in a private evaluation sysroot by
the [local builder](../upstream/rust-llvm/BUILDING.md). The
[arithmetic assessment](../tests/arithmetic-parity/RESULTS.md) retains the
matched device measurements and distinguishes the frozen 29-patch candidate
from the unchanged six-patch default. Compiler evidence binds the actual
compiler and driver hashes; source patches alone do not prove which compiler
built an image.

`tests/nuttx-std/prepare-std.py` generates seven unified-diff patches against
version-checked Rust library and libc inputs: NuttX parker initialization,
file-descriptor sanitization, startup `SIG_IGN`, `sockaddr_storage` alignment,
NuttX 13.1 open flags, and the private libc override/lock. NuttX 13.1 changed
the access modes and flag bit positions; both pinned libc versions predate
this change. The ABI gate checks all 17 exported open flags against the actual
target headers, so an old shared SDK is rejected rather than silently reused.
Two committed patches are also applied: `upstream/libc/patches/0001-nuttx-pollfd-abi.patch`
defines NuttX's complete pollfd ABI, and
`upstream/rust-std/patches/0001-native-pollfd-initialization.patch` zero-initializes
all native pollfd fields in std. Each build records nine patch entries total.
The 77-observation gate compares the full native pollfd layout, field offsets
and widths, mask types and constants against the exact Cargo libc artifact;
Rust polling is admitted only on an exact match. `ppoll` remains rejected
because its signal ABI is unqualified. `std-patch.json` records source blobs
and patch hashes; generated patch files are retained with the build output.
The generator is authoritative because SDK versions have different preimages.
No upstream checkout or installed SDK is changed.
Local provenance marks tracked, uncommitted nxrs changes with `nxrs_dirty=yes`;
the recorded commit alone is not a reproducible source identity in that case.

The separate [std math RFC](../upstream/rust-std/README.md) routes NuttX
inverse-hyperbolic methods to C libm. It is not part of the normal std
qualification: its own manifest, patch ledger and complete packaged source
inventory are verified and recorded independently from LLVM.

Browser-thread qualification also has a separate opt-in std TLS-selection
patch generated by `tests/browser-threads/prepare-std.py`; its input/output
blobs are recorded in `std-patch.json`. This browser-specific diff is generated
from pinned inputs. SDK upgrades
require review of pins, transformations and generated diffs.
