# Capability-owned Rust HALs

Each capability has one runtime Rust crate with its own API, errors, and private
providers. Services consume capability APIs; the platform selects providers at
the module/factory boundary. Shared controllers, workers and I/O algorithms do
not inspect platform identity. Factories supply worker budgets and providers
report capabilities.

The [peripheral composition architecture](../docs/peripheral-composition.md)
separates sensor-specific driver packages from capability implementations. IMU
drivers implement the common device contract directly; its HAL adds managed
acquisition and stream metadata. OS factories supply resources through existing
Rust interfaces such as embedded-hal. IMU and GNSS hardware profiles resolve
concrete device bindings.
The separate simulation profile supplies both capabilities from one coordinated
session. The [implementation plan](../docs/peripheral-composition-plan.md)
records the migration and qualification gates.

`hal/` owns capability/subsystem APIs and their private native implementations;
it is not a vendor register HAL. There is no mandatory driver/HAL/service chain.
Use Rust `std` directly where it already supplies the portable operation. Add
device adapters and execution resources only for requirements it cannot express.
The [shared acquisition package](../shared/acquisition/README.md) provides
worker lifetimes, cancellation and readiness for IMU, GNSS and audio. The
owning HAL defines delivery and overload handling. `imu::Session` and
`gnss::Session` name acquisition lifetimes.

`settings::Store` persists small settings records such as Wi-Fi credentials
and gateway enrollment. The NuttX provider uses named records; each
application owns its record formats.

OS implementations live in each HAL's `src/platform/`, named `linux`,
`macos`, `windows`, or `nuttx`, matching the profile bindings in the root
`platform/` tree. Explicit `mock` bindings are test fixtures. Rust `std` is an
available environment, not a platform or provider choice. Reusable code lives
in ordinary modules outside `platform/`, with no OS flags or copied OS wrappers.
The root tree owns configuration and build wiring; HAL code stays in its crate.
Rust's target selects the OS; profiles enable capabilities at module/factory
boundaries. Each `src/platform/mod.rs` uses one `cfg_if::cfg_if!` to select the
validated binding. Local providers use `src/platform/<binding>/mod.rs` and the
unavailable fallback uses `src/platform/unbound.rs`.

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

`radio-types` contains request identities and bounded payloads shared by Wi-Fi and BLE. The
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
in their respective providers. Shared implementation packages are ordinary
Cargo dependencies; HALs do not source-include sibling packages.

## Public surface conventions

Each HAL crate root explicitly lists its stable contracts, acquisition functions
and deliberate fixture exports. `src/platform/mod.rs` contains only one selected
implementation module and named factory exports, not implementation code or
wildcard re-exports. An unbound acquisition must fail explicitly.

Explicit file replay and recording use the nondefault `replay` and `recording`
Cargo features on `camera::replay` and `storage::recording`. TCP and UDP use
`std::net`. The camera
[fixed replay aliases](camera/README.md) retain the same software meaning under
every profile. Synchronous record-file acquisition is the fixed
`storage::create_record_file(path, max_records, max_frame_bytes)` operation; it
returns `Unsupported` when unavailable and never substitutes queued recording.
Camera frame history and stop belong to `camera::capture::Session`. Station retry
and rejoin belong to `wifi::Reconnector`. Cargo determines which crates
are built; profiles select physical resource bindings and NuttX configuration.

## Build and qualification ownership

Cargo adapters and host helpers live in `tools/build-support`,
and detector SDK compilation lives in `tools/detector-build-support`.
HAL manifests point directly to those tooling-owned adapters.
Only build dependencies connect a HAL to these packages. The application build
controller in `tools/build` does not enter the HAL dependency graph.
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
