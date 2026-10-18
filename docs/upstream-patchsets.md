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
| 0018 | GATT qualification | Match ESP32-S3 BLE controller allocations and teardown to the kernel deallocator |
| 0019 | GATT qualification | Advertise usable host ACL capacity, check controller acceptance, and bound receive lengths consistently |
| 0020 | GATT qualification | Negotiate peer/local ATT receive MTUs and recognize only the command flag |
| 0021 | Pico firmware staging | Match the SDK resource's 512-byte CLM alignment on RP2040 and RP23xx |
| 0022 | TCP qualification | Keep a pending handshake in poll after blocking mode is restored; avoid premature writable notifications |
| 0023 | Advertising qualification | Propagate controller/setup/stop errors and update cached advertising state only after confirmation |
| 0024 | Radio counters | Advertise existing BCMF/Espressif netdev statistics support; does not change radio or retry behavior |
| 0025 | Packet-loss diagnostics | Default-off Ethernet/gSPI frame observer; no radio or retry changes |
| 0026 | Bluetooth diagnostics | Normal ACL sends use info, preserving warnings in bounded logs |
| 0027 | GSPI frame capacity | Size slots to the configured bus limit, including DMA rounding; reject oversized TX before allocation |
| 0028 | Pico 2 W calibration | Select the pinned SDK PA coefficients in the board header; preserve the common fallback for other boards |
| 0029 | GSPI backpressure | Preserve queued frames when credits/FIFO are unavailable; fail closed on a failed readiness read |
| 0030 | Legacy advertising state | Track automatic shutdown after peripheral connection; make an already-disabled stop a no-op |
| 0031 | Association diagnostics | Default-off numeric event/state trace; no authentication or radio-policy change |
| 0032 | Receive diagnostics | Reuse existing controller query; default-off SPI/parser and RX work-dispatch observations |
| 0033 | PHY diagnostics | Decode documented legacy/version-10 PHY counters; report unsupported layouts unavailable |
| 0034 | Daemon diagnostics | Observe existing GSPI reads, IRQ notifications and waits without extra bus operations |
| 0035 | GATT connection callbacks | Expose connection events with caller-owned storage and quiescent callback removal; no new thread, timer or radio policy |
| 0036 | GSPI receive validation | Check complementary length bits and actual received extent before credits or dispatch; flush short records |
| 0037 | GSPI byte transfers | Stage partial/unaligned words for DMA without changing the wire length; preserve aligned zero-copy transfers |
| 0038 | GSPI I/O errors | Propagate register and power-transition errors; retain failed IRQ acknowledgements and keep the worker alive on malformed input |
| 0039 | Bluetooth transmit credits | Track outstanding ACL packets per connection; reclaim disconnect/failure credits and reject stale or excessive completions |
| 0040 | GSPI F2 word framing | Pad the bus command to its whole-word transfer extent, zero the tail and preserve the logical SDPCM packet length |
| 0041 | GSPI error acknowledgement | Enable fault IRQs, acknowledge polled errors without pending RX and reject stale pre-ack readiness |
| 0042 | TCP retransmission progress | Preserve running/pending deadlines across outgoing ACKs and retry state across idle polls; explicitly reset/cancel expiry state |
| 0043 | HCI protocol diagnostics | Distinguish invalid ring indices from malformed frame metadata without dumping packet bytes or changing fault policy |
| 0044 | RP23xx PIO receive count | Encode the word-rounded receive count minus one, including the existing F1 dummy word |
| 0045 | RP23xx DMA acknowledgement | Write the owned channel mask to the write-one-to-clear status register, preserving other channels' pending completions |
| 0046 | Shared-bus fault readback | In error-debug Bluetooth builds only, read back bus settings after HCI framing faults or once per FIFO error burst; no retry or repair |
| 0047 | RP23xx DMA lifetime | Check bus ownership/allocation errors, preserve in-flight transfers across signals, and retire failed DMA before releasing callback storage |
| 0048 | RP23xx RX FIFO validity | Reject an empty peripheral read even when DMA reports completion; record bounded error-debug pacing metadata without retry or repair |
| 0049 | RP23xx PIO request mapping | Select PIO1's DMA request bank rather than PIO2; test every PIO/state-machine/direction mapping |
| 0050 | HCI RAM fault readback | Compare live/cached RAM bases and paired/single index reads after a protocol fault; debug-only, no recovery or memory dump |
| 0051 | Bluetooth disconnect reason | Retain the controller reason before connection callbacks; no connection-policy or handle-logging change |
| 0052 | ESP BLE allocator portability | Wrap the kernel deallocator callback so single-heap macro and separate-heap function builds both compile |
| 0053 | Test-only SPI capture | Default-off, serialized read hooks for an independently claimed PIO/DMA observer; no transfer-policy changes |
| 0054 | Link-event diagnostics | Default-off controller connection parameters and native tick timestamps, without peer identifiers or retry changes |
| 0055 | Test-only SPI write capture | Default-off serialized write hooks, reusing the passive read observer; no transfer-policy changes |
| 0056 | LE connection timing | Validated caller-supplied timing through the normal create/scan path; existing API and defaults unchanged |
| 0057 | Setup-stage diagnostics | Existing default-off link trace gains bounded HCI/L2CAP/ATT/driver milestones; no payloads or policy changes |
| 0058 | Buffered setup diagnostics | Capture first milestones in fixed storage; defer setup-log formatting until disconnect to reduce timing intrusion |
| 0059 | ATT request lifetime | Complete pending requests on disconnect; detach all callback/destructor state before reentrant user code |
| 0060 | Service discovery bounds | Retain the current service group's end handle for scoped characteristic discovery; reject malformed ranges and avoid empty-response loops |
| 0061 | Negotiated ATT MTU | Expose the existing negotiated value so bounded clients can distinguish complete reads from full-size fragments |
| 0062 | Discovery completion status | Distinguish a complete search from ATT errors, malformed responses and failed pagination; do not accept a valid prefix as an exhaustive result |
| 0063 | Short UUID expansion | Compare equivalent 16-bit and 128-bit UUIDs correctly, independent of host endianness |
| 0064 | USB recovery policy | Optionally disable BOOTSEL mass storage for the USB recovery hook; preserve normal status-3/manual UF2 entry |

Patch 0059 fixes a separate central-GATT disconnect bug: the host previously
erased the pending request without completing it. Its sanitizer-backed witness
executes the patched ATT functions for pending MTU/discovery/read/write opcodes,
duplicate terminal paths, destructor reentry, response chaining and channel
reuse. It does not add ioctl deadlines or make cancellation a cross-thread
callback barrier, and is not a fix for radio-level reason-8 timeouts.

The tracked empty fork `build.log` is intentionally excluded from patch 0002.
Patch 0045 uses the Pico SDK's normal DMA acknowledgement path. RP23xx bit
helpers use hardware atomic aliases, not software read-modify-write; its host
witness checks the actual addresses and masks without inventing RP2350 alias
behavior. Patch 0047's before/after witness executes the actual transfer and
uninterruptible-wait functions, covering signals, lock/allocation errors,
DMA failure and callback lifetime. Neither witness proves physical causality.

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
The current radio asset pin follows Pico SDK 2.3.1, including its `.0065`
Bluetooth patch with the [vendor's Data Length Extension fix](https://github.com/georgerobotics/cyw43-driver/commit/7f422fef4ea5bff7285fb78340d3f28f5461cff2).
This is an asset update, not a demonstrated explanation of connection timeouts.
For the retained `.0031` comparison, set
`NXRS_PICO_RADIO_RELEASE=pico-sdk-2.1.1` for the normal build. Caches are scoped
by revision, and provenance records the selected pin; no SDK checkout is needed.
The firmware is not checked into nxrs. Bluetooth profiles select the pinned
`wb43439` WLAN image, as the Pico SDK does; Wi-Fi-only profiles retain `w43439`.
They also stage the separately pinned controller patch (6,970 bytes for the
current `.0065` pin; 6,164 for the retained `.0031` pin) and link no Pico SDK
runtime. Separate binary cache paths prevent the two modes
from overwriting each other's build input. Patch 0021 aligns CLM to 512 bytes:
the old 256-byte rounding coincidentally worked for `w43439`, but would select
padding rather than CLM for `wb43439`. Native tests compile both board macros
against the actual firmware lengths and alignment boundaries.
Patch 0014 is experimental, not an upstream-accepted or fully hardware-
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

Patch 0018 fixes an allocator mismatch exposed by a separate kernel heap:
the ESP32-S3 controller allocated with `kmm_malloc` but freed through the user
heap. Replacing advertising data reached `mm_heapmember`'s assertion. Both
the controller free callback and OSI-table teardown now use `kmm_free`;
allocation policy and radio behavior are unchanged.
Patch 0052 makes that callback portable to single-heap builds, where
`kmm_free` is a function-like macro rather than an addressable function.
The allocator witness compiles and exercises both forms.

Patch 0019 fixes the next ESP32-S3 GATT blocker: its controller rejects the
old 74-byte host ACL capacity, but an asynchronous command hid that rejection
and enabled receive flow control with zero credits. The host now accommodates
a 251-byte ACL payload (bounded by the configured IOB), represents the complete
256-byte frame and checks controller acceptance before enabling flow control.
Its receive bound accounts for H4 independently of transmit headroom and
cannot wrap for an oversized length. The host witness exercises actual buffer
and initialization functions with 128/400/512-byte IOBs, controller errors and
allocation failures under sanitizers. Restoring ACL reception alone does not
prove GATT qualification.

Patch 0020 fixes ATT interoperability exposed once packets reached the host.
The Mac advertises a 527-byte receive MTU; NuttX incorrectly rejected it instead
of negotiating its smaller capacity. An incorrect command mask also suppressed
the resulting error reply. The patch advertises the same local receive limit
in both exchange directions, negotiates the minimum and recognizes only bit 6
as the command flag, following [Core Vol 3 Part F, sections 3.3.1 and 3.4.2](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-62/out/en/host/attribute-protocol--att-.html).
The host witness compiles actual ATT handlers and checks a large peer MTU,
minimum/invalid MTUs, truncated requests, error replies, command suppression
and repeated cleanup. These are host-stack fixes, not platform-specific HAL
retries or application workarounds.

Patch 0022 is included in the normal builder. Its source witness checks both
buffered and unbuffered TCP, successful handshakes and connection failures.
Patch 0023 makes advertising setup synchronous and preserves stop failures
through the ioctl. Its source witness checks every setup stage and failed
start/stop/restart operations. Neither patch changes application deadlines.

Patch 0024 declares existing statistics support: BCMF updates its TX queue
and IPv4/unsupported receive counters; Espressif WLAN uses the common netdev
upper half's counters. It does not turn counters on unless requested, add
retries or alter transmission. The radio fixture requests counters explicitly.

Patch 0025 is diagnostic instrumentation, not a corrective patch or an
upstream-accepted API. Only the Pico radio qualification enables it. A weak
no-op observer can be overridden to record Ethernet/gSPI boundaries without
logging from driver callbacks. Disabled configurations contain no observer
calls or storage. Bus-write success is not an over-the-air acknowledgement.
Its task-only counter query validates legacy layouts and modern, bounded XTLVs;
unsupported or failed queries remain unavailable.

Patch 0032 extends that diagnostic only: receive errors, buffer/FIFO shortages,
filtering and resets come from the already-queried, validated counter layouts.
It records existing GSPI receive outcomes and RX work dispatch without extra
reads, polls or retries. Coalescing is not a queue error; controller-global
counts are not per-packet proof. The fixture rejects deltas across controller
resets. Native sanitizer witnesses exercise every receive outcome, dispatch
failure/coalescing and diagnostic-enabled/disabled builds.

Patches 0033–0034 narrow that investigation without tuning the radio. PHY
checksums/preambles are separate from software RX errors; version 30's decoded
software prefix does not provide them. Daemon observations reuse existing
status reads and IRQ/wait events. A cached FIFO observation is meaningful only
with a successful read and its age; it is not a live RF capture. All observers
remain default-off and nonblocking, including those called from an ISR.

Patch 0027 corrects a separate capacity invariant: GSPI advertised its configured
maximum but allocated smaller Ethernet-sized slots. It keeps the SDIO layout,
pool count and configured limit unchanged. This allocates the RAM that the
existing GSPI limit actually requires; it is not a workaround for the observed
small-packet loss or a radio/retry-policy change.

Patch 0028 makes the PA coefficients board-selectable and aligns the Pico 2 W
adaptation with the pinned SDK NVRAM resource. Its native regression checks
that only `pa2ga0` changes among all 46 fields; other RP23xx configurations keep
their original value. Reference alignment alone does not establish a fix for
Wi-Fi/Bluetooth packet loss.

Patch 0029 rejects credit exhaustion before dequeueing and checks a fresh F2
readiness value before consuming a frame. The existing worker handles
`-EAGAIN`, so receive and Bluetooth work can continue; no application retry or
new spin loop is added. Native witnesses check queue/sequence preservation,
read failures, normal write cleanup and sequence wrap with tracing on and off.
This fixes a driver contract violation, not a proven explanation of every loss.

Patch 0030 tracks the controller's automatic end of legacy advertising on a
successful peripheral connection. The host clears its cache before connection
allocation and can then stop an already-disabled advertiser without sending a
redundant command. Central/failed connection events preserve the prior state;
active-stop errors still propagate. The existing native advertising witness
covers these transitions and allocation/command failures.

Patch 0035 exposes the existing GATT connection events through caller-owned
callbacks. Successful removal waits for any active invocation, so callback
storage can then be released safely. Callbacks must be short and must not
register/remove callbacks or perform synchronous Bluetooth operations.
The radio fixture uses disconnect events to cancel incomplete work instead of
waiting for its watchdog. The native witness exercises dispatch ordering,
concurrent removal and fixture cleanup failures. This does not fix the separate
unbounded central ATT ioctl waits or provide a portable GATT HAL.

Patches 0036–0038 correct transport contracts, not application retry policy.
Native tests reproduce the old framing and byte-buffer faults and exercise
the actual patched functions under sanitizers. They also check partial/failed
register reads, interrupt acknowledgement, clock readiness and cached power
state. These proven defects are not yet a demonstrated explanation of every
observed radio failure; hardware qualification remains separate.

Patch 0039 follows the controller-buffer reclamation rule on disconnection.
Its native witness reproduces the old reconnect credit loss, checks 32 corrected
cycles and exercises the actual transmit function's failure/completion races.
It adds a counter per connection, not another queue, thread, retry or timer.
Hardware tests are still needed to establish whether it explains a particular
warm-use failure.

Patch 0040 makes F2 command lengths match their word-rounded physical transfer,
as in the [CYW43 reference sender](https://github.com/georgerobotics/cyw43-driver/blob/055d64274b014dd7b1c2fc94d26e8a18face7124/src/cyw43_ll.c#L716).
It uses the existing rounded frame slot and changes no payload, retry policy
or thread. The native witness separates this framing correction from whether
it explains the observed intermittent FIFO overflow on a physical board.

Patch 0041 prevents a sticky bus fault from waiting indefinitely for another
receive interrupt. Fault-injection tests use the actual worker, with tracing
both on and off, and preserve queued frames on failed acknowledgement.
Patch 0042 follows [RFC 6298's timer policy](https://www.rfc-editor.org/rfc/rfc6298.html#section-5):
unrelated outgoing ACKs do not restart a running deadline. Its native witness
also checks pending expiry, zero receive windows and idle retry state in all
three send modes. Neither patch changes application deadlines or retry limits;
passing these contracts is separate from physical radio qualification.

`external/nuttx-apps` is pinned to the matching **13.1.0** release,
`3c772ea35b9245972e18999828f7b533e6fc57cc`. Its patch series contains:

| Patch | Purpose |
| --- | --- |
| 0001 | ESP32-S3 VHCI transport for NimBLE, from fork commit `eaab369070bf` |
| 0002 | Reset NSH `dmesg` option parsing on each call; fix clear-only syntax/help and preserve read failures |

The `dmesg` witness uses the pinned NuttX parser and actual command body to
reproduce the baseline's alternating read-and-clear behavior and verify the
correction. This is a diagnostic-tool fix, not a radio fix. Patch 0031 shares
the existing default-off frame diagnostic switch; it records numeric event
fields without network names, credentials, addresses or payloads.

Both series are applied by
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

## Espressif SDK compatibility

ESP32-S3 builds also patch their isolated `esp-hal-3rdparty` copy after NuttX
context setup, before compilation. The source cache is never patched. NuttX
13.1.0 selects `b90b1837cb5ad24747deb4c895246037cc206ce5`; the build records
the actual revision, patch hash and source pre/postimages in `esp-hal-patches.json`.

| Patch | Purpose |
| --- | --- |
| [0001](../upstream/esp-hal-3rdparty/patches/0001-mbedtls-pair-configured-allocators.patch) | Pair mbedTLS's fallback libc `calloc` with `free`; keep capability allocations paired with `heap_caps_free` and preserve memory placement |

The previous pairing triggered the NuttX adapter's cross-heap warning and
fallback on ESP SoftAP startup. The resulting log flood interrupted shell
completion markers. This correction is not a Pico Bluetooth timeout fix.
With the same AP configuration, captured warnings fell from 437 to zero and
the strict console completion check passed. Four subsequent UDP/TCP C/Rust
coexistence cases completed 4,000 exact echoes and 400 GATT pairs in total.
Host tests compile the applied source across allocation modes, thresholds,
failure/NULL cases and custom-allocator compile-out without downloading the SDK.

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
