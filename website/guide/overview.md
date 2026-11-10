# Architecture overview

NXRS uses ordinary Rust `std` code for application behavior, with hardware integration and product configuration kept separate. NuttX provides the RTOS and board support.

Read [Why NXRS?](motivation.md) for the reasoning behind these choices, or [Getting started](getting-started.md) to run an application.

## NuttX supplies the operating system

NuttX's C/POSIX environment supplies scheduling, board support, controller drivers and hardware resource registration.

A Rust standard library compiled for the selected NuttX target provides thread, synchronization, time, file and socket interfaces. The [Rust std integration reference](../../docs/nuttx-std.md) describes the maintained patches, target configuration and supported APIs.

## Rust components supply firmware behavior

Peripheral wrappers connect Rust code to resources exposed by NuttX. Chip drivers implement device protocols. Capability HALs provide interfaces such as IMU or GNSS acquisition. Services add reusable behavior, and the application composes the components.

Ordinary file or socket operations use `std` directly. An application can also own a capability directly rather than introducing a service for it.

The [architecture reference](../../docs/architecture.md), [HAL ownership reference](../../docs/hal-platform-architecture.md) and [peripheral composition reference](../../docs/peripheral-composition.md) describe these interfaces and their implementation.

## Profiles describe the product

Platform profiles and overlays select implementations, devices, resource bindings and logical roles at build time. NuttX owns MCU pin configuration, controller initialization and device registration.

An application selects a portable device name, such as `devices::primary_imu()`, and the profile supplies its concrete construction. [Profiles and overlays](profiles.md) explains device selection and the supported bindings.

## Concurrency uses ordinary Rust

Applications use handwritten `main()` functions. Active modules use ordinary threads, blocking waits and synchronization. Typed events cross ownership boundaries; handlers use synchronous calls within an owner. Queue capacity, overload handling, stop requests and joined cleanup are explicit.

The [services guide](services.md) explains the component interfaces and lifecycle.

## Tests and evaluations

Host tests cover application and service behavior. Simulator and QEMU runs test firmware execution, while on-board tests measure device behavior and performance. Existing evaluations include thread stacks, queues, heap use, image size and event-delivery latency.

See [Testing and debugging](testing.md), [Performance and memory](performance.md) and [Current status](status.md) for commands and results.
