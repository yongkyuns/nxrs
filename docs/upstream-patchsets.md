# Upstream patchsets and provenance

Dependency patches are reproducible build inputs, separate from nxrs platform
code and pinned source checkouts. Build tools modify private archived copies,
not Git submodules or installed SDKs. The applicator verifies each source
revision and patch preimage, then records patch hashes and before/after file
hashes in a build ledger. Preserve that ledger with firmware.

## Maintaining a small downstream delta

Prefer supported upstream configuration, board directories and callbacks when
they express the required integration. Keep project-owned board code and product
identifiers in their owning source directories. A dependency patch is appropriate
when the selected upstream source cannot satisfy a required contract through
those interfaces.

Retained changes need a specific requirement, the regression or source evidence
that establishes it, their upstream status, and a removal condition. Record these
beside the patch or in this guide. Keep fixes to ABI, ownership and protocol
correctness separate from performance experiments so each can be retired on its
own evidence. An accepted upstream change becomes removable after the selected
compatible revision contains it and the regression passes without the backport.

The source ledger proves which changes were applied. Behavioral qualification
establishes whether they satisfy the product's contract. For performance changes,
compare one change at a time using the same workload, priorities, toolchain and
memory placement. Keep historical measurements tied to their original sources;
they do not qualify a newly reduced configuration.

The unused private ESP32-S3 camera driver and NimBLE transport integration have
been retired. Maintained camera profiles use NuttX's standard video interface and
OV3660 sensor lower half; maintained Bluetooth profiles use the native NuttX host.
Patch 0068 retains the standard-camera registration with its context rebased after
the old camera code was removed. The old NimBLE patch's initialization debug
message is also removed. Existing native capture and Bluetooth ownership fixes
remain in the ordered series.

## NuttX

`external/nuttx` is pinned to NuttX 13.1.1
(`4268617f28ad0ed7e900a081ea1b33c9347458b3`). The
[published release](https://nuttx.apache.org/download/) uses the approved
`nuttx-13.1.1-RC0` source tag. The released OS and Apps archives were checked
against their published SHA-512 digests and these commits: all tracked regular
files present in the archives match, with only the generated OS `.version`
file added by release packaging.
`external/nuttx-apps` remains at `3c772ea35b9245972e18999828f7b533e6fc57cc`,
which is also the Apps revision for that release.
The authoritative ordered lists for NuttX and NuttX Apps are in
[`upstream/build-patches.json`](../upstream/build-patches.json), consumed by
[`tools/apply-nuttx-patches.py`](../tools/apply-nuttx-patches.py).
Kernel patch 0102 has one preimage indentation adjustment for upstream's V4L2 formatting fix;
its camera stop-acknowledgement behavior is unchanged. Patch 0146 uses GCC 16's
explicit `wchar_t` minimum so the Xtensa limits match its unsigned native type.
Patch headers retain their original source and rationale; build ledgers retain
the exact applied bytes. IDs intentionally have gaps: accepted upstream changes
and retired investigation tools are not silently renumbered.

| Area | Retained patch IDs | Purpose |
| --- | --- | --- |
| Random-source failure | 0090–0091 | Bound RP2350 hardware polling/health rejection and latch failures; stop `arc4random_buf` after clearing output when device reads cannot fill it |
| ESP32-S3 RF entropy access | 0093 | Hold the existing SDK PHY mutex through `/dev/random` sampling; refuse unavailable RF and unsupported PM and auto-sleep, cap reads at 32 bytes, and honor nonblocking raw-device locking |
| Original platform support | 0001, 0004, 0007–0008 | ESP32-S3 BLE, Wi-Fi, PSRAM validation and Freenove LED |
| POSIX/network integration | 0005–0006, 0022, 0042, 0077–0078, 0086–0087 | UDP empty datagrams, flat-build pthread cleanup, TCP connect/retransmission progress, safe link-layer dispatch, sequence validation before ACK/close progress, cumulative ACK acceptance after partial retransmission, and congestion-window recovery |
| Pico board and radio assets | 0009, 0011–0012, 0021, 0028 | Pico 2 W wiring, firmware validation, CLM alignment, calibration and open-network setup |
| Pico shared Wi-Fi/Bluetooth transport | 0013–0015, 0027, 0029, 0036–0038, 0040–0041 | HCI rings and power ownership; bounded frames, backpressure, receive validation and error propagation |
| RP2350 transfer correctness | 0044–0045, 0047–0049 | PIO receive counts/request mapping, DMA acknowledgement/lifetime and invalid FIFO-read detection |
| Bluetooth command and connection ownership | 0017, 0035, 0039, 0051, 0065 | HCI waiter lifetime, quiescent callbacks, ACL credits, disconnect reason and retryable scan cleanup |
| Bluetooth interoperability | 0019–0020, 0023, 0026, 0030 | Host receive capacity, MTU negotiation, accurate advertising state/errors and normal-send log level |
| Portable GATT client | 0059–0063 | Pending-request completion, discovery bounds/status, negotiated MTU and correct short UUID expansion |
| ESP Bluetooth allocation | 0018, 0052 | Pair kernel allocations/deallocations in both single-heap and separate-heap builds |
| Development USB recovery | 0016, 0064 | Opt-in 1200-baud ROM entry with an optional picoboot-only interface |
| Standard camera capture | 0067–0071, 0088–0089 | OV3660 sensor and Freenove registration; exact clock/source selection and board-configurable clock-pin drive, bounded DMA/cache ownership, and capture lifecycle error propagation |
| JPEG capture | 0073–0075, 0079–0085 | Sensor-before-DMA sequencing, bounded JPEG/queue ownership, continuous capture synchronization, configurable VGA capacity, bounded cache work, standard quality control and initialized scalar-control wrappers |
| Xtensa Espressif interrupt ownership | 0072 | Release NuttX-owned callback adapters after successful interrupt detach; retain them on failure for retry |
| ESP32-S3 performance accounting | 0076 | Initialize hardware-counter conversion with the actual configured CPU frequency before scheduler accounting |
| ESP32-S3 voluntary switching | 0106 | Build the full native context directly in flat single-core windowed-ABI builds; retain syscall switching for other targets and modes |
| ESP32-S3 IRAM product layout | 0107 | Include an empty-by-default product linker fragment; the build frontend stages and hashes a profile-selected synchronization layout |
| ESP32-S3 SIMPLE_BOOT image parsing | 0108 | Skip a declared ROM prefix digest before parsing flash-mapped segments |
| Signed ESP32-S3 SMP | 0115–0118 | Acknowledge peer flash-cache restoration, serialize station reconnect and stop, backport the heap mutex fix and select PSRAM-safe atomics |
| Espressif I²S receive and GDMA | 0126 | Roll back failed receive submissions and terminate a short ESP32-S3 descriptor list within its capacity |
| ESP32-S3 I²S receive interrupt | 0127 | Keep the RX interrupt on its registering CPU and serialize DMA completion with task-context queue operations |
| ESP32-S3 I²S receive stop | 0128 | Keep active buffers until GDMA is parked and submissions and callbacks have retired, then cancel and release the queued buffers |
| Host task debugging | 0151 | Read current native mutex owners, map ARM/Xtensa saved contexts correctly, and restore live debugger registers and selected frame after task inspection, including command errors |

Patch `0151` changes only the Python `pynuttx` host helpers. Use the helpers from
the private patched NuttX build copy with its exact unstripped firmware ELF;
sourcing the unmodified submodule does not apply this patch. `info nxthreads`
and `deadlock` read the current native mutex-holder encoding. The helper's
`thread apply` / `nxthread apply` commands preserve the live register bank and
selected frame when a subcommand fails, use the running task's live context,
and map saved Xtensa logical registers independently of remote-packet order.
`ps` additionally needs native macro debug data (`-g3`); without it the command
reports that prerequisite and points to `info nxthreads` instead of assuming
configuration values.

The focused host regression is
`python3 -m unittest discover -s tests/host -p test_pynuttx_threads.py`.
It applies the patch to a private copy, demonstrates
the old owner/cleanup failures, and checks the corrected behavior with literal
task/context inputs. Real GDB/QEMU qualification also checks optimized Rust
mutex wait sites, the native ownership cycle and healthy release/join control,
all-task inspection, and physical-register/selected-frame restoration after
injected command failures. These checks concern stopped flat single-core
targets; they do not qualify physical hardware, SMP inspection, arbitrary
deadlocks, or every debugger/ABI combination.
The original QEMU qualification used NuttX `2b5c0de5`; the current 13.1.1 pin has
byte-identical helper preimages, but fresh runtime qualification is required.
Retire this patch when the selected compatible upstream revision contains the
repair and the focused host and QEMU controls pass without the backport.
| ESP32-S3 BLE entropy source | 0130 | Admit an active BLE controller through the existing guarded RF entropy path; CoreS3 board wiring is maintained outside the patch |
| FT5x06 poll ownership | 0132 | Start and stop polling with open consumers and bound its interval; CoreS3 registration is maintained outside the patch |
| GPIO edge timestamps | 0145 | Retain the generic GPIO timestamp interface; CoreS3 pin registration is maintained outside the patch |

Patch 0090 bounds each RP2350 TRNG block's polling and health-rejection retries,
including startup. Failure disables the source, clears partial output and keeps
reads closed until reboot. `RP23XX_RNG_POLL_TIMEOUT` controls the busy-wait budget;
interrupts and mutex contention can extend wall time. This does not enable the
RNG in a product or qualify an application deadline.
The existing Pico 2 compile fixture enables this driver and checks the resolved
hardware-source configuration; running it on a board remains unqualified.

Patch 0091 removes the clock-derived fallback from NuttX's infallible random API.
Fallible consumers should use `getrandom()` and handle errors. A device read
succeeding still does not establish cryptographic entropy: source selection,
startup readiness and physical qualification remain required under #35.
Host controls execute pinned driver/libc source with injected registers/reads; they
do not qualify hardware randomness or target timing.

NuttX Apps changes cover NSH `dmesg` reset options, correct DHCP pad-option
parsing, the selected MCUboot helpers, bounded Dropbear service ownership, and
remote-prompt initialization.

The same manifest lists the retained NuttX Apps patches. Patch `0007` adds
`trace capture <seconds> <recording-id>` to the existing trace utility. It
records the enabled event scopes in a bounded, non-overwriting RAM window and
retains measured start/end timestamps for normalization, including quiet time.
An overflow preserves the admitted prefix and marks coverage incomplete; it
cannot establish a full-window event rate. See
[measured capture windows](nuttx-memory-qualification.md#capture-a-measured-window)
for ownership, clock, cancellation and qualification limits. The tracked empty
fork `build.log` is not part of a patch.

### CoreS3 board ownership

CoreS3 uses NuttX's custom-board interface. Its eight native board files live in
[`platform/cores3/board/`](../platform/cores3/board/), selected by the profile's
`board-dir` declaration. The existing board identity and BSP resource mapping
remain `esp32s3-m5-cores3`; the native selector is `ARCH_BOARD_CUSTOM` with that
custom name. See [board inputs and outputs](build-system.md#inputs-and-outputs)
for staging, source hashes and cache ownership.

This retires board-only patches 0104, 0109, 0123, 0129 and 0143. The board portions
of 0130, 0132 and 0145 move with them; those three patches retain their generic
entropy, touchscreen and GPIO changes. The extracted C, headers, Kconfig and
Make files preserve the previously prepared board bytes. Only the defconfig's
board selection and path change for the supported interface. This is an ownership
change, not new physical-device qualification.

### Pico integration and limits

NuttX 13.1.0 already includes the common CYW43439 transport and firmware helper;
the old 0010 backport was retired. Patch 0011 supplies the Pico 2 W board
adaptation and rejects missing firmware instead of using a dummy image. The
[upstream Pico 2 W board port](https://github.com/apache/nuttx/pull/19250) was
merged on 2026-10-28. The selected 13.1.1 source still lacks that port; replacing
the local adaptation requires a compatible source update and a comparison of
board wiring, firmware failure handling and selected radio behavior.

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
13.1.1 still selects `b90b1837cb5ad24747deb4c895246037cc206ce5`; the build records
the actual revision, patch hash and source pre/postimages in `esp-hal-patches.json`.
Keep this kernel-selected HAL revision: a newer independent HAL tag is not a
drop-in replacement for the NuttX driver interfaces. All eight registered
application HAL patches target this source.

| Patch | Purpose |
| --- | --- |
| [0001](../upstream/esp-hal-3rdparty/patches/0001-mbedtls-pair-configured-allocators.patch) | Pair mbedTLS's fallback libc `calloc` with `free`; keep capability allocations paired with `heap_caps_free` and preserve memory placement |
| [0002](../upstream/esp-hal-3rdparty/patches/0002-intr-alloc-pair-capability-allocators.patch) | Pair interrupt handles, shared vectors and trace arguments with `heap_caps_free`, including error paths; avoid cross-heap teardown in split-heap builds |
| [0003](../upstream/esp-hal-3rdparty/patches/0003-esp32s3-serialize-wifi-phy-state.patch) | Move NuttX's Wi-Fi hardware-bit operations inside the existing SDK PHY lock, completing the sampling/shutdown barrier used by NuttX patch 0093 |
| [0009](../upstream/esp-hal-3rdparty/patches/0009-c23-static-atomic-initializers.patch) | Replace the removed C23 `ATOMIC_VAR_INIT` wrapper with equivalent direct static initialization in GPIO, SPI, ADC and I2S sources for GCC 16 |

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

## MCUboot and image signing

The product builder and `tools/mcuboot-requirements.txt` use
[MCUboot v2.4.0](https://github.com/mcu-tools/mcuboot/releases/tag/v2.4.0), revision
`6d3b3d2c38ab20c242e5b9abb04d050086383eb2`. The Apps download guard and imgtool
require the same source archive SHA-256:
`37e0d9c9d90abf2b65c49b93401e1b4d44805295d9f5788bca83526864aeaa5b`.
The application-side flash-backend patches and selected bootloader patches apply
to this revision; image geometry, ownership, provisioning restrictions and
verification gates remain in force.

The image-class check uses MCUboot's existing `boot_uuid_cid_match()` callback.
Its [implementation](../tools/build/src/ffi/mcuboot_image_class.c) and
[CMake integration](../tools/build/mcuboot/image_class.cmake) live with the NXRS
build controller. The controller copies them into each fresh loader build and
records both hashes in `bootloader.json`. CMake 3.19 or newer attaches the callback
to the upstream executable through its project-include hook; no MCUboot source
patch is needed. MCUboot continues to parse and authenticate the signed class
TLV, and the callback retains its image-index check and FIH return contract.

The Espressif bootloader uses HAL revision
`911dbec8e4a92e70056b58a3d2b0d965b8b7bcc9`, separately from the application's
HAL. MCUboot v2.4.0 still requires ESP-IDF HAL 5.1.6, which this pin provides.
The protected bootloader's eFuse-write refusal patch applies to that exact
source. Changing the kernel HAL does not change the bootloader HAL.

## Rust, LLVM and std

The normal build controller uses Rust 1.100.0. Firmware std is rebuilt from
these independently pinned SDKs:

| Target family | Rust/std SDK | Private libc | Recorded patch entries |
| --- | --- | --- | ---: |
| Xtensa ESP32-S3 | `esp-1.100.0.0` | `0.2.191` | 16 |
| ARM and native NuttX simulator | `nightly-2026-11-26` | `0.2.191` | 15 |

The local [Xtensa LLVM series](../upstream/rust-llvm/README.md) remains an
opt-in compiler evaluation with its original Rust/LLVM revisions, source
preimages and patch bytes. Its six-patch default and named candidates are
built in a private sysroot by the [local builder](../upstream/rust-llvm/BUILDING.md).
The [arithmetic assessment](../tests/arithmetic-parity/RESULTS.md) retains its
matched device measurements and frozen 29-patch candidate. Updating the normal
SDK does not change those historical source or measurement identities.
Compiler evidence binds the actual compiler and driver hashes; source patches
alone do not prove which compiler built an image.

`tools/build/src/sdk.rs` owns preparation; `tests/nuttx-std/prepare-std.py`
forwards to that frontend. Preparation checks the exact source blobs before
copying an SDK, applies the selected committed patches to its private copy,
and records source preimages, resulting blobs and patch hashes in
`std-patch.json`. A generated lockfile patch records the local libc override,
including the Xtensa SDK's upgrade from its upstream locked `0.2.189` to
`0.2.191`. The registry archive is SHA-256 checked before extraction.
Installed SDKs and upstream source checkouts remain unchanged. Explicit older
SDK preparation is retained for the frozen evaluation paths.

The shared NuttX channel and parker implementations are unchanged by this
upgrade. Current context patches 0013–0017 preserve per-entry OS startup,
image-wide once-only main-thread identity, native parker selection, standard-fd
sanitization, startup `SIG_IGN` and the libc override. Rust 1.100 moved the
main-thread identity publication before `sys::init`; the new context patch
preserves that order while limiting identity publication to one call per image.
Both SDKs use the same channel and parker implementation bodies.

The [independent runtime comparisons](../tests/nuttx-std/channels/README.md#independent-runtime-comparisons)
reverse one change at a time in a private qualification SDK: the bounded queue,
native parker, or operation outlining. The normal frontend accepts that SDK only
for its exact qualification fixture and still applies its source and target ABI
checks. Production preparation does not select these experiments.

| Candidate for further reduction | Evidence required before retirement |
| --- | --- |
| Native bounded-channel patch 0007 | Preserve ownership, cancellation, disconnect and finite progress under the actual priority policy, including a preempted publisher between slot reservation and publication |
| Native parker and selector patches 0002 / 0014 | Qualify the narrower upstream pthread parker with NuttX mutex initialization independently of the queue; retain blocked-wait, timeout, wake, joined-retirement and target latency controls |
| Outlining patches 0010 / 0011 | Compare linked IRAM/shared-SRAM cost and workload timing with the same compiler, memory placement and queue/parker bodies |
| NuttX direct context-switch patch 0106 | Compare upstream switching under the same product workload and deadlines; keep other architectures, SMP and ABI modes qualified |
| Already accepted board or correctness backports | Select a compatible upstream revision containing the same fix, then pass the original regression with the local patch omitted |

An instrumented scheduling counterexample can rule out a candidate for that
contract. Passing ARM functional controls alone cannot establish Xtensa timing
or physical-device memory budgets. Preserve the production implementation until
its particular removal condition is met.

Two fixes are now supplied by upstream inputs. `libc 0.2.191` contains all
NuttX 13.1 open flags, so its older open-flags patch is omitted. The current
nightly already zero-initializes the complete native poll descriptor, so its
source is checked and recorded without applying the poll-initialization patch.
The Xtensa SDK still needs that std patch. Current libc patches retain the
complete NuttX poll descriptor, directory entry/inode/block-size layout and
socket-storage alignment. They also supply the omitted Xtensa `wchar_t`
selection as unsigned 16-bit, matching the pinned Espressif GCC 16.2 compiler
and NuttX headers, and give all exported poll masks the native `pollevent_t` width.
The old patch files remain tied to the older SDKs; they are not reapplied to
sources that already contain their fixes.

The compiled target ABI gate checks 130 observations, with one additional
Xtensa `wchar_t` signedness observation. These include every exported open flag,
all poll-mask aliases, native poll descriptor field offsets and widths,
semaphore storage/alignment, `SEM_PRIO_NONE`, inode/block-size types, and
public directory-entry/stat field offsets and widths. Trailing storage may be
larger; undersized buffers and shifted fields are rejected. The gate uses the
actual Cargo libc artifact and configured target headers. Compile-only witnesses
consume the paired `.rmeta` files from those Cargo records; reports retain the
libc link archive and all target metadata hashes, and the socket gate rechecks
both identities before reuse. `ppoll` remains
rejected because its signal ABI is unqualified. These checks do not
retroactively qualify older artifacts or establish physical target behavior.

Immediate-abort builds use the current compiler's `-Cpanic=immediate-abort`
with `-Zunstable-options`; the removed `panic_immediate_abort` std feature is
not requested. Diagnostic builds retain their selected panic reporting policy.
Local provenance marks tracked, uncommitted NXRS changes with `nxrs_dirty=yes`;
the recorded commit alone is not a reproducible source identity in that case.

The separate [std math RFC](../upstream/rust-std/README.md) routes NuttX
inverse-hyperbolic methods to C libm. Its original manifest, patch ledger and
complete packaged source inventory remain independent of the current normal
SDK and of LLVM qualification.

Browser-thread qualification uses `nightly-2026-11-26` and a separate opt-in
TLS-selection patch in `tools/build/src/web.rs`; the Python preparation entry
forwards to it. The current source selects WASI separately from generic Wasm.
The patch still sends atomic Emscripten thread cleanup through Unix TLS keys
instead of the generic Wasm branch that retains allocations at thread exit.
Its exact input/output blobs and generated diff are recorded in `std-patch.json`.
SDK upgrades require review of these selectors and real-source patch application;
host channel tests do not establish browser-thread or hardware qualification.
