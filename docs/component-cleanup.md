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
Existing root aliases remain explicit but are hidden from primary generated
API documentation; the named constructors are the public usage template.
Further removal requires a complete consumer inventory and coordinated migration,
not dropping fixtures or changing synchronous/queued semantics for aesthetics.

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

## Post-#269 boundaries and acceptance

The source selectors and public software-composition path are stable. #269
replaces the detector's hand-written manifest traversal with target-qualified
Cargo selection, filters private NuttX helper sources by consumed HAL packages,
and qualifies optional/inactive/transitive dependency cases using real Cargo.
External camera-path consumption is checked from a separate application
workspace and lockfile.

Legacy platform-specific factory aliases are kept only for compatibility and
are hidden from primary API docs. Their removal is an API compatibility decision,
not a reason to invent adapters or merge distinct file and UDP semantics.
Platform-independent recording and UDP remain available on qualified std OSes,
and contract-only core/WASM builds remain intentionally lightweight.

Follow-up engineering distinct from the HAL's core API is explicitly recorded
in `docs/build-system.md`: strict standalone package publishing, automatic
incremental SDK/native rebuild caching, and optional NuttX mock driver graph
exclusion. Those changes must have independent acceptance tests rather than
altering this design without evidence.

The exact PR-head portable/native/firmware matrix and actual physical-device
tests determine what is qualified; source organization and host tests alone
do not establish hardware performance or binary size.
