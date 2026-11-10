# Architecture overview

NXRS is a general-purpose Rust firmware framework built on NuttX. NuttX supplies the RTOS and board support; NXRS provides the Rust integration, reusable components, and platform configuration used by applications.

## NuttX and Rust std

NuttX's C/POSIX environment supplies scheduling, synchronization, files, sockets, controller drivers, and hardware resource registration. A Rust standard library compiled for the target exposes the corresponding Rust APIs.

Application code uses ordinary Rust functions and libraries. Target integration and native ABI bindings remain below the application. The [Rust std reference](../../docs/nuttx-std.md) documents that integration.

## Components and responsibilities

| Component | Responsibility |
| --- | --- |
| Resource adapter | Expose an OS resource or peripheral bus to Rust |
| Device driver | Implement a device's protocol |
| Capability HAL | Present a subsystem API and manage its resources |
| Service | Provide reusable application behavior |
| Application | Construct components and define product behavior |
| Platform profile | Select hardware bindings and build configuration |

Use the layers that the operation needs. A file write or socket exchange can use `std` directly. A peripheral requiring device-specific controls uses a driver or capability interface.

## Concurrency

Applications have handwritten `main()` functions. An active component can own a thread, mutable state, and a bounded inbox. It waits for events, handles them synchronously, and supports explicit shutdown. Components that do not need independent execution remain ordinary objects or functions.

[Services and concurrency](services.md) explains this with a small command-processing example. The [event communication reference](../../docs/concurrency-event-communication.md) records delivery and lifecycle conventions.

## Platform configuration

Profiles and overlays select implementations, device instances, resources, and logical names at build time. This borrows the declarative configuration approach of device trees while retaining NuttX's BSP and native configuration.

Application behavior stays in Rust; board wiring stays in the profile and platform integration. [Profiles and overlays](profiles.md) explains the build inputs.

## Developing and testing

Begin with host execution and component tests. Build the same application model for a selected NuttX configuration, then test device access and resource use on the target. Existing tests cover host behavior, simulator/QEMU execution, and on-board performance and memory use.

The [guide index](start-here.md) provides the development sequence. [Why NXRS?](motivation.md) explains the reasoning behind this architecture, and [Current status](status.md) summarizes implemented features and tests.
