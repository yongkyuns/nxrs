# Architecture overview

NXRS is an experimental Rust-based firmware development environment on top of NuttX. It is not a new RTOS. The experiment is to use ordinary Rust `std` code for application behavior while keeping hardware integration and product configuration separate.

For the reasoning behind those choices, read [Why NXRS?](motivation.md). For an execution path through a real application, start with [Getting started](getting-started.md).

## NuttX supplies the operating system

NuttX's C/POSIX environment supplies scheduling, board support, controller drivers and hardware resource registration. NXRS does not replace those responsibilities with a Rust scheduler or a second BSP.

A Rust standard library compiled for the selected NuttX target provides the familiar thread, synchronization, time, file and socket interfaces where supported. Maintained patches and target configuration are part of that integration; [Rust std on NuttX](../../docs/nuttx-std.md) records the scope.

## Rust components supply firmware behavior

Peripheral wrappers connect Rust code to resources that NuttX exposes. Chip drivers implement device protocols. Capability HALs present interfaces such as IMU or GNSS acquisition without making applications name a particular chip. Services add reusable behavior, and the application composes the parts.

These are responsibility boundaries, not a rule that every operation needs to pass through every layer. Ordinary file or socket operations can use `std` directly. An application can own a capability directly when that is the natural fit.

The [architecture reference](../../docs/architecture.md), [HAL ownership reference](../../docs/hal-platform-architecture.md) and [peripheral composition reference](../../docs/peripheral-composition.md) describe the current implementation and remaining work.

## Profiles describe the product

Platform profiles and overlays select implementations, devices, resource bindings and logical roles at build time. This is device-tree-like configuration, not a runtime devicetree interpreter. NuttX still owns MCU pin configuration, controller initialization and device registration.

The application can select a portable device name, such as `devices::primary_imu()`, rather than construct a particular chip driver. The profile supplies that construction. Read [Profiles and overlays](profiles.md) before changing a binding; a declared role does not establish that arbitrary combinations of devices are implemented or physically tested.

## Concurrency uses ordinary Rust

Applications use handwritten `main()` functions. Active owners use ordinary threads, blocking waits and synchronization, rather than requiring an async executor. Typed events cross ownership boundaries; handlers use synchronous calls within an owner. Queue capacity, overload handling, stop requests and joined cleanup remain explicit.

The [services guide](services.md) explains this model. It is a set of conventions for reusable firmware, not a claim that all application code is framework-free or that threads have no cost.

## Boundaries need evidence

Host tests, emulated firmware and physical hardware answer different questions. Monitor stacks, queues, heap, image size and event-path latency as components and applications grow. See [Testing and debugging](testing.md), [Performance and memory](performance.md) and [Status and limitations](status.md).
