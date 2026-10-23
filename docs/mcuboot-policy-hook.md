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

Qualified fixture images are about 48 KiB unsigned and 53,248 bytes after
ROM-format padding/signature, within the existing 61,440-byte reserve. Exact
unsigned sizes depend on the policy/build and are recorded per artifact. No flash layout or
application slot is enlarged. Actual disassembly requires a strong windowed-ABI
callback and records its 5,376-byte frame. This measures that function's frame,
not the complete boot call stack. The SDK watchdog-enable definition is required
and bounded policy progress feeds its real macro; hardware reset timing is not
qualified.

Target execution of the callback, full boot-stack and watchdog qualification,
exclusive flash ownership, physical ROM boot, protected rollback, interrupted
activation/recovery, production root custody and trusted release attestations
remain open. Rust toolchain/core advisory closure is also explicitly unclaimed;
the exact C-only SDK advisory review is not a Rust compiler assessment. The
cross-build report remains nondeployable and no board, serial/reset, flash,
eFuse or debug-lock operation is performed.
