# Device TLS identity

This capability supplies the credential foundation for authenticated camera
ingestion (#33/#35): one volatile, non-exportable P-256 signing key and public
PEM enrollment requests. Rust owns admission, SDK/callback lifetime, error policy
and teardown; private C supplies actual SDK layouts and header constants.
The hash-pinned, unmodified Mbed TLS 4.2.0 / TF-PSA-Crypto 1.2.0 source builds
offline after `python3 upstream/mbedtls/fetch.py`.

Select `linux-tls` or `macos-tls` for host qualification, or `pico2-tls` for
NuttX compile qualification. Ordinary unbound builds acquire no SDK or entropy.
The NuttX factory accepts only the configured RP2350 hardware source, opens
`/dev/random` nonblocking and refuses software fallbacks. Polling and interrupted
reads are bounded; contention, partial output and source failures close the
identity. There is no fixed seed. ESP32-S3 source admission remains unsupported.

Only one SDK owner may exist. It cannot move between threads; entropy callbacks
borrow it only during synchronous calls. Fresh reseeding is explicit and source
failure is sticky, including automatic reseeding. Dropping retires the borrowed
key context, PSA key, DRBG and source before another owner can start.

`python3 tests/tls/qualify.py --platform linux` (or `macos`) runs actual SDK
failure/lifetime tests, native sanitizer controls and independent CSR signature
and public-key uniqueness checks. The existing Pico CI job compiles the same
adapter into an ordinary-main NuttX image; it does not run hardware.

The key is protected by software usage policy, not persistent or hardware-backed
custody. CSR subjects are advisory; the issuer must independently assign the
approved identity. Device TLS sessions, enrollment/persistence, trusted time,
physical entropy/resource measurements and reset/decommission behavior remain
required before product admission. No product profile enables this capability.
