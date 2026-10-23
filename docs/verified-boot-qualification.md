# Verified boot qualification

Issue #34 remains open. Legacy ESP32-S3 NuttX image packaging supplies no
signing key. The opt-in policy-bound camera profile and actual native boot
builds are described in [the build guide](../tools/boot/README.md#esp32-s3-build-only-integration).
An authenticated physical boot chain still requires the remaining ROM, protected
state and device recovery gates.
The existing camera profiles expose development/debug facilities and are not
production security profiles. No board boot, flashing or irreversible action is
part of this host qualification tool.

The CI recipe also requires `qualify.py --update-protocol`: real native bootutil
trailer observations are checked by the exact production Rust staging predicates.
Every observed trial-swap and revert write/erase boundary is interrupted, resumed
and checked for the permitted authenticated image. The variable-sector host
simulator and emulated counter are explicitly separate from ESP32-S3 configuration,
protected state and physical power-loss qualification. Reproduction and evidence
limits are in [the protocol guide](../tools/boot/README.md#native-boot-state-compatibility-with-staging).

## Actual native bootutil tests

`tools/boot/qualify.py` downloads checksum-verified MCUboot 2.4.0 at
`6d3b3d2c38ab20c242e5b9abb04d050086383eb2` and the official Mbed TLS 3.6.7
release. It builds the actual C bootutil through the upstream Rust simulator,
not a replacement verifier. Rust 1.90.0 and the simulator's complete reviewed
89-package lockfile are fixed. Host-only ring 0.17.14 and env_logger 0.11.8 replace
affected/unmaintained dependencies; the small ring API adaptations and the
correct `uint32_t` capability-bitmask ABI are reviewed in `tests/boot/simulator.patch`.
No production HAL bridge or MCU dependency graph changes.

The native profile enables RSA-3072/PSS/SHA-256, primary-slot validation,
scratch swapping and the simulated nonvolatile rollback-counter interface.
Six additional witnesses explicitly assert the real native capabilities and
exercise valid/invalid primary signatures; post-signature header, body and
protected-counter corruption; invalid-candidate fallback; trial reversion
without counter advancement; and confirmation followed by counter advancement
and rejection of an older signed image. Upstream swap/interruption and missing/
insufficient-security-counter controls also execute. Some of the upstream 25
test functions intentionally return early for other features; their aggregate
count is not 25 applicable policy proofs. Required named cases are checked,
so zero, filtered, failed or ignored policy tests cannot produce a report.

The qualification runs a fresh independent RustSec audit before compilation,
rejecting vulnerabilities, informational warnings and configured exceptions.
The original upstream lockfile must fail the same scanner for the actual
ring advisory RUSTSEC-2025-0009 before the maintained graph is admitted. SDK
archives, local patch/witness/lockfile and feature identities are recorded in a
public JSON report. This unsigned report is host execution evidence, not a
release attestation, product SBOM, FIPS validation or native advisory closure.

Requirements: Python 3.12+, Rust 1.90.0, cargo-audit 0.22.2, `patch`, native C
compiler and HTTPS access. Use a new disposable directory:

```sh
python3 tools/boot/qualify.py --work /tmp/nxrs-boot-native
```

The tool never overwrites an existing work directory. Its minimal environment
uses private Cargo/home/build state; the installed Rust toolchain and native
compiler remain host dependencies, not a hermetic toolchain. Tool groups are
retired on timeout. Only `qualification.json` is uploaded in CI. Downloaded
SDKs include public upstream example keys used solely for simulator fixtures;
no key material, source tree, raw log, binary or simulated flash is uploaded.
No example key may become a product trust anchor.

## Target evidence matrix

| Target | Current nxrs evidence | Production qualification still required |
| --- | --- | --- |
| ESP32-S3 camera/stream | Exact NuttX packaging source assessed; separate host RSA-3072 MCUboot policy/recovery qualification | Actual target bootloader/crypto build, authenticated ROM chain, signed target/layout admission, protected counter, flash/update ownership and physical interruption/recovery |
| RP2350 / Pico 2 | Entropy-source software/build evidence tracked separately in #35; this simulator does not qualify RP2350 boot | Independent ROM/image-format and NuttX boot-chain assessment, supported signing/rotation/counter policy, target builds and physical qualification |

The [offline application authority](../tools/boot/README.md) supplies encrypted
key custody, explicit target/layout signing, authenticated issuance/recovery and
host image verification. Its fresh-key native witnesses build on this baseline.
Current firmware profiles do not consume that policy or implement device update
admission/confirmation; production acceptance below remains open.

## Required target work

The ESP32-S3 standalone port in the selected upstream revision uses Espressif
HAL/ESP-IDF 5.1.6, matching the existing NuttX third-party revision. Its Mbed TLS
ECDSA path is unsupported; qualifying RSA-3072 avoids selecting the TinyCrypt
ECDSA path by assumption. This is a candidate verification profile, not an
instruction to replace current firmware. Actual target SDK configuration,
patched crypto-library integration, flash geometry and bootloader size must be
built and qualified separately. Native MCUboot/Mbed TLS/Espressif/NuttX
advisories and the exact compiled crypto surface still require review.

Production acceptance additionally requires:

- An offline, encrypted and target-scoped application-signing authority, a
  separately governed ROM bootloader trust root, bounded signed target/layout
  metadata, explicit security counters, release inventory and key-rotation/
  compromise recovery. Never silently accept the SDK's sample signing key.
- An actual NuttX update writer with typed Rust ownership, quiescent flash
  access, exact geometry/budgets, authenticated admission before selection,
  interrupted-write recovery and explicit post-startup confirmation.
- A trustworthy device counter implementation and physical reset/interruption
  tests. The host counter is thread-local simulated state, not protected NVM.
  Test flash geometry is an upstream fixture, not the ESP32-S3 board's layout.
- Verified ROM-to-bootloader-to-application chaining, entropy and key custody,
  physical debug/recovery policy, manufacturing preflight and read-only evidence
  before any permanent operation. ESP32 secure-boot/flash-encryption first boot
  can burn eFuses automatically; compiling a secure configuration must never be
  treated as permission to flash or boot it. Virtual eFuses are development
  evidence only. No eFuse, debug lock or production rollout is authorized here.

Primary references: [MCUboot Espressif port at the selected revision](https://github.com/mcu-tools/mcuboot/blob/6d3b3d2c38ab20c242e5b9abb04d050086383eb2/docs/readme-espressif.md),
[security counter design](https://docs.mcuboot.com/design.html),
[upstream security advisories](https://github.com/mcu-tools/mcuboot/security/advisories),
[Mbed TLS 3.6.7 release and security fixes](https://github.com/Mbed-TLS/mbedtls/releases/tag/mbedtls-3.6.7).

## Native input inventory and current advisory scope

The target build now uses the exact minimal verification-only Mbed TLS header
and emits a schema-validated artifact-bound native input SBOM and unsigned build
record. Fresh official MCUboot/Espressif/Mbed TLS feeds are checked against the
expiring exact-source/configuration/function review. See the
[native evidence procedure](../tools/boot/README.md#minimal-crypto-and-native-build-evidence)
for scope, eight actual artifact/bundle refusal controls and remaining coverage.
NuttX/application, compiler/vendor support, physical ROM, protected counters,
trusted release attestation and full product qualification remain open.

## Rust device admission protocol core

[Image admission qualification](firmware-image-admission.md) describes the
allocation-free Rust protocol core and real native-crypto signed-policy fault
controls. Stock bootutil authenticates the custom TLV without enforcing nxrs
manifest semantics. This core is not yet connected to a target update/boot
decision, and its caller-supplied floor is not evidence of protected rollback
state. Device activation and physical recovery gates remain open.
