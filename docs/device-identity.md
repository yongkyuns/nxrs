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
than host wall time. The NuttX recipe below executes this same certificate API with a live target key;
its runtime flag may become true only after actual console and issuer evidence
passes for that exact image. Host controls alone cannot satisfy it.

## In-memory revocation admission

`RevocationState::from_trusted_floor` takes the independently provisioned issuer
pin and an explicit trusted minimum generation (1 through 1,000,000,000).
`observe` verifies the issuer's complete DER CRL before changing any state.
`Identity::bind_current_certificate` accepts that update first, then performs
all the key/chain/scope/time checks above and refuses a revoked leaf. A failed
leaf admission cannot suppress a valid revocation update. The resulting
`CurrentCertificateBinding` borrows both the key owner and state. Every DER,
generation or remaining-validity access rechecks owner health, certificate/CRL
monotonic expiry, the generation floor and revocation. A newer accepted CRL
invalidates old guards, including guards for leaves that remain unrevoked;
callers must repeat admission against that generation.

Rust preflights canonical definite DER before native CRL allocation: at most
32,768 bytes, 512 unique positive serials of at most 20 encoded bytes, a bounded
issuer name and no entry extensions. The exact issuer profile requires v2,
ECDSA-with-SHA256 in both algorithm fields, CRLNumber and the root's 20-byte
AuthorityKeyIdentifier as the only, noncritical extensions. Delta, indirect,
unknown and missing scope selectors refuse; this implementation cannot treat a
partial CRL as a complete list. The actual Mbed TLS parser still parses admitted
CRLs and provides dates/entries; Rust cross-checks its signed body, signature,
serials and complete input consumption. TF-PSA-Crypto verifies the signature
against the pinned root's P-256 key. CRLs must have thisUpdate at or before the
supplied UTC, at most 24 hours old, and nextUpdate in the future and within
24 hours of thisUpdate and the root expiry. Revocation dates cannot follow
thisUpdate. This device profile is deliberately stricter about future clock
skew than the offline bundle verifier.

Only authenticated updates can affect state. Older generations refuse without
changing a healthy cache. Re-signing identical signed contents at the same
generation is idempotent and never extends the first monotonic deadline, even
after expiry. An authenticated different body at the same generation, a newer
generation with an earlier thisUpdate, or removal of an already observed
revocation permanently poisons this cache and all its guards. This follows the
issuer's complete, append-only revocation history. A higher externally acquired
trusted floor can invalidate guards immediately; lower floors cannot reopen
them. A rejected unsigned mutation cannot poison the revocation cache.

These are **in-memory guarantees**, not protected persistence. The caller must
acquire fresh trusted UTC and a protected generation floor, durably reserve an
accepted generation before production use, and preserve issuer state across
reset, ownership transfer and recovery. One possible integration orders
`observe`, protected persistence of its returned generation, verification of the
persisted floor, and admission against that same CRL. Constructing a fresh cache
loses its observed history and cannot detect pre-restart equivocation without
additional trusted state. There is no default floor, wall-clock or file-storage
fallback, and this API does not authorize transport by itself.

The host SDK recipe executes the actual issuer's durable exports, refresh and
revocation, a 512-entry positive boundary, 19 signed/malformed negative cases,
every proper CRL prefix, a bit mutation at every byte, same-body re-signing,
revocation before leaf refusal, floor advancement, authenticated equivocation,
time/history regression, monotonic expiry and actual entropy failure. Compiler
controls reject Send/Sync and retirement of either borrowed owner or state.
There are 15 required SDK controls per ordinary/sanitized variant. The NuttX
recipe below separately executes this CRL API. Its `revocation_runtime_qualified`
flag can become true only after both fresh kernels complete all required CRL
controls and the host independently verifies every signed issuer export and
control, the exact source bindings and NSH success. Ordinary host SDK reports
keep that runtime flag false. Native C sanitizer coverage does not claim Rust
memory instrumentation or physical qualification.
The standard fields are specified in [RFC 5280 sections 5 and 6.3](https://www.rfc-editor.org/rfc/rfc5280.html#section-5).

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
with a fresh 4,096-byte private host-OS entropy fixture and a separate disposable
issuer. The issuer's public DER root, SHA-256 pin and trusted UTC fixture are
fixed before compilation and included explicitly in the witness; its private
key/password stay on the host. The UART cannot choose a new root or time.
One fresh QEMU kernel per
image exercises the production Rust key owner and real SDK: initial entropy
refusal, exclusive admission on the owner thread and a real second NuttX thread,
a bounded admission result and completed thread join, two P-256/SHA-256 CSRs, forced-refresh failure,
sticky refusal with cleared output, and ownership recovery. The key owner remains alive while the host independently verifies its CSR and
returns three bounded DER certificates from the actual scoped issuer: matching
key, correctly signed wrong key, and a ten-second expiry control. The target
executes the production certificate binding, thirteen signature/key/scope/time/
framing refusals per key, monotonic expiry and poisoned-owner admission refusal.
The two images must bind four certificates, refuse 52 invalid inputs, execute
four expiry controls and refuse two poisoned admissions. It records per-key
verification duration and waits out the measured remaining short-certificate
lease before requiring refusal; verification time is included in that lease. The host independently
verifies all four CSR signatures/distinct keys, returned signatures/public keys,
wrong-key controls and actual reused-key/bad-signature issuance refusals.

For the same live keys, UART also supplies six bounded CRLs each: actual durable
issuer exports before/after refresh and revocation, a re-signing of identical
TBS contents, a signed conflicting generation and a ten-second expiry control.
Each target key admits five current bindings, rejects fourteen inputs/state
transitions, invalidates three old guards, refuses a revoked leaf while
accepting its valid update, closes after authenticated equivocation, and refuses
to revive an expired guard by replaying the same signed CRL. Unsigned signature,
prefix, trailing-byte and wrong-pin inputs leave healthy guards usable. A raised
trusted-floor fixture invalidates an old guard and a lower floor refuses. The
initial floor is the qualification issuer's fixed generation 1, compiled into
the witness; UART cannot choose trusted state. After actual entropy failure,
revocation-aware admission also refuses. Two independent images require 20
current bindings, 56 refusal controls, 12 invalidations, four each of revoked,
equivocation, CRL-expiry and expired-replay controls, and two poisoned-owner
admission refusals. The host independently verifies 24 CRLs, exact generations,
revoked serial/date sets, signatures, issuer/AKI and the actual signed bundles.
The small target fixtures do not qualify the maximum 512-entry memory/CPU
workload; that boundary is tested in the host SDK recipe. Public reports retain
that scope and keep protected-generation/time-acquisition/custody/physical flags
false. Actual NuttX ABI execution with host fixtures is separate from production
MCU qualification.

The witness consumes fixture bytes once and clears its mutable copy. The immutable
fixture remains inside the private image; this is not secure seed erasure.

A passing run uploads only the bounded public `qualification.json`. It binds reviewed source,
compiler inputs, SDK libraries, the real libc ABI report, final ELF/flash hashes,
fixture hashes, public CA/pin/time fixture hashes, CSR/key/certificate hashes and exact scope. **Do not publish or deploy these
images:** their embedded entropy fixtures can reproduce their keys. Raw fixtures,
images, SDK source, command lines, UART, CSRs and synthetic issuer state remain
private. The temporary encrypted diagnostic export approved for draft #71 was
retired after its build failure was resolved and the exact target qualification
passed. Normal CI uploads no private diagnostic data.
Rebooting one fixture image does not provide fresh entropy and cannot
count as a restart-uniqueness test. The recipe requires actual UART execution,
NuttX identity, NSH success and issuer verification before emitting a pass report;
its oracle unit fixtures do not constitute target execution evidence.

The qualification-only UART uses length-delimited DER (at most 2,048 bytes),
32-byte hex payload lines and at most 128 bytes per host write. Pacing prevents
emulator RX overrun; it is not a product transport or application deadline.
Console collection is bounded to 256 KiB and each interactive wait to 60 seconds,
inside the existing 150-second process-group execution deadline. Successful NSH
status, every binding/refusal/expiry marker, independent issuer checks and
unchanged compiled public fixtures are mandatory before a pass record.

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
