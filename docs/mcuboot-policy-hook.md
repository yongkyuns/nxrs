# MCUboot policy callback integration

`driver/mcuboot-policy` binds the portable image policy to MCUboot's actual
`boot_image_check_hook`. It is a read-only boot-SDK protocol adapter, separate
from the application update HAL and its kernel/worker lifecycle. It does not
provide a general flash HAL, alternate bootloader, signature verifier or
activation API.

The boot firmware invokes `bind_image_check_hook!(TRUST)` with independently
approved policy data. The emitted Rust callback rejects a policy error with the
SDK's `FIH_FAILURE`. A pass returns the SDK's `FIH_BOOT_HOOK_REGULAR`, which makes
the real loader run its normal digest, RSA/PSS and floor checks. Returning
`FIH_SUCCESS` here would skip those checks; the callback never returns it.
The application security floor is not inferred from this policy check.

The adapter hashes the sole public key from the actual linked MCUboot key table
and compares its canonical PKCS#1 DER SHA-256 with the independently trusted
root before opening a flash area. It checks the actual SDK area offset and
length against the selected primary/secondary slot, then uses the bounded
streaming parser. It refuses unsupported image/slot indices. An SDK area handle
is closed on every subsequent return, including policy failure.

Rust owns validation and resource cleanup. The private `src/ffi/sdk.c` reads
actual SDK fields and sizes; it contains no policy or state machine. Compile it
against the same MCUboot/Mbed TLS headers and configuration as the bootloader.
It rejects unsupported FIH ABIs and alternate/asynchronous SHA contexts. Rust
feeds the actual SDK watchdog macro after successful bounded read/hash progress. SHA
uses a borrowed, fixed, aligned 256-byte storage region whose address remains
stable from initialization through free. No Rust allocator is needed.

Opening the SDK flash area does not establish an exclusive lease. The caller
must own the single, nonreentrant prekernel execution context and prevent writes,
DMA, untrusted callbacks and key-table mutation through policy checking, stock
signature validation and final boot selection. Native calls must be synchronous
and return normally. These safety requirements must be established by each boot
firmware integration; they are not evidence of a qualified physical board.

## Qualification scope

`tools/boot/qualify_signing.py --minimal-crypto` retains the full upstream and
repository native recovery suites, the affected-lock negative control, and the
fresh-authority signature suite. A separate SDK copy then compiles the real
loader with the production callback. The frozen registry graph may gain only
the two local Rust crates and their exact edges, and the resulting complete host
graph is audited again.

The disabled-hook control demonstrates that the actual SDK accepts correctly
signed wrong-manifest images without policy enforcement. The enabled-hook run
requires policy refusal, accepted valid images, corrupted-signature/wrong-root/
PSS-salt refusal, higher-floor refusal, compiled-root/actual-geometry refusal
with restored success, a valid secondary trial/revert, and invalid-secondary
fallback to the valid primary. The SDK's simulated counter must retain the
expected value after each refusal.

`boot-hook-qualification.json` records this host-loader scope. The K64f flash
model, simulated counter and disposable fixture authority are not product
geometry, protected rollback storage or production custody. The ESP32-S3 build now links this callback through explicit bare-boot firmware
wiring, independently of the application update HAL. The source/SDK build
qualification below does not qualify physical boot. No provisioning, boot, flash, eFuse,
debug-lock or deployment operation is performed by this qualifier.


## ESP32-S3 cross-build

The normal boot builder now requires the policy archive and actual SDK ABI glue;
a missing archive or missing strong callback fails the build. A real missing-archive
CMake refusal and an exact linker-map archive-origin check are required; an SDK
fallback symbol cannot satisfy the latter. `platform/mcuboot`
owns only bare-boot firmware wiring and a nonreturning panic handler. It obtains
its generated immutable `Trust` from the independently pinned public policy;
ordinary unbound library builds export no boot callback. No new flash HAL,
provider-selection feature or C state machine is introduced.

The official Espressif 1.90.0.0 compiler and matching Rust sources are installed
under the disposable build directory using independent release SHA-256 pins for
macOS ARM64 and Linux x86-64. Upstream Cargo 1.98.1 drives the frozen Xtensa
compiler successfully. Cargo runs from the private working directory with a
fresh Cargo home, exact lockfiles, known flags and no registry credentials.
This Cargo version includes the fixes for [the registry extraction advisory](https://blog.rust-lang.org/2026/05/25/cve-2026-5223/)
and [the sparse-registry credential advisory](https://blog.rust-lang.org/2026/05/25/cve-2026-5222/).
No global toolchain switch or SDK replacement is performed.

The final static library contains exactly the three local Rust components,
`core` and the distribution's `compiler_builtins`; no registry code is compiled.
The independent verifier re-reads actual compiler-artifact output and dependency
files, checks both authenticated compiler/source archives, the final linked
archive and all source digests, and requires all five components and two host
build scripts. The native inventory and CycloneDX/SLSA-shaped records include
these inputs and the two mechanical C binding files. C compiler options describe
C compilation units; Rust compiler/profile information is reported separately.
Coherent Rust component removal, compiler substitution and coverage claims,
plus actual private standard-library-source and linked-archive changes, must be
refused and restored before qualification succeeds.

Qualified fixture images are about 49 KiB unsigned and 57,344 bytes after
ROM-format padding/signature, within the existing 61,440-byte reserve. Exact
unsigned sizes depend on the policy/build and are recorded per artifact. No flash layout or
application slot is enlarged. Actual disassembly requires a strong windowed-ABI
callback and records its 5,376-byte frame. This measures that function's frame,
not the complete boot call stack. Both the product watchdog flag and the actual SDK watchdog-enable definition
are required, with the SDK's 9,000 ms timeout. The former alone did not start the
watchdog in the frozen port. The pinned sdkconfig mapping fixes that defect;
actual bootloader-init preprocessing and missing-mapping refusal/restoration
controls enforce it. Bounded policy progress feeds the SDK macro; hardware
reset timing remains unqualified.

Full boot-stack and watchdog qualification,
exclusive flash ownership, physical ROM boot, protected rollback, interrupted
activation/recovery, production root custody and trusted release attestations
remain open. Rust toolchain/core advisory closure is also explicitly unclaimed;
the exact C-only SDK advisory review is not a Rust compiler assessment. The
cross-build report remains nondeployable and no board, serial/reset, flash,
eFuse or debug-lock operation is performed.


## ESP32-S3 emulator execution

`qualify_target.py --execute` builds a test-only no-std Rust application with
volatile entry evidence, then signs the existing admission corpus with the same
disposable authority as the production bootloader. The actual frozen SDK
`esp_image_load_header_t` encodes the payload; the fixture has no alternate
loader, native policy or UART dependency. Nothing in the production boot image
is instrumented or replaced.

A checksum-pinned Espressif QEMU runs each private 16 MiB image with networking
disabled and snapshot writes. Its debugger listens only on a private Unix socket.
The pinned emulator source defines the register mapping and explicitly lacks
the RTC watchdog counter/reset model. A successful boot cannot prove watchdog
reset timing in this emulator. Breakpoints come from
the actual final ELF symbols/disassembly, including the no-image panic after
`boot_go`. Bounded, checksum-checked debugger observations require the production
SDK watchdog initialization with its actual RWDT argument, the production
Rust callback, its SDK continuation return, the stock image/signature path, and
then application entry plus the actual volatile write. Correctly signed wrong
policy must fail before stock checking; invalid signatures and PSS salt must
continue policy checking but fail stock authentication. Every negative reaches
the actual no-image halt, rather than merely timing out. All input flash files
must remain unchanged.

The executable corpus has 32 cases: four accepted slot images and 28 refusals,
including a signed container at the exact slot/trailer budget and its corrupted
signature. The actual producer must refuse one additional payload byte.
Unused bytes after the signed image container are permitted slot data, as in the
host boot-loader suite. The separate stored-floor case is explicitly skipped:
the target has no protected-counter backend. It remains required in the host
simulator and remains an open target acceptance gate. The emulator does not
qualify physical ROM/fuses, protected state, flash ownership, complete stack,
watchdog timing, NuttX application startup, activation/recovery or deployment.

`boot-execution-qualification.json` binds the boot-build/ELF, public policy,
executable payload, emulator distribution/ROM/debugger source, observed sites and
source inputs. Only this public report is uploaded. The OS/dynamic emulator
libraries are host prerequisites; their advisory closure is explicitly unclaimed.
The existing cross-build report retains its narrower scope and false execution
flag; execution evidence is a separate report. Linux CI requires both.


## Observed boot stack budget

The SDK-header probe records the 8 KiB ROM boot stack and the actual watchdog
constants; it is never linked into firmware. At `boot_go` entry, after SDK
initialization, the qualification paints only the SDK stack region below the
active stack pointer and a preserved 64-byte Xtensa spill allowance. The actual
ELF's data/crypto heap must end below this region. It verifies the paint before
continuing, samples SP at every observed event, and reads the region at the
actual no-image halt or application park. All 32 cases must preserve at least
1 KiB of untouched guard. A deliberate memory write into that guard in a fresh
emulator must produce the specific guard refusal; a separate restored positive
must still boot. Private input flash remains unchanged even on refusal.

This measures writes from `boot_go` through the terminal observation. It does
not capture earlier ROM/SDK initialization, unused stack reservation, every
instruction's SP, asynchronous hardware activity, or all future call paths.
`full_startup_or_static_bound_qualified` and `boot_stack_qualified` remain false.
The guard is an enforcing qualification check, not a production runtime guard.
The measurements must not be presented as a complete static stack proof.


## NuttX watchdog handoff

Enabling the boot watchdog also requires correct application handoff. The frozen
NuttX startup function previously wrote the enable bit while write protection was
still locked, then wrote the lock register. Patch 0092 uses the existing native
SDK operations to unlock, disable and relock before NuttX proceeds. It adds no
C policy or orchestration outside the necessary native register operations.

The target qualification fetches the exact checksum-pinned NuttX source matching
`upstream/nuttx/release.json`, applies the real patch to a private copy, extracts
only that actual startup function and executes it with a protected-register host
model. Register addresses, enable mask and unlock value come from the separately
compiled actual SDK header probe. The original and a reordered-unlock control
must fail; patched/restored functions must preserve unrelated bits, end locked
and remain idempotent for all four initial enabled/protected states. This model
is not actual MMIO or target ABI qualification. Linux CI separately builds the
full patched NuttX application against the real SDK. Physical handoff and reset
timing remain open; no RTC watchdog reset is inferred from QEMU.
