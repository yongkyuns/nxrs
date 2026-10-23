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
It rejects unsupported FIH ABIs and alternate/asynchronous SHA contexts. SHA
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
geometry, protected rollback storage or production custody. ESP32-S3 Rust
callback linkage, the final ROM-signed bootloader size, complete mixed-language
inventory, boot stack/watchdog/flash ownership, and physical interrupted boot
remain separate open qualification gates. Existing ESP32-S3 build profiles do
not silently enable an unqualified hook. No provisioning, boot, flash, eFuse,
debug-lock or deployment operation is performed by this qualifier.
