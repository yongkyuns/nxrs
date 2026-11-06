# Component organization cleanup

## Result and scope

Applications compose ordinary Cargo packages through explicit Rust APIs. HAL
selectors choose resource acquisition; shared software modules select their own
functionality through additive Cargo features. Cargo owns dependencies,
features, aliases and target conditions. There is no additional capability
resolver or runtime provider registry.

This cleanup completes the storage/transport API migration, gives shared Rust
and build helpers real package owners, and makes native compilation follow the
consumed Cargo graph. It restores verified reuse of completed native/SDK builds.
The rules are defined in [HAL architecture](hal-platform-architecture.md) and
[build-system ownership](build-system.md).

## Public operations

| Operation | Canonical API | Availability |
| --- | --- | --- |
| File-backed frame replay | `camera::replay::open(...)` | Nondefault `nxrs-camera/replay` feature. |
| Bounded queued directory recording | `storage::recording::FileRecorder::create_new(path, StorageLimits)` | Nondefault `nxrs-storage/recording` feature. |
| Read one committed frame record | `storage::recording::read_record(path, max_frame_bytes)` | Same `recording` feature. |
| Synchronous append/rollback record file | `storage::create_record_file(path, max_records, max_frame_bytes)` | Configured NuttX acquisition; `Unsupported` elsewhere. |
| Numeric IPv4/IPv6 nonempty packets | `transport::udp::UdpTransport::connect(SocketAddr)` or `::bind(local, peer)` | Nondefault `nxrs-transport/udp` feature. |
| IPv4 datagrams, including empty datagrams | `transport::udp::UdpSender::connect([u8; 4], port)` | Same `udp` feature. |
| Packet-producing logic with an injected output | `transport::PacketSink` | Contract-only dependency; callbacks can implement it directly. |

Replay, recording and UDP require Rust std but have no OS whitelist or hardware
profile requirement. Selecting them beside a mock profile still performs real
file/socket I/O. The native CLI forwards all three features through its existing
`replay` feature. Core-only application-library consumers disable that default.

The profile-dependent storage and transport `open()`, `storage()` and
`transport()` factories, concrete root aliases and the `Transport` trait alias
are removed. Their in-tree callers and target fixtures use the explicit APIs.
Camera's documented [fixed replay aliases](../hal/camera/README.md) remain:
they always denote the same software operation and do not change signatures or
meaning with the profile. Mock fixtures remain deliberately exported.

The operation contracts remain distinct. Synchronous record-file append rolls
back partial writes, preserves quota after successful rollback and becomes
permanently poisoned after failed rollback; flush synchronizes the file. Queued
directory recording accepts into owned bounded buffers, uses a FIFO flush
barrier and provides explicit `finish()` drain/join. Its `Drop` does not join.
`UdpTransport` rejects empty packets; `UdpSender` preserves zero-length IPv4
datagrams. Sharing implementation does not erase those differences.

## Package and source ownership

| Owner | Responsibility |
| --- | --- |
| `hal/<capability>` | Public contract, errors, resource acquisition and device policy. |
| `driver/acquisition` | Shared sensor worker, cancellation and scripted-session mechanics. |
| `driver/linux/gio` | Shared GLib/GIO Rust bindings and one native ABI archive for Wi-Fi and BLE. |
| `platform/build-support` | Generic host build, profile, native-tool and source helpers. |
| `hal/detector/build-support` | Detector-owned numerical SDK build code and its compatibility header. |
| `platform/build` | Application build controller, target configuration and qualification commands. |

HALs own local `build.rs` entry points and depend on host helpers through
`[build-dependencies]`. The build controller does not enter HAL or driver build
closures. Detector build support can use generic support; generic support
cannot depend back on a capability's SDK tooling. Apps/services cannot depend
on either host-tooling layer.

Shared production Rust is imported through Cargo dependencies. Package-local
source modules and generated `OUT_DIR` code remain normal Rust. Explicitly
test-only host witnesses remain separate from production selection. Selectors
still contain source-module selection and named exports only; contracts and
algorithms do not acquire profile logic. The ABI ledger covers both HAL-owned
and driver-owned native files, with the same prohibition on C orchestration.

Physical sensor libraries remain ordinary target-scoped dependencies. A mock
profile changes the selected acquisition but does not remove a dependency that
Cargo enables for that target. This is supported composition, not an implicit
promise that profile changes prune the target graph. Cortex-M0 contract builds
exclude the std acquisition worker; WASM/mock and native compositions use their
declared package dependencies.

## Cargo selection and native compilation

The frontend asks Cargo for the production graph of the actual configured build
invocation. The query retains the selected package, user/default features,
target, compiler environment and Cargo configuration. Disabled optional, dev,
host-build and proc-macro branches do not enable a product detector model.
Profile model features are added only through a dependency alias Cargo can
address and are checked by Cargo before compilation. A transitive-only detector
without an addressable model feature reports the required direct dependency;
the frontend does not synthesize manifests or introduce service feature
forwarding. SDK-free planning reports unresolved graph selection honestly.

NuttX supplies a validated native compiler/header context to Cargo. Each consumed
package compiles and links its own private ABI helper archive. A profile binding
does not eagerly compile an unconsumed HAL. Board/runtime initialization remains
explicit target wiring, independently of optional HAL helpers. Linux GIO owns
one archive even when both radio HALs are used.

A detached-consumer fixture copies only declared normal/build path dependencies,
uses renamed crates and a handwritten `main`, and creates its own lockfile. It
runs actual replay, recording and UDP with unset, mock and native profiles,
exercises selected mock sensors and links both Linux radio packages through the
single GIO owner. It has no dependency on the NXRS application build controller.
Outside an NXRS checkout, a selected profile requires an explicit `NXRS_SDK_ROOT`
for profile/SDK inputs.

## Verified rebuild reuse

Completed builds record their configuration, feature/toolchain/SDK inputs and
the contents of their owned native/SDK directories. Reuse requires both the
inputs and retained bytes to match. Changed or missing sources, headers and
patch inputs invalidate the affected state; interrupted builds have no completed
reuse record. An explicit clean option discards compilation state.

Starting another build invalidates prior success evidence even when compilation
state is reusable. Cargo and Make still determine which units need recompilation.
Unchanged generated files preserve their modification times. A warm cache is
never treated as evidence that the current build succeeded.

## Verification and qualification boundaries

| Check | Evidence |
| --- | --- |
| [Architecture controls](../tests/host/test_architecture.py) and [HAL boundary controls](../tests/host/test_hal_boundaries.py) | Reject runtime build-tool edges, escaped source imports, wildcard selectors and unreviewed native behavior; preserve explicit test witnesses. |
| [Clean HAL isolation](../tools/check-hal-isolation.py) and [artifact rejection controls](../tests/host/test_hal_isolation.py) | Compare actual host/target artifacts for core, WASM, mock and native cases; reject controller contamination, missing packages and stale evidence. |
| [Detached consumer](../tests/host/test_external_consumers.py) | Declared-package reuse, independent lockfile, renamed dependencies, real software operations and one GIO archive. |
| [Cargo graph controls](../platform/build/tests/cargo_graph.rs) | Defaults, optional/target dependencies, aliases, compiler/configuration forwarding and excluded nonproduction edges. |
| [Native helper ownership](../platform/build/tests/nuttx_helper_ownership.rs) and [header freshness](../platform/build/tests/native_header_freshness.rs) | Compile/link only consumed helpers; rerun or fail after header/source/configuration changes. |
| [Reuse controls](../platform/build/src/reuse.rs) | Matching bytes, changed configuration, interrupted builds, explicit clean and unchanged-file timestamps. |
| [Storage faults](../hal/storage/src/file_tests.rs), [worker lifecycle](../hal/storage/src/recording/writer.rs) and [UDP tests](../hal/transport/tests) | Rollback, poisoning, bounded buffers, barriers, completion, packet bytes, rejection and distinct empty-packet behavior. |
| [CLI oracle](../tests/host/test-native-runner.py) | Independently decode four persisted frames and four UDP datagrams per unset/mock/native profile, with owner waits and cleanup. |

The portable workflow runs native consumers on Linux, macOS and Windows and
retains core-only and WASM gates. Storage/transport workflows select their
software features explicitly, including under mock profiles, and retain strict
Clippy and lifecycle checks. Workflow path filters follow the new package owners.

Representative commands:

```sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py -v
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py -v
python3 -m unittest discover -s tests/host -p test_hal_isolation.py -v
cargo test --locked -p nxrs-build-support -p nxrs-detector-build-support -p nxrs-build
cargo test --locked -p nxrs-storage --features recording
cargo test --locked -p nxrs-transport --features udp
python3 -m unittest discover -s tests/host -p test_external_consumers.py -v
python3 tests/host/test-native-runner.py
```

These checks establish source/build ownership and host software behavior. Real
NuttX ABI, scheduler, device timing, DMA, callback lifetime and physical radio or
sensor behavior still require their target qualification. Results apply to the
tested source revision and target; older device results do not qualify a new
binary automatically.
