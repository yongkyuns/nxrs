# Documentation

Start with the [repository README](../README.md) for app, test and firmware
commands. The active architecture and device-access guides are
[source ownership](architecture.md), [HAL ownership](hal-platform-architecture.md)
and [NuttX device access](nuttx-device-access.md).

The [peripheral composition architecture](peripheral-composition.md) specifies
the next driver/resource/profile separation. Its
[step-by-step implementation plan](peripheral-composition-plan.md) includes
file ownership, dependencies, compatibility gates and verification commands.
These documents distinguish proposed interfaces from current implementation.

Each `hal/<capability>` is one crate. Its contract is in `src/api.rs`, selected
Rust implementations stay private under `src/platform/`, and target-specific
NuttX C helpers live beside their Rust provider in its `ffi/` directory. A single `NXRS_PLATFORM` value selects
an actual OS profile (`linux`, `macos` or `windows`), board profile or explicit
fixture (`mock`, `host-demo`, `host-imu`). `std` is not a platform. Without a profile, builds use portable contracts
only and do not silently select mocks. A profile binds only implementations
that exist; it does not imply that every radio or board is supported.

Hardware qualification scope, limitations and measured results remain in the
individual fixture guides and evidence documents linked from the README.

The [event communication guide](concurrency-event-communication.md) explains
provider ownership, bounded delivery and the owner's wait loop.
[Measuring Rust std in nxrs's event-driven core](std-channel-performance.md)
walks through the performance and memory evidence for that model, from channel
storage and thread stacks to physical handoff measurements and their scope.

The [security design and release requirements](security.md) records the trust
boundaries, explicit non-FIPS deployment decision and evidence needed for
authenticated transport, firmware integrity, key lifecycle and protected storage.
