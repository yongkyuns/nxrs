# Device identity key and CSR software qualification

`driver/device-identity` owns a volatile P-256 signing key and generates a public
SHA-256 CSR through Mbed TLS 4.1.1 and its actual bundled TF-PSA-Crypto 1.1.1.
The official archive is checksum pinned in
[the dependency record](../security/device-identity-dependencies.json). This
application crypto graph is separate from the existing bootloader and vendor
radio crypto graphs. No existing camera profile acquires crypto readiness here.

The platform supplies a cryptographic `Entropy` implementation. It must enforce
the source's hardware/resource conditions, bounded reads, full output and failure
policy. There is no default device source. The callback feeds the SDK's maintained
HMAC-DRBG/SHA-256; it does not implement a new random algorithm. An unavailable
source clears partial output and latches failure. `refresh_entropy()` requires a
fresh seed and treats failure as terminal even though the SDK may retain usable
cached DRBG state. Automatic reseeding remains bounded by 1,024 SDK requests.
The owner must explicitly refresh after supported resume/cloning events before
private operations; no automatic VM-image or hardware-cloning protection is claimed.

Rust owns admission, buffers, the key handle, callback source, error latch and
retirement. One owner holds the SDK global-state lease. It is neither Send nor
Sync. A thread-local pointer borrows stable source storage only during each
synchronous SDK operation and is cleared before return. Foreign/late callbacks
cannot dereference retired storage. The SDK build enables no background work or
threading. Native glue only obtains actual header layouts/constants and invokes
inline attribute setters. Borrowed PK contexts retire before the PSA key and
DRBG; the source remains owned through native teardown. The SDK ownership lease
is released only after the source itself has finished retiring.

The key permits signing with ECDSA/SHA-256, has volatile lifetime, and denies
private export. This is software policy: privileged memory access, physical key
isolation, persistence, power-loss/reset recovery and protected custody are not
qualified. No private key is serialized by this API. CSR subjects are advisory;
the existing offline issuer assigns site/camera/gateway identity from its approved
catalog. Device identity authorization and physical enrollment remain separate.

## Reproduce the actual SDK and issuer checks

Use the existing hash-pinned target tool runtime from `tools/boot/install_target.py`
(Python 3.12, CMake 3.31.10, cryptography 50.0.2) and Rust 1.90.0. Then run:

```sh
python tools/device-identity/qualify.py --work /absolute/fresh/private/directory
```

The recipe downloads and verifies the official archive, validates contained
members/links, and compiles the exact config twice. Both ordinary and native-C
ASan/UBSan variants execute the production Rust implementation and real SDK.
Intentional native faults must activate each sanitizer. Rust memory instrumentation
is not claimed. Compile controls prove the actual public owner rejects Send/Sync.

SDK controls cover real CSR generation, ownership/reacquisition, denied private
export with an actual exportable control, initial source loss, partial output,
automatic reseed failure, explicit refresh failure, sticky refusal, invalid input,
capacity, unsupported SDK requests, and foreign/late callbacks. Two independent SDK-generated keys/CSRs per
variant must be distinct, parse with valid signatures in cryptography, and enroll
through the actual scoped issuer. Reused keys and corrupted signatures refuse
without changing issuer state. Public-key fingerprints must also be distinct
across both independently executed processes. No statistical test is treated as
entropy evidence.

Only `qualification.json` is publishable: source/artifact hashes, counts, public
CSR/public-key hashes and scope flags. SDK source, raw commands/logs, CSRs, issuer state,
synthetic authority keys and native control buffers remain private. Test diagnostics
must not print sample or private-key bytes. Ordinary root tests compile this
SDK integration but do not execute these native witnesses; the qualification
recipe requires every named SDK test, so zero tests cannot satisfy it.

## Returned certificate binding

`Identity::bind_certificate` verifies a returned DER camera certificate against
its still-live volatile key. Supply the independently provisioned issuer's DER
certificate SHA-256, expected site/camera identifiers and private/loopback IPv4
address, and a fresh trusted UTC observation. Untrusted inputs cannot select a
new issuer or time source. The API does not acquire trusted time or a protected
generation floor on the caller's behalf.

Rust bounds each DER input to 2,048 bytes and requires one complete certificate,
checks the pinned root, CA path constraint, leaf key usage, sole server-auth EKU,
exact camera URI/IP SAN set, root/leaf validity and maximum 30-day leaf lifetime.
The actual SDK verifies the P-256/SHA-256 chain without a flag-waiving callback;
Rust compares the leaf's public SPKI to the held key's actual SDK public SPKI.
The SDK time callback borrows only the call's supplied trusted UTC, retires on
success/error and refuses unscoped or foreign-thread reads. It has no ambient
clock fallback. Native code translates only actual header fields, constants,
layouts and the SDK callback's `time_t` ABI.

The returned `CertificateBinding` borrows the live key owner. Access requires an
unpoisoned owner and a monotonic lease bounded by the certificate/root expiry and
five minutes. Invalid certificates do not destroy a healthy key, so a corrected
issuer response can be checked; entropy failures retain the owner's sticky
refusal. Private key material is never exported for this comparison.

**This binding is not transport authorization.** It does not check fresh CRLs,
verify a signed enrollment-bundle generation against protected state, establish
clock trust, install credentials or enable TLS. Those remain mandatory before a
production connection. Host SDK qualification executes real issuer-generated
certificates, 21 signed/malformed refusal variants, all proper DER prefixes,
policy mismatches, monotonic expiry and an actual entropy-failure close. Compiled
controls reject Send/Sync and key retirement while a binding is borrowed. Its
trusted UTC fixture is deliberately in 2030 to exercise the SDK callback rather
than host wall time. The NuttX key/CSR witness does not yet execute this new
certificate API; `returned_certificate_runtime_qualified` remains false.

## NuttX SDK/Rust ABI qualification

The separate `esp32s3-identity-sdk` product profile selects only the pinned SDK
build recipe, with RF, ADC, networking, PSRAM, SMP and the NuttX-apps Mbed TLS
package disabled. The recipe uses the actual configured NuttX headers and real
Xtensa compiler, captures SDK and helper compilations through a checked compiler
launcher, and stages SDK archives into NuttX's ordinary application link group.
The actual Rust SDK entropy callback is explicitly retained through the
intermediate Rust garbage-collection link, before native SDK archives resolve it.
Real Xtensa GNU controls prove that a later-SDK callback is lost without this
root and resolves when retained. Dependency probes omit object-output arguments
and must preserve the real compiled object byte-for-byte. Host controls link
and execute the captured object; a separate actual Xtensa capture/link control
protects GCC behavior. SDK archive indexes, required functions and the custom
entropy callback import must be present before publishing a compile-only record. The existing real libc/header ABI gate remains
mandatory. No production entropy HAL
or device TLS factory is selected by this qualification profile.

On disposable Linux x86-64 with the hash-verified tools installed by
`tools/install-qemu-tools.sh` and the isolated target Python runtime, run:

```sh
python tools/device-identity/qualify_nuttx.py --work /absolute/fresh/private/directory
```

The runner requires a clean checkout. It builds **two independent images**, each
with a fresh 4,096-byte private host-OS entropy fixture. One fresh QEMU kernel per
image exercises the production Rust key owner and real SDK: initial entropy
refusal, exclusive admission on the owner thread and a real second NuttX thread,
a bounded admission result and completed thread join, two P-256/SHA-256 CSRs, forced-refresh failure,
sticky refusal with cleared output, and ownership recovery. The host independently
verifies all four CSR signatures and distinct public keys and enrolls them through
the real scoped issuer, including reused-key and corrupt-signature refusals.
The witness consumes fixture bytes once and clears its mutable copy. The immutable
fixture remains inside the private image; this is not secure seed erasure.

A passing run uploads only the bounded public `qualification.json`. It binds reviewed source,
compiler inputs, SDK libraries, the real libc ABI report, final ELF/flash hashes,
fixture hashes, CSR/key hashes and exact scope. **Do not publish or deploy these
images:** their embedded entropy fixtures can reproduce their keys. Raw fixtures,
images, SDK source, command lines, UART, CSRs and synthetic issuer state remain
private. With the project owner's explicit approval, a failed target run may
retain one encrypted diagnostic report for one day. It includes at most the last
8 KiB from each of four fixed build stdout/stderr files (32 KiB total), sealed
with RSA-3072-OAEP/SHA-256. Blocks bind checkout, logical file, index and count.
The private decryption key stays local and off-repository; only its public key
is checked in. Seed/image files and issuer state are excluded. This disposable
mechanism cannot emit a qualification pass or serve as production key storage.
Rebooting one fixture image does not provide fresh entropy and cannot
count as a restart-uniqueness test. The recipe requires actual UART execution,
NuttX identity, NSH success and issuer verification before emitting a pass report;
its oracle unit fixtures do not constitute target execution evidence.

This profile qualifies SDK/Rust ABI behavior only when that exact target run
passes. It establishes no onboard entropy quality, SAR/RF ownership, protected
key custody, device TLS, general camera-image readiness, whole-product advisory
closure, physical operation, FIPS validation or deployment permission.

## Remaining production gates

Implement and qualify the actual MCU entropy resource lease and production
SDK/header/link integration before device key generation. Enforce RF/ADC ownership and startup,
radio-sleep/restart, bounded failure, reseeding and callback teardown conditions.
Implement protected credential custody, certificate matching/trust/time/epoch,
rotation/revocation/reset/transfer/decommissioning and authenticated device TLS.
Qualify exact target ABI, runtime resources, physical startup/recovery and release
integration. Existing host issuer or SDK results cannot satisfy these gates.

Fresh whole-product/native advisory coverage and trusted release attestation
remain open. Review [official Mbed TLS advisories](https://mbed-tls.readthedocs.io/en/latest/security-advisories/)
against the exact SDK, compiled mechanisms and compiler. The selected release
includes the [ECC side-channel fix](https://mbed-tls.readthedocs.io/en/latest/security-advisories/mbedtls-security-advisory-2026-07-ecc-optimized-modp-side-channel/).
The [compiler optimization advisory](https://mbed-tls.readthedocs.io/en/latest/security-advisories/mbedtls-security-advisory-2026-03-compiler-induced-constant-time-violations/)
requires build-specific assessment; a version pin is not an advisory-clean verdict.
The source graph and configuration above are not FIPS validation. The general
IoT/camera baseline has no known FIPS requirement. Track original acceptance in
[#35](https://github.com/yongkyuns/nxrs/issues/35), [#33](https://github.com/yongkyuns/nxrs/issues/33),
[#38](https://github.com/yongkyuns/nxrs/issues/38) and [#32](https://github.com/yongkyuns/nxrs/issues/32).
