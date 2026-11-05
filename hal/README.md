# Capability-owned Rust HALs

Each capability has one runtime Rust crate with its own API, errors, and private
providers. Services consume capability APIs; the platform selects providers at
the module/factory boundary. Shared controllers, workers and I/O algorithms do
not inspect platform identity. Factories supply worker budgets and providers
report capabilities.

OS implementations live in each HAL's `src/platform/`, named `linux`,
`macos`, `windows`, or `nuttx`, matching the profile bindings in the root
`platform/` tree. Explicit `mock` bindings are test fixtures. Rust `std` is an
available environment, not a platform or provider choice. Reusable code lives
in ordinary modules outside `platform/`, with no OS flags or copied OS wrappers.
The root tree owns configuration and build wiring; HAL code stays in its crate.
Rust's target selects the OS; profiles enable capabilities at module/factory
boundaries. No per-platform Cargo features or OS-valued custom cfg are used.

Rust owns validation, ownership, queues, synchronization, retries, cancellation,
recovery, deadlines, and error policy. Use ordinary qualified `std::fs`,
`std::io`, `std::net`, `std::thread`, and `std::sync` facilities directly.
One implementation may serve multiple platforms.

C/Objective-C is restricted to private target-header ABI and native SDK glue.
The USERLED and video ioctl helpers illustrate this boundary. A scalar ABI
alone does not justify moving behavior into C. The remaining ABI glue is recorded in
[ffi-boundaries.toml](ffi-boundaries.toml) and lives beside its owning provider.
No C behavior migration exceptions remain. Preserve callback quiescence and
ownership guarantees across every SDK boundary.

Calls between Rust modules stay typed Rust. Wi-Fi stations and BLE scan/GATT
executors use private Rust contracts; C-facing SDK witness adapters live only
under `tests/host/radio_shims` and are excluded from production builds.

`common` contains radio request identities and bounded payload storage. The
`shared/messaging` crate owns event delivery and rejection. Neither owns device
errors. The old core-only camera/storage providers and their C/POSIX wrappers live under
`tests/nuttx/`; production providers do not depend on them. NuttX storage uses
ordinary Rust files and a shared Rust append/rollback algorithm.

See the normative [HAL architecture](../docs/hal-platform-architecture.md),
[NuttX device access](../docs/nuttx-device-access.md), and repository
[agent instructions](../AGENTS.md). `tools/check-architecture.py` audits the
native boundary inventory and platform selection in shared Rust modules.

Ordinary execution timekeeping uses `std::time`. Tests pass explicit timestamps
to deterministic service transitions; acquisition timestamps remain capability
contracts. Cross-capability composition belongs in an application or test.

IP transport uses one `std::net` implementation across qualified platforms.
Wi-Fi association and BLE discovery/GATT require OS radio-control APIs that
`std` does not provide. Keep those protocols provider-local while sharing Rust
controllers/workers and ordinary resource handling. Linux Wi-Fi and BLE share
the `nxrs-linux-gio` package in `driver/linux/gio/`, which owns one native ABI
archive; NetworkManager/BlueZ schemas, radio ownership and capability errors stay
in their respective providers. IMU and GNSS use the `nxrs-acquisition` package
for their identical worker, cancellation and scripted-session mechanics. These
are ordinary Cargo dependencies; HALs do not source-include sibling packages.

## Public surface conventions

Each HAL crate root explicitly lists its stable contracts, acquisition functions
and deliberate fixture exports. `src/platform/mod.rs` contains only one selected
implementation module and named factory exports, not implementation code or
wildcard re-exports. An unbound acquisition must fail explicitly.

Ordinary shared Rust `std` facilities stay in their own modules, independent of
hardware admission: file replay in `camera::replay`, recording in
`storage::recording` and UDP in `transport::udp`, selected by their nondefault
`replay`, `recording` and `udp` Cargo features. The camera
[fixed replay aliases](camera/README.md) retain the same software meaning under
every profile. The old profile-dependent storage and transport factories are
removed. Synchronous record-file acquisition is the fixed
`storage::create_record_file(path, max_records, max_frame_bytes)` operation; it
returns `Unsupported` when unavailable and never substitutes queued recording.
`PacketSink` is the sole packet-output trait name. Cargo determines which crates
are built; profiles select physical resource bindings and NuttX configuration.

## Build and qualification ownership

Each HAL owns a local `build.rs`. Host helpers live in `platform/build-support`,
and detector SDK compilation has its own `hal/detector/build-support` package.
Only build dependencies connect a HAL to these packages. The application build
controller in `platform/build` does not enter the HAL dependency graph.
Native ABI compilation belongs to the consumed Cargo package; an unused profile
binding does not compile another capability's helpers.

Target-specific device drivers remain ordinary target-scoped dependencies.
Choosing a mock changes acquisition, not Cargo's dependency declarations. The
Cortex-M0 contract builds exclude the shared std acquisition worker; WASM/mock
and native builds use their declared dependencies normally.

The [component cleanup record](../docs/component-cleanup.md) links the compiled
isolation, detached-consumer, native-byte, lifecycle and build-freshness tests.
These checks complement the source audit and retain the separate requirements
for real NuttX ABI and physical-device qualification.
