# Documentation

Start with the [repository README](../README.md) for app, test and firmware
commands. The active architecture and device-access guides are
[source ownership](architecture.md), [HAL ownership](hal-platform-architecture.md)
and [NuttX device access](nuttx-device-access.md).

Each `hal/<capability>` is one crate. Its contract is in `src/api.rs`, selected
Rust implementations stay private under `src/platform/`, and target-specific
NuttX C helpers live beside their Rust provider in its `ffi/` directory. A single `NXRS_PLATFORM` value selects
an actual OS profile (`linux`, `macos` or `windows`), board profile or explicit
fixture (`mock`, `host-demo`, `host-imu`). `std` is not a platform. Without a profile, builds use portable contracts
only and do not silently select mocks. A profile binds only implementations
that exist; it does not imply that every radio or board is supported.

Hardware qualification scope, limitations and measured results remain in the
individual fixture guides and evidence documents linked from the README.

The [security design and release requirements](security.md) records the trust
boundaries, explicit non-FIPS deployment decision and evidence needed for
authenticated transport, firmware integrity, key lifecycle and protected storage.

Report vulnerabilities using [SECURITY.md](../SECURITY.md). The
[device entropy trace](entropy.md) records source selection, failure behavior
and remaining physical/provisioning qualification gates. The
[integration evidence guide](security-integration.md) preserves dated source
cohorts and their limits; current implementation status remains linked from
the security tracker in the design guide.
