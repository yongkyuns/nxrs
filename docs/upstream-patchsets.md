# Upstream patchsets and provenance

Dependency patches are reproducible build inputs, separate from nxrs platform
code and pinned source checkouts. Build tools modify private archived copies,
not Git submodules or installed SDKs. The applicator verifies each source
revision and patch preimage, then records patch hashes and before/after file
hashes in a build ledger. Preserve that ledger with firmware.

## NuttX

`external/nuttx` is pinned to stable [NuttX 13.1.0](https://nuttx.apache.org/download/)
(`nuttx-13.1.0`, released 2026-10-04),
`2b5c0de5e933281c209f954a63b99cf10c132346`; its ordered patch series
covers the following fork and local changes:

| Patch | Source | Purpose |
| --- | --- | --- |
| 0001 | `c74c0548a6c7` | ESP32-S3 BLE advertising |
| 0002 | `0c68623b0cfa` | NimBLE HCI option |
| 0003 | `89434949d511` | OV3660 camera driver |
| 0004 | `433092e620a9` | Wi-Fi HAL helpers |
| 0005 | `61e84c256590` | Zero-length UDP readahead |
| 0006 | nxrs PR #2 | Flat-build pthread keys and deferred cleanup |
| 0007 | Local study | PSRAM-size mismatch error |
| 0008 | Local study | Freenove GPIO2 USERLED |
| 0009 | Radio bring-up | BCMF explicit open-network key reset |
| 0011 | Board PR #19250 | Pico 2 W wiring, firmware staging, strict firmware validation and GPIO25 LED exclusion |
| 0012 | Local qualification | CYW setup errno sign |
| 0013 | Radio bring-up | Consume header-only gSPI credit updates and release receive buffers on all non-data paths |
| 0014 | Experimental radio bring-up; Pico SDK 2.1.1 protocol reference | CYW43439 Bluetooth firmware upload, shared-backplane HCI rings and power lease |
| 0015 | Radio bring-up | Per-device requested IRQ state, separate from temporary SPI transfer masking |
| 0016 | Development recovery | Opt-in USB 1200-baud bootloader trigger and interrupt-safe RP2350 ROM entry |
| 0017 | BLE qualification | Retain synchronous HCI command ownership; detach timed-out waiters and serialize buffer references |

The tracked empty fork `build.log` is intentionally excluded from patch 0002.
Patch 0010 was retired: 13.1.0 includes the accepted transport
(`bf6de2df0c52`) and common firmware helper (`7e074b23c9ef`). Patch 0011 retains
only the board adaptation and rejects missing firmware rather than silently
using a dummy image; the pending Pico board port is not claimed as merged.
The RP23xx board adaptations remain inert without the CYW43439
configuration; the BCMF corrections apply to the shared driver. The errno
correction in 0012 remains suitable for independent upstream submission; its
obsolete debug-header adaptation was removed. Pico radio builds stage
hash-pinned firmware with `tools/prepare-pico2w-radio.py`, retain `LICENSE.RP`
in the cache, and record its source/binary hashes in `radio-firmware.json`.
The firmware is not checked into nxrs. The optional Bluetooth profile stages
the separately pinned 6,164-byte controller patch image; it links no Pico SDK
runtime. Patch 0014 is experimental, not an upstream-accepted or fully hardware-
qualified Bluetooth implementation. It reuses the radio daemon, protects the
shared backplane window, and keeps the chip powered while HCI is open. Host
tests cover firmware bounds, ring wrapping, backpressure, receive retries,
ambiguous-commit faults, concurrent senders and serialized idle shutdown.
Patch 0015 fixes a reproduced IRQ state-machine bug; its baseline witness and
patched implementation are both exercised. See the radio fixtures for actual
device results; host tests alone do not qualify coexistence or power consumption.

Patch 0017 fixes a generic NuttX host bug reproduced during repeated Pico BLE
scans: command completion released the command before its waiting caller read
the response pointer. Pool reuse produced a false `EIO` despite controller
success. A separate caller reference removes that race; serialized waiter
detachment prevents late completion from posting to an expired stack semaphore.
Timeout does not cancel an already queued controller command. The host witness
compiles the actual command and buffer functions, forces the baseline failure,
and checks cleanup and concurrent references under sanitizers. No HAL retries
or Pico-specific workarounds are added.

`external/nuttx-apps` is pinned to the matching **13.1.0** release,
`3c772ea35b9245972e18999828f7b533e6fc57cc`. Its one patch adds ESP32-S3 VHCI
transport support for NimBLE and comes from fork commit `eaab369070bf`. Both series are applied by
`tools/apply-nuttx-patches.py`. Run
`python3 tests/nuttx-std/test-patches.py -v` for source mismatch,
reapplication and provenance regression coverage. Radio witnesses compile the
patched C implementations with host Clang/GCC; the HCI parser/ring test also
requires AddressSanitizer and UndefinedBehaviorSanitizer support and treats
their findings as failures. No board is accessed by these host tests.

13.1.0 also includes the upstream RP2350 USB endpoint re-arm and memory-ordering
fixes (`d89019aa78f4`, `a85b28bc2cb6`); no local USB backport is needed. The
USB witness compiles actual completion/control functions with mocked hardware
boundaries; it checks callback re-arm, continuation/ZLP and write ordering,
not controller timing or USB interoperability.
Builds select the Git index's dependency pins, so staged upgrades can be tested
before committing; use `git add external/nuttx external/nuttx-apps` after updating
both clean submodules. Patch ledgers and ABI checks identify the actual build.
The shared version helper restores the selected pin's release identity in
archived builds; `uname` reports the release and `<revision>-nxrs`, not `0.0.0`
or an unmodified upstream build. Update `upstream/nuttx/release.json` with the
gitlink on future upgrades; a mismatched label/pin is rejected even in shallow CI
checkouts without tags.
Older device and RTOS-comparison results remain tied to their original sources;
they are not relabeled as 13.1.0 qualification.

Patch 0016 is default-off and enabled only in the Pico radio development
profiles. A real USB line-coding request at 1200 baud schedules ROM bootloader
entry after 100 ms, without NSH, a worker queue or a Pico SDK runtime. A valid
request is committed: changing baud or closing the port does not revoke it.
Short payloads, incorrect lengths and wrong-interface requests cannot trigger
it, and driver unbind/release cancels embedded timers. Anyone with USB access
can request this reset, so production images
should leave it disabled unless unauthenticated maintenance access is intended.
It needs working USB and timer interrupts; it is **not** a hang-detecting
hardware watchdog. Usage is in the [Pico Wi-Fi fixture](../tests/nuttx-std/wifi/README.md#usb-recovery).

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
`std-patch.json` records source blobs and
patch hashes; generated patch files are retained with the build output. The
generator is authoritative because SDK versions have different preimages.
No upstream checkout or installed SDK is changed.
Local provenance marks tracked, uncommitted nxrs changes with `nxrs_dirty=yes`;
the recorded commit alone is not a reproducible source identity in that case.

The separate [std math RFC](../upstream/rust-std/README.md) routes NuttX
inverse-hyperbolic methods to C libm. It is not part of the normal std
qualification: its own manifest, patch ledger and complete packaged source
inventory are verified and recorded independently from LLVM.

Browser-thread qualification also has a separate opt-in std TLS-selection
patch generated by `tests/browser-threads/prepare-std.py`; its input/output
blobs are recorded in `std-patch.json`. These Rust adaptations are generated
from pinned inputs rather than committed as static patches. SDK upgrades
require review of pins, transformations and generated diffs.
