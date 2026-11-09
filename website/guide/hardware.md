# Drivers and HALs

Keep the resource boundary close to the operation it represents. NXRS does not require every operation to pass through a new abstraction merely because the program runs on an MCU.

## Start with std when it fits

Use qualified `std::fs`, `std::io`, `std::net` and `std::time` operations directly when the target implements the needed behavior. A UART data stream can use an owned `File` and `Read`/`Write`. IP transport can use ordinary Rust sockets.

Opening a POSIX device node does not make every device control a std operation. UART configuration, cancellable readiness, ioctls and radio SDK operations can need thin native bindings. Keep validation, ownership, error policy, retry and cleanup in Rust.

## Use resource traits for resource semantics

I²C transactions, SPI chip-select sequencing and GPIO are not interchangeable byte streams. Use existing embedded-hal contracts where appropriate. NuttX resource adapters own the underlying descriptors and implement the required transaction/pin semantics.

A reusable chip driver consumes usable resources. It should not discover board names, parse a product profile or depend on a service. Device identity, register protocol, FIFO decoding and chip-specific settings belong with the driver.

## Understand capability HALs

A capability HAL is a subsystem boundary such as IMU, GNSS, storage or transport. It is not necessarily a vendor register HAL. It owns its public Rust API and errors, selects the appropriate implementation at its module/factory boundary, and manages acquisition and lifetime policy.

For example, an IMU driver exposes synchronous device behavior. The IMU HAL adds stream metadata, resource ownership, acquisition and delivery. `imu::Session` represents active use; a device object and an active session are different things.

Ordinary std behavior is shared Rust code, not a separate “std platform.” Actual OS-specific integration stays under its operating-system implementation.

## Follow source ownership

| Location | Start here for |
| --- | --- |
| `driver/<device>` | Reusable chip and protocol behavior |
| `driver/i2c`, `driver/spi`, `driver/gpio` | NuttX resource adapters |
| `driver/peripheral` | Shared synchronous device contracts |
| `hal/<capability>/src/api.rs` | Public capability contract |
| `hal/<capability>/src/platform/` | Private OS/provider integration |
| The provider’s `ffi/` directory | Narrow native ABI translation |
| `hal/acquisition` | Shared acquisition and cancellation mechanics |
| `service/` | Reusable processing and execution policy |

The [peripheral architecture](../../docs/peripheral-composition.md) records current ownership and interfaces. The [HAL architecture](../../docs/hal-platform-architecture.md) and [NuttX device-access rules](../../docs/nuttx-device-access.md) are implementation requirements, not optional style suggestions.

## Add hardware in the right order

First verify that the NuttX BSP initializes and registers the required controller or endpoint. Then qualify the resource operation, driver protocol and capability lifecycle. Add the profile composition only for bindings that actually exist.

A profile declaration cannot create missing BSP support. A host bus mock cannot establish repeated-start timing, interrupt behavior, electrical correctness or physical throughput. Keep simulated protocol tests and target/device evidence separate.

Preserve teardown behavior when moving code between C and Rust. DMA buffers, native callbacks, descriptors and interface leases must remain alive until the relevant work is quiescent. Do not report release merely because a Rust handle was dropped.
