# Component organization cleanup

## Scope and completion criteria

The goal is a normal Cargo application composed from understandable Rust crates,
not a new capability resolver. A short selector is necessary but not sufficient:
public interfaces, shared software behavior, dependency selection, test wiring
and native build inputs must also be understandable independently.

PR #260 simplified selection across 15 HAL capabilities. PR #261 adds named
backend exports, separate host-injected test wiring and independent camera
replay. PR #262 is stacked on #261 and preserves its TLS entropy-source and
detector probe exports. Its distinct work is explicit common APIs, independent
shared storage/UDP operations and migration of the native CLI composition.
Neither PR is a completed whole-repository API or build-system rewrite.
Qualification results apply to the exact tested head.

## Structural coverage

All HAL selectors enumerate exported names. No capability root uses
`pub use platform::*` or `pub use api::*`, and no selector uses `selected::*`.
Factories and unavailable implementations remain in selected source files;
shared contracts and algorithms do not acquire platform-selection logic.
The unavailable LED implementation stays private. Deliberate existing native
and mock types are preserved for their consumers.

Host-injected test wiring is separate for camera, IMU, GNSS, LED, Wi-Fi, BLE,
storage and config. The original ownership, rollback, callback, cancellation,
DMA and shutdown assertions remain. TLS and detector qualification-only exports
are explicit and private, not lost when the glob is removed.

The cross-HAL regression discovers components from workspace metadata rather
than hardcoding their names/count. Keep its negative controls. The architecture
audit also rejects wildcard contract exports in crate roots. A source inventory
is not proof of runtime correctness.

## Explicit software operations

These APIs select an operation directly, not a provider through an OS profile:

| Operation | Explicit API |
| --- | --- |
| File-backed frame replay | `camera::replay::open(...)` with Cargo `replay` functionality |
| Bounded queued directory recording | `storage::recording::FileRecorder::create_new(path, StorageLimits)` |
| Read one committed frame record | `storage::recording::read_record(path, max_frame_bytes)` |
| Numeric-address nonempty UDP submission | `transport::udp::UdpTransport::connect(SocketAddr)` or `::bind(local, peer)` |
| Legacy IPv4 submission allowing empty datagrams | `transport::udp::UdpSender::connect([u8; 4], port)` |

Recording and UDP are public shared Rust modules on the existing qualified std
targets. They do not require `NXRS_PLATFORM`. Explicitly choosing them alongside
a mock profile still means real file/socket I/O; there is no silent fallback
from a failed hardware acquisition. The native CLI now constructs these typed
operations directly. Its owner wait loop, recording barriers and teardown logic
are unchanged. Application/service code gains no platform inspection.

`UdpTransport` rejects empty and oversized packets before submission. `UdpSender`
intentionally retains its different empty-packet contract. Directory recording
retains exclusive creation, bounded admission, quota, sticky failure and explicit
worker completion. This is not the synchronous NuttX record-file operation:
`FileStorage::create` retains its existing rollback and file-sync behavior.

### Compatibility, not a second recommended API

Existing root `open()`, `storage()` and `transport()` entry points retain their
historical signatures and profile admission. They are compatibility paths, not
the template for new applications. An unbound legacy factory still returns
`Unsupported`; it never silently invokes the explicit software operation.
Existing root type aliases remain explicit. Further removal requires a complete
consumer inventory and coordinated migration, not dropping fixture exports to
make the selector shorter.

### Verification

Storage and transport workflows test unset, mock and Linux profiles with strict
Clippy. New Rust integration tests check actual file bytes, exclusive creation,
validation before filesystem mutation, quotas, UDP delivery and differing empty
packet semantics. Existing failure/rollback/ownership tests are retained.

`tests/host/test-native-runner.py` runs the same CLI with no profile, the mock
profile and the native OS profile. An independent Python decoder verifies all
four persisted frame records and four actual UDP packets for each run, together
with exact frame polls, owner timed waits and cleanup. The existing portable
workflow runs that matrix on Linux, macOS and Windows; the transport workflow
also runs it on Linux. No test is replaced by a source-layout assertion.

```sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py -v
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py -v
python3 tests/host/test-native-runner.py
```

These are host tests of production software. They do not refresh physical
hardware qualification or establish an untested NuttX std operation.

## Remaining work: do not label the entire repository complete

1. Complete the inventory/migration of legacy factory users, including target
   fixtures. Keep synchronous record-file operations separate from queued
   directory recording. Do not change flush or empty-packet semantics to make
   signatures look alike.
2. Simplify std-versus-contract-only module availability and cross-package
   source inclusion. Preserve core/WASM support unless its removal is a stated
   support decision. Qualify actual independent package reuse before claiming it.
3. Use Cargo-grounded detector dependency selection, conditional implementation
   dependencies and consumed-component native helper compilation; coordinate
   with #258. Preserve board initialization and final Kconfig/ABI checks.
4. Restore verified incremental SDK/native/Cargo reuse while invalidating stale
   success evidence. Test dependency, profile, feature and SDK patch changes
   against clean outputs.
5. Finish exact-head app/service, native and firmware qualification. Keep all
   security/lifecycle gates; prior-head results are not evidence for a new head.
