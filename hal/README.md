# Capability-owned Rust HALs

Each capability is one Rust crate with its own API, errors, and private
providers. Services consume capability APIs; the platform selects providers at
the module/factory boundary. Shared controllers, workers and I/O algorithms do
not inspect platform identity. Factories supply worker budgets and providers
report capabilities.

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

`common` contains the event-delivery protocol shared by BLE and Wi-Fi, including
identities, event sinks, rejection, and bounded bytes. It owns no device errors.
The old core-only camera/storage providers and their C/POSIX wrappers live under
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
the GIO protocol binding in `driver/linux/gio.rs`; NetworkManager/BlueZ schemas,
radio ownership and capability errors stay in their respective providers.
