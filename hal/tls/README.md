# Device TLS

This capability supplies the credential foundation for authenticated camera
ingestion (#33/#35): a volatile, non-exportable P-256 signing key, public PEM
enrollment requests and TLS1.2/1.3 server connections. Rust owns peer/time
admission, socket budgets, callback lifetime, error policy and teardown; private
C supplies actual SDK layouts, field access and enum translation.
The hash-pinned, unmodified Mbed TLS 4.2.0 / TF-PSA-Crypto 1.2.0 source builds
offline after `python3 upstream/mbedtls/fetch.py`.
The SDK uses CMake's size-optimized `MinSizeRel` build to match the embedded
product's code-size priority; device execution and memory peaks still need qualification.

Select `linux-tls` or `macos-tls` for host qualification, or `pico2-tls` and
`esp32s3-camera-tls` for NuttX compile qualification. Public socket/time types use
Rust `std` and remain available without a selected backend; `open()` then returns `Unsupported`,
acquiring no SDK or entropy. Services need no platform conditionals.
The NuttX factory accepts only the configured RP2350 hardware source, opens
`/dev/random` nonblocking and refuses software fallbacks. Polling and interrupted
reads are bounded; contention, partial output and source failures close the
identity. There is no fixed seed. ESP32-S3 source admission remains unsupported.

Only one SDK owner may exist. It cannot move between threads; native callbacks
borrow it only during synchronous calls. Fresh reseeding is explicit and source
failure is sticky, including automatic reseeding. Dropping retires the borrowed
key context, PSA key, DRBG and source before another owner can start.

`Identity::accept` borrows the key for one nonblocking `TcpStream`. Supply one
approved DER issuer, own leaf, exact peer leaf, signed CRL and `CertificatePolicy`.
The policy pins the issuer's DER SHA-256 and expected own/peer URI names from
authorized enrollment. Each leaf needs exactly one matching URI among at most
eight SAN entries; names/values are bounded to 256 bytes. Subject CNs and other
SAN types do not supply URI identity. The adapter checks the key pair,
signatures, revocation, matching issuers and validity; the
own leaf must declare only serverAuth and the peer only clientAuth. Missing,
wrong or additional extended key usages are refused before handshake I/O. The
application must approve the identity mapping/generation and reject enrollment
or CRL rollback before supplying that material. Dropping a connection preserves the key.

Supply `TimeLease::from_trusted` with an authenticated UTC observation and a
maximum age of at most 60 seconds. The adapter advances it monotonically, rounds
uncertainty outward and refuses I/O at the earliest lease/certificate/CRL deadline.
It never reads ambient UTC. Refresh before expiry; closed connections stay closed.
TLS uses P-256/ECDHE, SHA-256 and AES-128-GCM; tickets, early data and renegotiation
are disabled. A write retry must present the same pending bytes. Each SDK call
admits at most eight socket callbacks and 8 KiB; plaintext records are at most
4 KiB. The handshake has a five-second admission deadline. Native cryptography
and entropy reads are synchronous: qualify their execution latency or isolate
them from capture before product admission. Drop aborts without waiting on peers.

`python3 tests/tls/qualify.py --platform linux` (or `macos`) runs actual SDK
failure/lifetime tests, native sanitizer controls, independent CSR verification
and OpenSSL TLS peers with revocation, deadline and real backpressure controls.
The existing Pico job links the endpoint and loopback stack into an ordinary-main
NuttX image; it does not run hardware. Fixture-generated PKI and host UTC are test
inputs, not device enrollment or clock qualification.

The key is protected by software usage policy, not persistent or hardware-backed
custody. CSR subjects are advisory; the issuer must independently assign the
approved identity. The camera composition offers an opt-in TLS worker via
`LiveCamera::new_tls`. Enrollment/persistence, a trusted device-time source,
physical entropy/execution/resource measurements and
reset/decommission behavior remain required before product admission. Shipping
profiles do not enable this capability.

The ESP32-S3 compile profile inherits the video-only camera's Wi-Fi, PSRAM and
cache configuration. Build the existing endpoint fixture with the normal pinned
ESP32-S3 tools (no hardware is accessed):

```sh
python3 upstream/mbedtls/fetch.py
bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/tls-identity/Cargo.toml \
  --app-package nxrs-std-tls-identity --bin tls-endpoint \
  --command tls_endpoint --priority 100 --stack-size 65536 \
  --platform esp32s3-camera-tls --out target/esp32s3-camera-tls \
  --abi-profile minimal --size-optimized --trace-only-backtrace \
  --panic-immediate-abort
```

This links the shared TLS adapter with the native camera/radio drivers; the
fixture uses synthetic capture and test PKI/time. It does not qualify physical
capture, handshake execution or peak memory. ESP32-S3 `tls::open()` still returns
`Unsupported`: its RNG requires an active RF or exclusive SAR-ADC entropy source.
The pinned PHY power flags are per radio, not per caller, so TLS cannot borrow
Wi-Fi's flag as an entropy lease. Source ownership through sampling, bounded
driver reads and physical measurements are required before admission; see
[Espressif's RNG requirements](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/random.html).
