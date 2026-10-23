# Firmware image admission qualification

`driver/firmware-image` authenticates an immutable MCUboot container against an
independently trusted nxrs policy and caller-supplied security floor. It is a
`no_std`, allocation-free Rust protocol core with unsafe code forbidden. It does
not open a device, write flash, request a swap, confirm an image or advance state.

The caller selects a trusted crypto implementation and approved RSA-3072 root.
The `Crypto` contract requires SHA-256 and RSA/PSS/SHA-256, MGF1/SHA-256 and exactly
32 salt bytes. `Crypto::key_sha256()` identifies the actual fixed verification key
using canonical PKCS#1 DER. Admission compares this provider-reported fingerprint
with the independently approved `Trust::key_sha256` before hashing or verifying
incoming bytes. A mismatch returns `FormatError::Policy`; the staging owner makes
it a sticky policy fault before any flash mutation. Implementations must derive
the fingerprint from their actual key and refuse construction if that fails.
Copying the policy's expected fingerprint into a provider would violate this
trusted contract. This required method has no accepting default.
The incoming image cannot select the policy, key, layout or security floor.

Admission bounds the complete unpadded/unconfirmed image and scratch-swap trailer,
checks the exact header and protected/unprotected TLVs, refuses unknown/duplicate
entries and authenticates the complete signed region. It reconstructs the exact
canonical nxrs manifest using the trusted target, policy/configuration/layout,
authenticated counter/version and actual payload hash. It does not allocate a
JSON tree or process arbitrary nesting. Correctly signed wrong-policy metadata
is refused. Application versions remain independent from security counters.

`AuthenticatedImage` borrows the exact validated bytes and exposes the payload,
version and counter. Its constructor is private. Successful authentication alone
does not authorize activation or prove the provenance of the supplied floor.

## Actual crypto qualification

Install the existing fully hash-pinned target tool environment in a new private
directory using `tools/boot/install_target.py`. With that runtime:

```sh
python tools/boot/qualify_admission.py --work /private/tmp/nxrs-admission-qualification
```

The runner authenticates the frozen official Mbed TLS 3.6.7 archive, builds its
actual RSA/SHA-256 implementation, and creates a fresh encrypted synthetic image
authority. Test-only native glue under `tests/boot/admission` translates SDK
context size/alignment and the SHA-256 enum. Rust owns allocation, initialization,
import, verification and destruction; calls are synchronous and no callbacks
outlive the context. This fixture is not a production crypto provider or C HAL.
Its seven-module configuration uses standard SDK allocation and is distinct from
the bootloader's fixed-buffer configuration.

The actual offline producer supplies the positive image. Thirty-one cases include
correctly signed wrong target, configuration, policy, layout, payload digest,
role, schema, counter and version; unknown/duplicate/noncanonical/nested metadata;
zero/duplicate counters; unknown TLVs/flags; wrong root (also with a forged approved key ID)/PSS salt; tampering,
trailing preconfirmation bytes and length bounds. Signed policy faults must first
pass the actual native RSA verifier before the Rust admission refusal is counted.
A separate regression initializes the actual SDK with the second real root,
verifies that root's genuine signature independently, and then requires policy
refusal for both the approved-root image and the other-root image with a forged
approved key ID. The SDK fixture re-exports its actual imported public modulus
and exponent, hashes their canonical PKCS#1 DER, and compares that identity with
cryptography's independently serialized root. This checks accidental provider
misconfiguration; it does not claim an attacker can change a trusted provider.
Every proper prefix of the positive image and 10,000 deterministic single-byte
corruptions must fail. Invalid trusted layouts are also refused.

The C SDK and header witness use address/undefined-behavior sanitizers with
failure termination. Actual heap-overflow and undefined-shift probes must first
fail with the expected sanitizer diagnostics. Rust is not sanitizer-instrumented. Linux enables leak
detection; macOS does not claim that coverage. Core-only Thumb and WebAssembly
compilation establish portability only, not NuttX ABI or physical qualification.

Only the bounded public qualification JSON is retained by CI. It binds the
repository/source/configuration, compiler/runtime, native archives and executable
and records the exact required cases. Keys, authority state, images, objects and
raw stage logs stay private. The record is unsigned and does not claim native
advisory closure, a complete product SBOM or trusted release attestation.

## Target headers, linked images and emulator execution

`qualify_admission_target.py --work <new-private-directory>` independently
compiles the frozen seven-module SDK and test ABI witness with the pinned actual
Xtensa compiler. It measures ELF32 context size/alignment, the SDK SHA-256 enum
and pointer width directly; Rust never copies a guessed native struct layout.
This cross-build alone does not claim execution or physical qualification.

On disposable Linux with the existing frozen NuttX/emulator tools, add `--nuttx`.
The runner refuses a writable live `/tftpboot` before creating outputs, preventing
ordinary NuttX make rules from publishing an image to a personal service.
The same Rust fixture/native SDK must link into both a full camera MCUboot
profile image and an ordinary NuttX Xtensa emulator image. The camera output
must retain the exact approved public policy/configuration/payload bindings.
The final ELFs must contain the actual SDK verifier, SHA-256 and ABI witnesses.
Two fresh emulator kernels execute the entire 31-case/truncation/corruption suite
and a real higher-floor refusal with nonzero NSH status. No replacement verifier,
source-main substitution or hardware radio/camera simulation counts toward this
protocol test. The existing QEMU profile's mock HAL bindings are not exercised
by this software crypto fixture and do not prove hardware behavior.

The admission-only emulator has no network device or monitor and uses snapshot flash. Console
bytes and wall time are bounded; process groups are terminated/reaped and image
hashes must remain unchanged. Raw serial logs remain private. The public report
separates cross-compilation, linked NuttX images, emulated execution and physical
qualification; no physical device or flash operation is part of this runner.

The nonzero floor-refusal invocation is also checked on the native host fixture.
Rust returns normally so native context cleanup precedes process exit. Execution
oracle controls refuse missing/duplicate/reordered/unknown cases, wrong totals,
kernel/Rust failures and forged or duplicate process statuses.

## Remaining device gates

This core is not yet wired into an update endpoint or the bootloader's custom-TLV
policy decision. The [Rust staging owner](../hal/update/README.md) owns secondary
writes, cancellation and shutdown through an explicit model or NuttX raw-MTD
provider; its separate qualification adds an isolated provider-root-refusal kernel
whose entire private flash copy must remain unchanged. Production crypto,
protected rollback state, trial/confirmation and physical recovery still need
qualification. A host result or caller-provided counter does not satisfy those gates. Physical ROM trust, manufacturing/root rotation, debug policy,
actual target memory/timing and power-interruption recovery remain open. The
existing ESP32-S3 qualification-only profile is not deployment authorization.
