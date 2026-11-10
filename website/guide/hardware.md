# Drivers and HALs

Use Rust's standard library for ordinary OS operations. Resource adapters, chip drivers and capability HALs provide the additional interfaces needed for hardware access.

## Use std where it fits

Use `std::fs`, `std::io`, `std::net` and `std::time` directly when the target supports them. A UART data stream can use an owned `File` with `Read`/`Write`, and IP transport can use ordinary Rust sockets.

UART configuration, cancellable readiness, device controls and radio SDK operations may need thin native bindings. Keep validation, ownership, error handling, retry and cleanup in Rust.

## Resource adapters and chip drivers

I²C transactions, SPI chip-select sequencing and GPIO use resource-specific interfaces such as embedded-hal traits. NuttX adapters own the underlying descriptors and implement those operations.

Chip drivers receive their resources from the caller. They implement device identification, register protocols, FIFO decoding and chip-specific settings. Board selection and product profiles stay outside the driver.

## Capability HALs

A capability HAL provides an interface for a subsystem such as IMU, GNSS, storage or transport. It owns its public Rust API and errors, selects the appropriate platform implementation, and manages acquisition and resource lifetimes.

For example, an IMU driver provides synchronous device operations. The IMU HAL adds stream metadata, resource ownership, acquisition and delivery. `imu::Session` represents active use of a device.

Shared Rust behavior stays in ordinary modules. OS-specific integration belongs in the corresponding platform implementation.

## Source layout

| Location | Purpose |
| --- | --- |
| `driver/<device>` | Chip and protocol implementations |
| `driver/i2c`, `driver/spi`, `driver/gpio` | NuttX resource adapters |
| `driver/peripheral` | Shared synchronous device interfaces |
| `hal/<capability>/src/api.rs` | Public capability interface |
| `hal/<capability>/src/platform/` | Private OS/provider integration |
| The provider's `ffi/` directory | Native ABI translation |
| `hal/acquisition` | Shared acquisition and cancellation logic |
| `service/` | Reusable processing and execution policy |

Follow the [peripheral architecture](../../docs/peripheral-composition.md), [HAL architecture](../../docs/hal-platform-architecture.md) and [NuttX device-access rules](../../docs/nuttx-device-access.md) when adding or changing hardware integration.

## Add and test hardware

Start by checking that the NuttX BSP initializes and registers the required controller or endpoint. Test the resource operations, device protocol and capability lifecycle, then add the platform bindings.

Use host mocks for protocol and failure-handling tests. Run on-board tests for transaction timing, interrupts, electrical configuration and throughput.

Preserve resource lifetimes when moving code between C and Rust. Keep DMA buffers, callback state, descriptors and interface leases alive until the associated work has stopped, then release them and report any cleanup errors.
