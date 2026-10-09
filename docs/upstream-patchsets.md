# Upstream patchsets and provenance

Dependency packages live in [`upstream/`](../upstream/README.md), separate
from nxrs platform integration and pinned source checkouts.

Nxrs pins the Apache NuttX source repositories at upstream commits, then
applies repository-owned patches **only to archived build copies**. The Git
submodules and installed Rust SDKs are not edited by a build. The patch
applicator checks each patch before use and writes the source revision, patch
SHA-256, and before/after file hashes to each build's `*-patches.json`.

## NuttX

`external/nuttx` is pinned to Apache NuttX
`2f3eb6d6774ab63b75788c27bde7644da48121b2`. The ordered series in
`upstream/nuttx/patches/` contains:

| Patch | Original fork commit | Purpose |
| --- | --- | --- |
| 0001 | `c74c0548a6c7` | ESP32-S3 BLE advertising |
| 0002 | `0c68623b0cfa` | ESP32-S3 NimBLE HCI option |
| 0003 | `89434949d511` | ESP32-S3 OV3660 camera driver |
| 0004 | `433092e620a9` | ESP32-S3 Wi-Fi HAL helpers |
| 0005 | `61e84c256590` | Zero-length UDP datagram readahead |
| 0006 | nxrs PR #2 | Flat-build image-wide pthread keys and deferred cleanup |
| 0007 | Local ESP32-S3 study | Return an error on a configured PSRAM-size mismatch |
| 0008 | Local ESP32-S3 study | Opt-in Freenove GPIO2 USERLED driver and board registration |

The fork's tracked empty `build.log` was excluded from patch 0002: it does
not change the build or source behavior.

`external/nuttx-apps` is pinned to Apache NuttX-apps
`85539a1223c4770ee36e68817f5bfe91e6b49369`. Its one patch in
`upstream/nuttx-apps/patches/` comes from fork commit `eaab369070bf` and adds
ESP32-S3 VHCI transport support for NimBLE. Fork commit `70d774868435` was
an empty CI trigger and needs no patch.

All build paths that archive these submodules invoke
`tools/apply-nuttx-patches.py` for both components. The host regression test
`python3 tests/nuttx-std/test-patches.py -v` covers both series, their
provenance, reapplication rejection, and incompatible-source rejection.

## Rust library and libc

There is no active Rust compiler fork or compiler source change in the firmware
build. The [Xtensa LLVM candidate series](../upstream/rust-llvm/README.md)
records a compiler-owned arithmetic fix with pinned input blobs and LLVM
regressions. The six-patch LLVM/Rust rebuild passes all 99 Xtensa backend tests;
the [shared local/CI builder](../upstream/rust-llvm/BUILDING.md) keeps clean
pinned checkouts read-only and produces a fresh compiler package. The
[measured report](../tests/event-services-comparison/ARITHMETIC.md) binds
compiler/driver, patch and firmware hashes; it shows removal of the tested
CPU overload in the historical five-patch cohort, not universal qualification.
The [sixth-patch cycle control](../tests/event-services-comparison/COMPILER_PROBE.md)
establishes arithmetic parity in the tested loop, not service deadline parity.
The series is not activated in normal firmware builds. Its assembly predecessor
and application feature flag have been removed. A qualified compiler build
must apply and record that dependency patchset, not modify application code.

The NuttX `std` qualification does adapt upstream Rust library sources in a
private, version-checked SDK copy. `tests/nuttx-std/prepare-std.py` generates
six actual unified-diff patch files per build:

- `std-parker.patch`: initialize the NuttX pthread parker mutex;
- `std-fd-sanitization.patch`: use the fcntl fallback instead of the incompatible NuttX pollfd layout;
- `std-sigign.patch`: use NuttX's SIG_IGN value at startup;
- `libc-sockaddr-storage.patch`: align vendored libc's NuttX sockaddr_storage;
- `std-libc-override.patch` and `std-libc-lock.patch`: select that verified private libc copy for rebuilt std.

The exact Rust source blobs and libc crate SHA-256 are pinned in the generator.
`std-patch.json` records the input and output blobs and the SHA-256 of every
generated patch. The patch files are retained in that build's output directory;
the committed, version-checked generator is the source of truth because the
nightly and Espressif SDKs have different original files and libc versions.
No installed SDK or upstream Rust source is changed.

The separate [std math RFC](../upstream/rust-std/README.md) routes NuttX's six
inverse-hyperbolic float methods to its libm. It is not silently included in
that qualification. Its archive applicator and optional compiler-builder input
verify pinned preimages, patch bytes and the full packaged std inventory.
The arithmetic reports carry its ledger separately from LLVM. This keeps a
library implementation change distinguishable from a compiler optimization.

The browser-thread probe has a separate opt-in Rust `std` TLS-selection patch:
`tests/browser-threads/prepare-std.py` generates `std-tls.patch` in a private
SDK copy. It likewise pins the original and patched source blobs in
`std-patch.json`.

These generated Rust patchsets are reproducible from their pinned inputs, but
are **not** checked-in static `.patch` files. Moving to a newer Rust SDK
requires explicit review of the pinned blobs, transforms, and resulting diffs.
