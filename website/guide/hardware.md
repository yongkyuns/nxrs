# Drivers and HALs

Hardware access is split between NuttX board support, Rust resource adapters, device drivers, and capability interfaces. Application code uses the interface appropriate to the operation.

## NuttX owns board support

The NuttX BSP configures pins and controllers, initializes devices, and registers OS resources. Platform profiles select the configuration used by a firmware build. Start by checking that the BSP exposes the resource the application needs.

## Use std for standard operations

Files, byte streams, sockets, time, and synchronization can use `std` directly. A serial stream can use an owned file handle and `Read`/`Write`; IP communication can use `std::net`.

Device configuration, ioctls, cancellable readiness, and radio-control operations may need a Rust adapter over native APIs. Keep those bindings small. Validation, ownership, retries, and cleanup stay in Rust.

## Resource adapters and chip drivers

Resource adapters expose bus or pin operations such as I²C transactions and GPIO changes. A chip driver implements the device's register protocol using those resource interfaces. The driver should not need to know which application is using it or how the product names its logical devices.

Use existing Rust bus traits where they express the operation. Keep controller initialization and electrical configuration with the BSP rather than duplicating them in each driver.

## Capability HALs

A capability HAL presents a subsystem interface, such as storage, transport, a camera, or a data-acquisition device. It owns its public Rust API, errors, resource lifetime, and OS-specific implementation selection.

The application can receive a capability through a constructor or acquire a named device through a portable catalogue. Services consume the interface rather than constructing board-specific drivers. A synchronous driver call and a streaming acquisition session are different APIs because their lifetimes differ.

For example, a data logger can separate input acquisition from formatting, recording, and transmission. Only the acquisition and resource configuration need to change when the hardware changes; the record format and processing code can remain ordinary Rust.

## Source layout

| Location | Responsibility |
| --- | --- |
| `driver/` | Resource adapters and device protocols |
| `driver/peripheral/` | Shared synchronous device contracts |
| `hal/<capability>/` | Public capability API and implementations |
| `hal/<capability>/src/platform/` | OS-specific implementation selection |
| Provider-local `ffi/` directories | Native ABI bindings |
| `platform/src/` | Named devices and typed constructors |
| `platform/<platform>/profile.toml` and `platform/nuttx/` | Product and target configuration |
| `service/` | Reusable application behavior |

## Add a peripheral

Check the BSP registration, implement or reuse the resource adapter, then test the device protocol and capability lifecycle. Add its supported device and resource bindings to the profile configuration. Verify startup, I/O, failure handling, and shutdown on the target.

DMA buffers, callbacks, file descriptors, and device leases must remain valid until the associated work has stopped. Include those lifetimes in tests when changing an implementation.

The [HAL architecture](../../docs/hal-platform-architecture.md), [NuttX device access](../../docs/nuttx-device-access.md), and [peripheral composition](../../docs/peripheral-composition.md) references contain the implementation rules and current device-specific details.
