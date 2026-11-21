# Rust API reference

Use this catalog to find the Rust library that owns a type, function, or method. If you’re deciding which API to use, start with [Interfaces and APIs](guide/interfaces.md).

## Find an API

Choose a library below, then search its generated Rust documentation for a symbol such as `Led` or `PacketSink`. Use the site’s header search for guides and library names.

The catalog shows both names you need: Cargo selects package `nxrs-led`, while Rust code imports its library as `led`, for example `led::Led`. An item's page gives its signature, documentation, and a link to source. Type and trait pages also list relevant implementations. Follow the owning contract for error, completion, and cleanup semantics.

<!-- api-catalog -->

## Coverage and features

These pages document the website's source revision using the pinned Rust toolchain on a host, with capability bindings unselected. Product profiles supply actual hardware bindings and device instances; acquisition still reports resource availability and initialization errors. See [Profiles and overlays](guide/profiles.md) and [Testing and debugging](guide/testing.md) for target configuration and qualification.

Ordinary default features are retained, including messaging channels. The reference also includes these explicit features:

| Cargo package | Included features | APIs to look for |
| --- | --- | --- |
| `nxrs-camera` | `replay` | `camera::replay` for explicit file replay |
| `nxrs-storage` | `recording` | `storage::recording` for queued file recording |
| `nxrs-transport` | `udp` | `transport::udp` for Rust socket output |
| `nxrs-composition` | `imu`, `gnss` | IMU and GNSS device-access modules |

The named devices available through `composition` follow the selected product profile. The public `imu::construction` and `gnss::construction` modules serve platform catalogues and qualification; applications use the catalogue's device accessors and the capability APIs. Detector model features and diagnostic probes are not enabled in this view. Private provider and build-tooling interfaces are outside the public catalog.

## NuttX resource interfaces

The following resource types are compiled specifically for NuttX. Read their contracts and implementation alongside [NuttX device access](../docs/nuttx-device-access.md):

| Resource type | Contract and source |
| --- | --- |
| `i2c::Bus` | [I²C adapter](../driver/i2c/README.md) · [bus implementation](../driver/i2c/src/platform/nuttx/bus.rs) |
| `spi::Device` | [SPI adapter](../driver/spi/README.md) · [device implementation](../driver/spi/src/platform/nuttx/device.rs) |
| `gpio::Interrupt` | [GPIO adapter](../driver/gpio/README.md) · [interrupt implementation](../driver/gpio/src/platform/nuttx/interrupt.rs) |

[Drivers and HALs](guide/hardware.md) explains when to use these resource adapters, a chip protocol, or a capability. Their presence in source does not establish physical-device qualification; use the owning component's target tests and hardware evidence.
