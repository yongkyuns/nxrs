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
| ESP32-S3 camera/stream | Actual sole-application-root MCUboot/crypto build and policy-bound NuttX images; Rust image admission/staging with emulator execution; production bootloader and signed executable corpus in ESP32-S3 QEMU; offline ROM artifact/preflight qualification | Physical ROM trust and root custody, full boot stack/watchdog/flash ownership, protected counter, production update/activation integration and physical interruption/recovery |
| RP2350 / Pico 2 | Entropy-source software/build evidence tracked separately in #35; this simulator does not qualify RP2350 boot | Independent ROM/image-format and NuttX boot-chain assessment, supported signing/rotation/counter policy, target builds and physical qualification |

The [offline application authority](../tools/boot/README.md) supplies encrypted
key custody, explicit target/layout signing, authenticated issuance/recovery and
host image verification. Its fresh-key native witnesses build on this baseline.
Qualification profiles consume the public policy/configuration in builds, and
Rust admission/staging qualifies authenticated secondary writes. Production update
endpoint and activation/confirmation integration remain
open; caller-supplied admission floors are not protected device counters.

## Required target work

The ESP32-S3 standalone port in the selected upstream revision uses Espressif
HAL/ESP-IDF 5.1.6. This bootloader graph is distinct from the frozen NuttX
application SDK: NuttX selects esp-hal-3rdparty `b90b1837cb5ad24747deb4c895246037cc206ce5`,
whose headers identify ESP-IDF 6.1.0 and whose Mbed TLS submodule is
`582ff482038db6e4010dbf6f943d97b05ad06ea5` (4.0.0 version macros). These
version macros do not establish release/support or security equivalence. See the
[frozen application SDK version header](https://github.com/espressif/esp-hal-3rdparty/blob/b90b1837cb5ad24747deb4c895246037cc206ce5/components/esp_common/include/esp_idf_version.h)
and [frozen application Mbed TLS version header](https://github.com/espressif/mbedtls/blob/582ff482038db6e4010dbf6f943d97b05ad06ea5/include/mbedtls/build_info.h).
Wi-Fi uses its separately namespaced vendor crypto graph. Boot crypto evidence must
not be reused to qualify device TLS or entropy. The boot port's Mbed TLS
ECDSA path is unsupported; qualifying RSA-3072 avoids selecting the TinyCrypt
ECDSA path by assumption. This is a candidate verification profile, not an
instruction to replace current firmware. The actual target SDK configuration,
minimal crypto-library integration, flash geometry and bootloader size now have
separate build evidence. The source/configuration-scoped native advisory review
does not establish complete product coverage or physical qualification.

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
manifest semantics. The Rust staging owner authenticates exact containers before
secondary writes. Production endpoint and boot-policy integration, protected
rollback state, activation/confirmation and physical recovery remain open.


The image core supplies bounded streaming policy checks to the production boot
callback described below. They share the full-authentication parser and use actual
SDK incremental SHA, but return no signature authority; stock validation remains
mandatory after policy continuation. Protected-floor integration and complete
boot-time resource qualification remain open; see the
[streaming policy contract](firmware-image-admission.md#bounded-streaming-policy-checks).

## ROM-signed bootloader artifacts

The [ROM request/assembly procedure](../tools/boot/README.md#rom-signing-requests-and-verified-artifacts)
prepares a digest request for a protected external signer and assembles its
RSA-3072/PSS response with the pinned vendor encoder. The actual ESP32-S3 image
format/checksum/digest, build/policy/root pins, complete embedded hardware-key
digest, canonical signature sector and reserved size must match. Coherent-CRC
embedded-key mutations must be refused even though the vendor's supplied-key
host signature check can accept them.

Target qualification invokes the complete request/assembly/verification CLI on
its real compiled bootloader and supplies that artifact to the same fuse-state
preflight. Fixture private keys stay in memory and sign only disposable
qualification artifacts. No tool in this workflow connects to or resets a board.
A report may set `rom_signed_bootloader_verified` for the host-checked artifact;
`rom_execution_qualified`, signing-key custody and physical/deployment acceptance
remain false. Real protected-root governance, ROM execution, manufacturing
ordering, boot policy/counters and physical recovery are still required.

## Actual host loader policy callback

The production `driver/mcuboot-policy` callback is exercised through the actual
host MCUboot loader, as described in [the integration contract](mcuboot-policy-hook.md).
A correctly signed wrong-manifest image and a valid image in a shifted actual
SDK area boot without the hook; enabling it refuses both without advancing the
simulated counter. Valid-image, stock RSA/PSS/floor, compiled-root restoration,
trial/revert and invalid-secondary fallback controls are mandatory in the
signing workflow, in addition to the unchanged upstream recovery baseline.

The ESP32-S3 cross-build now links the production callback and actual SDK
bindings, binds the independently approved policy and includes the five-component
Rust source/compiler closure in its native inventory. Its roughly 49 KiB unsigned
fixture image becomes a 57,344-byte ROM-signed artifact, fitting the existing
61,440-byte reserve; exact sizes are recorded per policy/build. The actual windowed callback has a 5,376-byte frame; this is not a full
boot-stack bound. The product flag is mapped to actual SDK watchdog enablement and both effective
definitions plus the 9,000 ms timeout are required. Missing mapping is reproduced
and refused with the real SDK unit before restoration. Successful bounded
policy progress feeds it. The separate `--execute` qualification now observes the actual callback,
stock authentication and signed Rust application entry/write in ESP32-S3 QEMU
across 32 cases, including the exact maximum signed-image budget and a
corrupted maximum image. The producer refuses one extra byte. All cases have
bounded observed stack-write guard checks, including actual guard corruption
and restoration controls; these do not prove a full stack bound. The pinned
emulator lacks the RTC watchdog model, so it cannot establish a stalled-boot
reset. The stored protected-floor case is explicitly skipped on this
target; emulator state is not protected rollback. Full stack/watchdog/flash
ownership, protected rollback, physical ROM boot and recovery, Rust toolchain
advisory closure and trusted production releases remain open. Cross-linking alone
does not close those gates.

The actual watchdog-enable correction is paired with NuttX patch 0092: startup
uses the existing SDK unlock/disable/relock operations before continuing. The
public handoff record executes the exact pinned source function under an explicit
protected-register host model, with original/reordered refusal and restored
idempotent success. The full Linux NuttX build provides the separate target-header
compilation/linkage gate. Neither proves physical watchdog reset or handoff timing.

The Linux target pipeline additionally signs and executes the actual just-built
NuttX payload through the production MCUboot, with unsigned/wrong-target/bad-signature
refusals. The read-only debugger oracle binds loaded code bytes and actual idle
stack to the final ELF, then requires native startup and watchdog handoff through
the next flash initialization call. This is recorded separately as
`signed-nuttx-qualification.json`. It precedes later peripheral/console setup;
full kernel/application, USB/external RAM, physical watchdog and recovery gates
remain open. No ELF or accepting guest code is injected to satisfy the check.


## NuttX application native compiler inputs

The policy-bound application build requires actual target C/C++/preprocessed-
assembly invocations through a private capture tool. The real compiler first
emits dependency names; their bytes are hashed before actual compilation and
compared with the final dependency set and bytes afterwards. Failed compilation,
changed compiler/producer/input/object, undeclared source origins, empty records
and duplicate output records are refused. Compiler caching is disabled for this
qualified native build; the real linker and existing ABI gate remain unchanged.

`application-native-inputs.json` binds the compiler-input superset to the actual
NuttX ELF and public application build/configuration record. It contains public
source identities/hashes and compiler/object/command digests. Absolute paths, raw
commands, source contents, keys, SDK fixtures and images remain private.
This applies only to the secret-free policy-bound qualification build.

The ledger does not establish retained-link coverage, a complete firmware/Rust
inventory, prebuilt radio/ROM/OS/toolchain coverage, advisory closure, trusted
release attestation, physical qualification or deployment permission. Those flags
remain false. The full exact NuttX build must execute this gate; host compiler
controls alone are not target evidence.
