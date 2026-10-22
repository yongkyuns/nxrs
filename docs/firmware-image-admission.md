# Firmware image admission qualification

`driver/firmware-image` authenticates an immutable MCUboot container against an
independently trusted nxrs policy and caller-supplied security floor. It is a
`no_std`, allocation-free Rust protocol core with unsafe code forbidden. It does
not open a device, write flash, request a swap, confirm an image or advance state.

The caller selects a trusted crypto implementation and approved RSA-3072 root.
The `Crypto` contract requires SHA-256 and RSA/PSS/SHA-256, MGF1/SHA-256 and exactly
32 salt bytes. `Trust::key_sha256` identifies the same public key using PKCS#1 DER.
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

## Remaining device gates

This core is not yet wired into an update endpoint or the bootloader's custom-TLV
policy decision. It needs an owning Rust update controller and qualified target
crypto, flash resource, protected rollback state, trial/confirmation, cancellation,
shutdown and recovery behavior. A host result or caller-provided counter does not
satisfy those gates. Physical ROM trust, manufacturing/root rotation, debug policy,
actual target memory/timing and power-interruption recovery remain open. The
existing ESP32-S3 qualification-only profile is not deployment authorization.
