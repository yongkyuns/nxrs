# Offline application signing

This is the software signing increment for [#34](https://github.com/yongkyuns/nxrs/issues/34).
It supplies a fresh target-scoped RSA-3072 application authority, encrypted PKCS8
custody, authenticated issuance state, signed MCUboot images with protected
metadata, explicit host verification and retryable export. It does not qualify
the device update writer, ROM chain, protected counter or a physical board.
Current camera profiles and the firmware build frontend do not consume this
policy yet. No production boot or flash operation is provided by these tools.

## Operator environment and authority

Run the commands below from the repository root with CPython 3.12 in a
dedicated operator virtual environment. Install the complete
requirements with `--require-hashes --only-binary=:all:`; `pip check` must pass.
`audit_signer.py --audit-python /absolute/path/to/auditor/bin/python` uses
pip-audit 2.10.1 in a separate environment, audits all eight exact packages and
requires detection of the historical affected cryptography requirement without
installing it. The imgtool 2.4.0 wheel's 13 Python modules were compared
byte-for-byte to the selected MCUboot source before integration. Python,
cryptography/OpenSSL and other native wheel/runtime boundaries are distinct
from MCU cryptography and require release inventory/advisory review.

Keep the authority on a trusted offline local POSIX filesystem with private
directories, no delegated ACL access, working locks, hardlinks and directory
fsync. Every authority state/key file must be owned by the operator, private and
unlinked elsewhere. Absolute canonical directory paths are required; resolve
aliases such as `/tmp` before use. The authority and export directories must be
separate. Public settings/policy/image inputs may be readable by others; reads
still refuse symlinks, non-regular files, hardlinks and excessive sizes.

Prepare a reviewed settings JSON with exactly these fields:

- `schema`: 1; `format`: `mcuboot-rsa3072-pss-sha256`; a distinct `target` ID;
  `configuration_sha256`: the reviewed target build configuration identity.
- `layout`: explicit `flash_bytes`, `primary_offset`, `secondary_offset`,
  `slot_bytes`, `scratch_offset`, `scratch_bytes`, `erase_bytes`, `header_bytes`,
  `write_align`, `max_align` and `max_sectors`. Ranges must fit without overlap.
  The trailer budget uses the actual compiled bootloader's `max_sectors`, not
  an assumed sector count. This increment supports the unencrypted,
  uncompressed scratch-swap format with alignment 4/8 and max alignment 8.

No production geometry/configuration defaults are supplied. The native recipe
uses an explicitly named host K64f flash fixture. Its geometry/configuration is
not an ESP32-S3 or RP2350 board claim. Exact target bootloader/Kconfig/link-map
and update-agent integration remain required before production use.

Create an empty private authority directory and a separate private export
directory. For example, using canonical absolute paths and a trusted settings
file:

```sh
python tools/boot/sign.py init --authority /absolute/path/authority --settings /absolute/path/settings.json
```

Use a strong, unique passphrase of at least 32 bytes with independent recovery
custody. The tool reads a non-echoing terminal. Automation may pass a single
owned anonymous pipe on FD 3 or greater through `--password-fd`; it must close
the writer to provide EOF within two seconds. Password arguments, environment
variables, regular files, named FIFOs (including unlinked ones) and terminal
echo fallback are unsupported. Linux uses the kernel `/proc/self/fd` anonymous
pipe identity; macOS checks the qualified anonymous pipe device/link identity. In-memory
copies and erasure are not proven. Only encrypted PKCS8 private material is
written. The application authority is distinct from enrollment and ROM
bootloader roots; no sample key import/fallback or ROM signing API exists.

`init` prints the SHA-256 of `policy.json`. Preserve that pin through an
independent reviewed build/device trust configuration. Fetching a policy and
its untrusted hash together does not establish a trust root. The signed policy
contains the public application key, target, configuration and exact layout.
Do not publish the authority's encrypted state directory. An interrupted init
fails closed; quarantine an incomplete unused directory and initialize a fresh
authority rather than overwriting it.

## Signing and explicit admission

```sh
python tools/boot/sign.py sign --authority /absolute/path/authority \
  --policy-sha256 REVIEWED_POLICY_SHA256 \
  --input /absolute/path/nuttx.bin --payload-sha256 REVIEWED_PAYLOAD_SHA256 \
  --version 1.2.3+4 --security-counter 7 --output /absolute/path/exports/application.bin
```

The raw payload identity must match the reviewed hash. ELF/already wrapped
images and excessive sizes are refused. MCUboot imgtool produces the standard
RSA-3072/PSS/SHA-256 image. Its authenticated protected TLVs contain the explicit
security counter and an nxrs manifest (tag `0xa0`) binding policy, target,
layout/configuration, version and raw payload hash. Firmware payloads remain
plaintext. No confirmation flag or update activation is written.

Security counters are independent from application versions. Issuance counters
cannot decrease; a compatible recovery image may use an older application
version with the current allowed security counter. The private signed ledger
retains up to 256 immutable release records and signatures. A repeated request
reconstructs the exact previously signed bytes rather than generating another
probabilistic signature. This filesystem ledger does not resist privileged
rollback or restore of an old valid snapshot. Protected device counters and
governed authority backup/restore remain production gates.

Verification requires an independent policy pin, expected target/configuration
and explicit minimum security counter:

```sh
python tools/boot/sign.py verify --policy /absolute/path/policy.json \
  --policy-sha256 REVIEWED_POLICY_SHA256 --image /absolute/path/application.bin \
  --target REVIEWED_TARGET --configuration-sha256 REVIEWED_CONFIGURATION_SHA256 \
  --minimum-security-counter DEVICE_SECURITY_FLOOR
```

Verification bounds every header/TLV, rejects duplicate/unknown elements,
requires the exact signature/key/hash scheme, authenticates before trusting
metadata, compares all duplicated counter/version/target/configuration fields
and reserves the compiled trailer budget. The host minimum is caller-supplied;
the device must derive its real floor from protected state. Stock MCUboot
authenticates the protected custom manifest but does not enforce nxrs target/
layout semantics. A typed Rust update admission path and per-target compiled
key selection must enforce those semantics before activation. This host
verifier is not that device integration.

`public-key` accepts the same policy pin, expected target/configuration and an
`--output` path. It exports only the verified public SPKI PEM for future target
bootloader wiring. Never feed the encrypted authority/private state to a
bootloader build. Key rotation/revocation requires a qualified boot trust
transition and compromise/recovery procedure; creating another authority alone
does not update or revoke device trust.

## Interrupted operations

The signed ledger reservation and its directory durability barrier finish
before export. A complete `.state-next` journal blocks issuance until explicit
authenticated `recover-state --authority ... --policy-sha256 ...`. Valid next
reservations are promoted, never discarded. `--discard-incomplete` is only for
an unauthenticated/incomplete uncommitted journal after operator review; an
authenticated inconsistent ledger remains a failure. Passphrases use the same
terminal/private-pipe channel. No image export occurs while its authority journal is unresolved.

An export uses a private content-addressed `.nxrs-release-<image-hash>` staging
name, fsync, atomic no-replace link publication and parent-directory fsync.
Retrying the same request can reuse a verified complete stage, reconstruct an
exact incomplete unlinked prefix, or retire the exact two-link alias left after
publication. Unrelated hardlinks, foreign/corrupt stages and different existing
outputs are preserved and refused. Failed exports retain the durable issuance
reservation; retry with the same body/version/counter and an appropriate
private output path. Old copied artifacts cannot be erased by advancing a
ledger or counter.

## Qualification scope

`test_sign.py` exercises actual crypto, hostile policy/image inputs, descriptors,
issuance ordering, failure controls and real process-group kills before/after
ledger replacement and atomic publication. `qualify_signing.py --work NEW_DIR`
first executes the complete pinned boot qualification, then creates fresh
encrypted authorities and compiles only the selected public key into a
separate real bootutil build. Native witnesses require acceptance of the issued
image and refusal of another fresh authority, the SDK sample key, modified
image bytes and a counter below simulated protected state.

Only public unsigned host-scope reports are retained in CI. No authority state,
password, SDK source/example keys, image binary, raw log or simulated flash is
uploaded. Reports explicitly deny target/physical qualification, device target/
layout enforcement, native advisory closure and FIPS validation. Actual target
builds, trustworthy counters, typed Rust update/write/confirmation ownership,
ROM authentication, rotation/manufacturing/debug policy, physical power-loss
tests and release provenance/distribution remain open under #34/#38. No eFuse,
debug lock, first boot, flashing, production deployment or release merge is
performed by this increment.

## ESP32-S3 build-only integration

The `esp32s3-camera-mcuboot` profile extends the existing physical Freenove
ESP32-S3 camera wiring and adds an explicit qualification-only MCUboot layout.
It preserves development console/debug access. It is not a production profile:
ROM authentication, protected rollback state, Rust device update/admission and
confirmation ownership, native advisory closure, debug/manufacturing policy and
physical interruption/recovery qualification remain open. The RP2350 has a
separate boot assessment. No FIPS validation is claimed.

Use CPython 3.12 and a fresh private output directory:

```sh
python3 tools/boot/install_target.py --out /absolute/new/target-tools
export NXRS_BOOT_PYTHON=/absolute/new/target-tools/venv/bin/python
export PATH=/absolute/new/target-tools/venv/bin:$PATH
cargo +1.90.0 firmware --platform esp32s3-camera-mcuboot \
  --emit-boot-plan --out /absolute/new/boot-plan
```

The installer verifies all hashes, installs the pinned wheel backend first and
builds only the selected esptool sdist with build isolation disabled. The complete
30-package target tool environment has a separate fresh audit:
`audit_signer.py --target --audit-python /absolute/auditor/bin/python`.
It must detect the actual affected historical requirement without installing it.
The installer and build retain private stage logs; their public output is bounded.

Review `boot-plan.json` and its `signer-settings.json` before initializing a new
offline application authority using the existing signer procedure above. Obtain
the resulting public `policy.json` SHA-256 from independent reviewed trust
configuration. Never infer trust by hashing an untrusted policy downloaded next
to a firmware image. The same policy settings gate both build paths:

```sh
cargo +1.90.0 firmware --platform esp32s3-camera-mcuboot --bootloader \
  --boot-policy /absolute/public/policy.json --boot-policy-sha256 APPROVED_SHA256 \
  --out /absolute/new/boot-build
cargo +1.90.0 firmware --app std-demo --platform esp32s3-camera-mcuboot \
  --boot-policy /absolute/public/policy.json --boot-policy-sha256 APPROVED_SHA256 \
  --out /absolute/new/application-build
```

The application command requires the existing qualified Linux Xtensa/NuttX SDK
and submodules. Source `target/qemu-tools/environment.sh` after the isolated
`tools/install-qemu-tools.sh` installation on a disposable Linux build host. The
normal backend's native-header/ABI and final-symbol gates remain active. A
writable live `/tftpboot` is refused before this build starts. No global compiler,
personal service or device configuration needs to change.

Boot builds download/checksum the exact MCUboot 2.4.0, Mbed TLS 3.6.7, ESP-IDF 5.1.6
HAL and GCC 14.2 archives into a private tree. They compile only the independently
approved RSA-3072 public key. A test-only target-header C witness measures the
actual key-structure layout; qualification reads the actual Xtensa ELF and
requires exactly one key, its length and the exact table pointers/key bytes.
The witness is never linked into the bootloader. Effective compiler definitions
must show RSA-3072, Mbed TLS, primary validation, scratch swap, TLV allow-list,
512 compiled sectors, alignment 8 and assertions. Software counter comparison
is enabled; this port has no hardware rollback provider. Release compiler flags,
source/profile/patch/tool identities, exact input hashes, Git head/dirty state
and binary hashes are recorded in public `boot-build.json`. This unsigned record
is not trusted release provenance or a complete native SBOM.

Only the SDK's fixed `getpub` and `elf2image` commands are admitted. Their exact
public key/input/output operands and geometry are checked before invocation;
serial/network opening paths are disabled defensively. These wrappers are not
a sandbox for hostile vendor code. ROM secure-boot, flash-encryption and virtual
eFuse flags are deliberately absent, since the SDK's first-boot paths can burn
permanent device state. There is no flash/eFuse/deploy command in this helper.

The application backend suppresses the old unsigned placeholder `imgtool sign`,
padding and preconfirmation hook. It preserves the linked Intel HEX, checks the
final resolved configuration against the approved layout, and exports the exact
vendor-interpreted raw `payload.bin` plus `payload-build.json` and the public
policy. Intel HEX address gaps use the vendor loader's erased-byte convention;
its base address is recorded. All three exports must exist after successful
completion; an interrupted private unsigned export is discarded and rebuilt in
a fresh output directory. It does not activate, sign or preconfirm the image.
Use the offline signer with the independently reviewed payload hash from that
build record; signed images remain plaintext. A valid signature is not device
admission: stock MCUboot does not enforce the nxrs custom target/layout manifest.

Run `test_target_plan.py` for actual crypto/policy, vendor HEX interpretation,
SDK-denial and Cargo frontend controls. `qualify_target.py --work NEW_DIR` creates
a fresh synthetic authority and executes the actual boot cross-build; `--nuttx`
also executes the full application frontend and verifies matching public build
records. Pinned weekly/PR CI exercises Linux and macOS boot builds and the Linux
NuttX build. Only the public JSON reports are retained; never upload authorities,
SDK/example keys, firmware images or private logs. A build or emulator cannot
establish actual flash timing, entropy, protected state or physical recovery.

## Minimal crypto and native build evidence

The target now supplies `mbedtls_config.h` through the SDK's existing CMake
configuration hook. Effective RSA compilation must expose exactly the nine
reviewed Mbed TLS modules, RSA/PSS and SHA-256. PKCS#1 v1.5, TLS, X.509, PSA,
DRBG and device entropy modules are disabled. The actual retained ELF functions
must match the reviewed public-verification set; private-key, decryption,
key-generation and unrelated protocol functions are refused. Enabled header
modules, compiled source files and retained functions are separate observations.
No private application key is compiled into this verifier.

`qualify_signing.py --minimal-crypto` first executes the existing upstream-key
policy/recovery suite with the exact target header, then builds a separate
fresh-authority verifier for the five interoperability/refusal cases. The
upstream example key is a test fixture only. These host runs do not qualify the
target allocator, flash controller, execution timing or protected device state.
The default host baseline still executes separately.

The complete target tool lock now has 30 packages, including independent
CycloneDX JSON-schema validation. All retain exact hashes and fresh advisory
qualification. The three official CycloneDX 1.6 schemas are hash-pinned; their
references resolve only from the local verified schema registry.

After the actual build, `native_inputs.py` reads Ninja's complete valid dependency
records and the real compiler commands, object files, linker inputs/scripts and
ELF functions. Unknown input roots, missing/stale/truncated dependency records,
other compilers/optimization modes, unexpected crypto modules and secret-key or
protocol APIs fail qualification. The current build records 81 compiled units,
427 files and seven linker scripts. The map's LOAD entries include inputs whose
sections were discarded. They are labelled as linker inputs rather than proof
of reachable code. Compiler archive hashes bind prebuilt support libraries;
SDK ROM address maps identify an ABI, while the physical ROM bytes/revision stay
explicitly unidentified.

The exact boot-only advisory gate reads all published
[MCUboot advisories](https://github.com/mcu-tools/mcuboot/security/advisories),
[Espressif advisories](https://github.com/espressif/esp-idf/security/advisories)
and the complete [official Mbed TLS index](https://mbed-tls.readthedocs.io/en/latest/security-advisories/).
Mbed TLS's empty GitHub advisory list provides no coverage. The reviewed
`security/boot-advisory-review.json` binds 106 source identities to the exact
source revisions, compiler, enabled modules, compiled SDK source set and retained
Mbed TLS functions. Fresh capture checks pagination and every main-index link;
unknown, changed, removed, duplicate, stale or unreviewed data is refused. The
review expires on its explicit date; updating a catalogue requires reviewing the
changed upstream advisory and the actual binary scope. Merely copying a new
fingerprint or extending expiry does not resolve a finding.

The compiler-related Mbed TLS advisory remains explicitly assessed against the
verified GCC/-Os build and the absence of secret-key/decryption operations. This
is not a general constant-time or physical side-channel proof. SDK radio,
provisioning and network advisories are excluded only for this exact boot source
set. This catalogue cannot authorize a NuttX application, TLS endpoint, radio
image, compiler lifecycle, vendor support or actual ROM revision. Those coverage
and support gates stay open; no blanket native-clean verdict or exception is
introduced.

A successful boot build also emits:

- `native-inputs.json`: exact native build inputs and explicit scope limits.
- `native-advisories.json`: fresh source/review identities and exact-build
  assessments, with uncovered product boundaries retained.
- `boot.sbom.cdx.json`: a schema-validated CycloneDX 1.6 build-input SBOM bound to
  the actual binary. The current graph has 512 components, including an opaque
  ROM component and explicit compiler/operator build tools.
- `boot.provenance.json`: an unsigned in-toto Statement/SLSA-v1-shaped record
  binding actual binary/ELF subjects, reviewed policy/configuration, source and
  recipe identities, inventory, advisory and SBOM hashes. No SLSA level or
  trusted publisher identity is asserted.

The build verifier recollects actual inputs and checks the actual artifacts,
then reconstructs the expected records. Bundle self-consistency alone is
insufficient. `qualify_target.py` additionally runs eight real refusal controls:
coherently rehashed SDK version, source-unit omission, header digest, compiler
identity, coverage/ROM/subject claims, and actual binary corruption. Its private
fixture is restored and reverified before
`boot-evidence-qualification.json` reports success. Normal boot builds perform
positive verification; corruption tests run only in the disposable qualifier.
The build input record identifies the supplied compiler/SDK artifacts, rather
than cryptographically proving all build derivation steps. Trusted release
attestation/distribution and full product/runtime inventory remain separate
gates.

Only these bounded public JSON reports and the existing public application
record are retained in hosted qualification. Authority state/passwords,
SDK/example-key source, image binaries, object files and raw stage logs stay
private. Failure diagnostics expose only an owned source filename/line, without
copying arbitrary tool output.
