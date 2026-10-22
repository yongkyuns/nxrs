# Documentation

Start with the [repository README](../README.md) for app, test and firmware
commands. The active architecture and device-access guides are
[source ownership](architecture.md), [HAL ownership](hal-platform-architecture.md)
and [NuttX device access](nuttx-device-access.md).

Each `hal/<capability>` is one crate. Its contract is in `src/api.rs`, selected
Rust implementations stay private under `src/platform/`, and target-specific
NuttX C helpers live beside their Rust provider in its `ffi/` directory. A single `NXRS_PLATFORM` value selects
the build profile: `std`, `mock` or `host-demo`; `host-imu` selects native
desktop I/O plus a mock IMU. Without a profile, builds use portable contracts
only and do not silently select mocks. A profile binds only implementations
that exist; it does not imply that every radio or board is supported.

Hardware qualification scope, limitations and measured results remain in the
individual fixture guides and evidence documents linked from the README.

Security trust boundaries, release gates and FIPS applicability are documented in
[the security model](security.md). Report vulnerabilities using [SECURITY.md](../SECURITY.md).

The [device entropy trace](entropy.md) records source selection, failure behavior
and the remaining physical/provisioning qualification gates.
