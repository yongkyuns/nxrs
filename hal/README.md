# Capability contracts and independently selected providers

`camera`, `storage`, and `transport` each own an `api` crate plus independently
selected implementations. Camera/storage currently have `native`, `nuttx`, and
`mock` providers; transport has native/NuttX std-socket providers plus a mock. Domain APIs activate no providers.
Services import only the APIs they need. App main selects resources through
ordinary Cargo dependencies and supplies them to service constructors.

See [camera support](camera/README.md), [frame-storage semantics](storage/README.md),
and [transport contracts and isolation](transport/README.md). Native providers
use standard-library file/socket operations beneath these device/data contracts.
Mock providers are explicit test choices, not evidence of real web hardware/I/O.

Each capability owns its API and error type; similar variants do not
make errors interchangeable across capabilities. Providers translate platform
failures into their capability's errors, and services wrap those errors locally.
`common` contains only the event-delivery protocol shared by BLE and Wi-Fi:
request/session identities, bounded bytes, event sinks, and delivery rejection.
`support/nuttx` contains the existing descriptor ownership used by older NuttX
camera/storage compatibility providers. It returns scalar bridge status codes;
each provider translates them into its own API errors. It is not a generic
device/POSIX layer and must not grow into one. NuttX transport already demonstrates the preferred
direction: its production provider uses ordinary `std::net` directly.

For new physical NuttX providers, keep the implementation in the capability's
Rust `hal/<capability>/nuttx` package. Use qualified `std::fs`, `std::io`
and `std::net` operations directly where applicable. If the NuttX driver
requires a device-specific control that `std` does not represent, keep only
the minimal typed target-header/ABI helper private to that provider; do not
expose raw `ioctl`, descriptors, request numbers or native structs to portable
consumers. See
[NuttX device access from Rust](../docs/nuttx-device-access.md).

## Composition and ordinary standard-library facilities

There are no platform-wide HAL facade packages. Callers select camera, storage,
and transport API/provider crates directly. Cross-capability composition belongs
in an application or test package, not in `hal/api`, `hal/native`, `hal/mock`,
or `hal/nuttx`.

Ordinary system timekeeping uses `std::time`, not a capability HAL. There is
no Clock trait or clock mock. Deterministic tests pass explicit timestamps
directly; the core-only x86 NuttX fixture keeps its C time shim under
`tests/nuttx` only. Service state transitions receive explicit time values;
sensor acquisition timestamps and clock-domain conversion stay device/data
contracts. No clock, memory, or runtime package suite is required.
