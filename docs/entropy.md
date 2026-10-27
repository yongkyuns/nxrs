# Device entropy and failure handling

Status: #35 implementation/qualification work. No supported board is declared
cryptographically qualified by this document. Source trace is pinned NuttX
13.1.0 `2b5c0de5e933281c209f954a63b99cf10c132346` and Rust 1.90.0.
The product baseline is general IoT/camera use without a FIPS requirement.

## Source and consumer trace

| Consumer/path | Actual source behavior | Required trust condition / remaining gate |
| --- | --- | --- |
| Root firmware build | The common backend enables DEV_URANDOM; its presence alone does not select a cryptographic generator | Record the final resolved Kconfig for each image; reject xorshift/congruential and unqualified pool sources before enabling cryptographic consumers |
| Rust std on NuttX | `sys/random/mod.rs` selects `arc4random::fill_bytes`; this calls libc arc4random_buf, including for HashMap random keys | A Rust/std API name does not qualify its OS entropy source; preserve failures rather than manufacture random-looking output |
| NuttX libc arc4random_buf | First tries getrandom with GRND_RANDOM, then the default /dev/urandom path. The pinned source falls back to a clock hash after both fail | Patch 0091 removes predictable fallback: failure clears output and aborts because this API has no error result. Fallible cryptographic consumers must use a fallible source and propagate its error |
| NuttX getrandom | Opens /dev/random for GRND_RANDOM, otherwise /dev/urandom, then performs a read | Descriptor exhaustion, interrupted reads and missing devices can fail. They must not turn into a clock seed or success with an incomplete buffer |
| RP2350 architecture driver | Six hardware EHR words per sample, on-chip rejection status and a four-block startup check. The pinned collector can wait/reject indefinitely | Patch 0090 bounds hardware polling/rejections, rejects error+valid samples, clears partial output and latches source failure. Physical startup, time/quality and fault/recovery evidence remain open |
| ESP32-S3 architecture driver | Calls SDK esp_random for each output word under its driver mutex | True-random strength requires an active documented entropy source. The current file API does not expose or enforce that condition; do not qualify it from Kconfig or successful reads alone |
| NuttX software generator | DEV_URANDOM_XORSHIFT128 and CONGRUENTIAL are explicitly non-cryptographic; pool security depends on seed quality | Development-only use is not approval for identity keys, TLS nonces or DRBG seeds |
| Gateway token generation | Independent host workspace uses getrandom/OS randomness; initialization uses Python secrets | Keep host OS qualification distinct from MCU entropy; gateway tests cannot qualify a device source |
| Native radio/crypto SDKs | May consume their own SDK entropy callbacks independently of Rust std | Capture exact linked source/configuration and callback path when selecting device TLS. Do not assume a fix to libc covers every SDK consumer |

Relevant pinned NuttX files are `drivers/crypto/Kconfig`,
`drivers/crypto/dev_urandom.c`, `libs/libc/misc/lib_getrandom.c`,
`libs/libc/stdlib/lib_arc4random.c`, and the architecture-specific RNG drivers.
Local patches and their exact preimage/file hashes are recorded by the normal
[dependency patch applicator](../tools/apply-nuttx-patches.py). The patches alter
actual NuttX driver/libc code; they do not introduce a C HAL or move Rust service
policy into a native shim.

The [Rust 1.90 source selection](https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sys/random/mod.rs)
is important: its generic arc4random documentation describes modern OS designs,
but nxrs must evaluate the actual NuttX implementation rather than inherit that
assumption. Patch 0091 fixes error behavior, not weak configured generators.

## ESP32-S3 entropy source ownership

[Espressif documents](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/random.html)
that RF activity or the explicitly enabled SAR-ADC entropy source is needed for
its true-random guarantee. Bootloader seeding does not cover arbitrary later
reads. A passing statistical test does not establish that hardware condition.

A production provider must either own a safe, exclusive SAR-ADC entropy lease
before radio/ADC initialization, or establish and maintain the required active RF
condition while collecting a strong seed. Enabling the SAR source concurrently
with ADC/RF users is not an acceptable shortcut. Account for radio sleep,
disconnect, stop/restart and reseeding. Use a maintained DRBG when continuous
hardware entropy is unavailable, with a qualified initial seed and explicit
reseed/error policy. No always-ready flag or silently weakened fallback is
introduced here. No existing ESP image is relabeled secure.

## Bounded RP2350 and libc failure behavior

Patch 0090 uses ten-microsecond hardware poll intervals and a configurable
`RP23XX_RNG_POLL_TIMEOUT` budget (default 100,000 microseconds per 192-bit block).
It also stops after eight rejected samples within a transaction. This does not
depend on scheduler ticks during early driver initialization. Successful normal
or transient-rejection paths retain the existing block/copy behavior.

A timeout or exhausted health-rejection budget disables the source, unlocks the
driver and latches it unhealthy until reboot. A partial failed read clears the
entire requested output and returns an error; later reads cannot silently reset
the source. Startup failure also remains latched. Interrupt preemption and
mutex contention can extend wall time; this hardware poll budget is not an
application deadline. Future Rust seed/TLS operations must bound their request
sizes and execution/deadline policy separately.

Patch 0091 bounds consecutive EINTR retries, rejects zero progress/impossible
read lengths, and requires one complete successful device-source fill. It can
try the second configured device source, but cannot manufacture data from time.
For the infallible arc4random API, loss of both sources is fatal to the caller.
This deliberate availability change prevents consumers from trusting a weak
success. Production crash/log policy must also prevent secret-bearing memory
dumps; development debug settings are not release logging settings. Error-handling consumers should use a fallible interface, discard any
failed partial output and report the unavailable crypto service. Empty requests
retain their normal no-op behavior even without a configured source.

## Build and failure qualification

```sh
python3 -m unittest discover -s tests/host -p test_rng_entropy.py -v
cargo firmware --app event-demo --platform pico2-entropy
```

The native tests reconstruct and hash-check the exact pinned source preimages,
apply the actual patches, and compile/execute them with AddressSanitizer and
UndefinedBehaviorSanitizer. Register injection covers ready/delayed/stalled
sources, each hardware health bit, error+valid priority, bounded transient
rejection, partial failure, sticky startup/read failures, repeated samples and
lock admission failure. libc injection covers both device paths, zero progress,
partial/error returns, repeated interruptions, impossible lengths and absent
sources. Tests never access a physical device or print sample bytes.

The `pico2-entropy` qualification profile explicitly selects the RP2350 hardware
source for both random devices and forbids software generators. Normal builder
requirements check the resolved choices and budget; hosted Pico CI builds the
actual image, checks linked RNG symbols and retains resolved config, patch/ABI
ledgers and image hashes. This is build/link evidence only. The event demo does
not itself perform an entropy sampling/key-generation qualification. No device
is flashed, no eFuse is programmed and no debug interface is locked by this work.

## Remaining device and provisioning gates

- Run exact-image cold/warm startup and bounded sample/read tests on RP2350,
  including before any radio activity and after restart. Record timing, register
  health behavior and driver failure/recovery; uniqueness checks alone are not
  entropy-quality evidence.
- Qualify ESP32-S3 entropy ownership and transitions against the selected SDK,
  radio/ADC policy and firmware image before allowing key generation or TLS.
- Gate actual production cryptographic consumers on an explicitly supported,
  qualified source/provider/configuration. The diagnostic profile is not that
  production approval, and existing development profiles remain unqualified.
- Implement per-device identity binding and protected credential storage with
  actual platform facilities. Define generation, enrollment, rotation,
  revocation, transfer, reset, backup/recovery and decommissioning. Host files
  with private permissions are not proof of hardware-protected device keys.
- Qualify DRBG seeding/reseeding, entropy-error propagation, failure-injection
  and restart behavior in the actual cryptographic consumer. Record SDK
  callbacks and zeroization/copy limitations without exposing key material.

Track these gates in [#35](https://github.com/yongkyuns/nxrs/issues/35), together
with [secure ingestion #33](https://github.com/yongkyuns/nxrs/issues/33) and
[verified boot #34](https://github.com/yongkyuns/nxrs/issues/34). Keep them open
until their physical and implementation evidence exists.

## Offline enrollment implementation

[Device CSR enrollment](../tools/enrollment/README.md) provides an offline host
issuer with encrypted CA custody, an operator-pinned root, approved catalog
scope, fresh-key checks, signed durable issuance/revocation state and public
certificate/CRL exports. Host mutual-TLS and failure witnesses qualify that
operator path. Endpoint entropy, protected key generation/storage, CSR origin,
transport authorization, bootstrap/time and applied live revocation remain
separate release gates; the issuer does not enable device crypto by itself.
