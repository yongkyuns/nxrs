# A Rust firmware environment on NuttX

NXRS is an **experimental** environment for developing firmware with ordinary Rust and its standard library on embedded MCUs. NuttX supplies the operating system and board support. NXRS brings together the target integration, Rust peripheral access, reusable components and product configuration.

The aim is similar to the benefit NuttX offers C developers through its POSIX-oriented environment: keep useful application code close to familiar language and library interfaces, rather than requiring a special application runtime for every board.

> NXRS is independent of the Apache NuttX and Rust projects. It is not an official distribution of either. Maintained SDK patches and target-specific qualification are currently part of using Rust `std` on NuttX.

## What belongs where

| Part | Responsibility |
| --- | --- |
| NuttX | RTOS scheduling, POSIX/C interfaces, board support, controller initialization and resource registration |
| Rust standard library | Familiar threads, synchronization, time, file and socket operations where supported by the target |
| Peripheral wrappers and chip drivers | Rust access to resources and reusable chip or protocol behavior |
| Capability HALs | Portable subsystem contracts, resource acquisition and managed device lifetimes |
| Services | Reusable processing, state and event-driven behavior |
| Applications | Product composition, connections and lifecycle in a handwritten `main()` |
| Platform profiles | Build-time selection of devices, resources, aliases and implementations |

This is not a mandatory runtime chain. A file operation that `std::fs` already expresses does not need another NXRS HAL. A simple component does not need a thread merely because it is a separate crate.

## The execution model

Active owners keep their state and resources together. They block waiting for typed events, process an event synchronously, then wait again. Queue capacities, full-queue behavior, cancellation and shutdown are explicit. Ordinary Rust functions remain the default inside an owner.

The framework does not require an async executor. `std::thread`, blocking waits and synchronization provide the execution model. This does not make arbitrary blocking work safe in every handler: deadlines, resource contention and scheduling still need design and measurement.

## Choose a starting point

Begin with the [host demo](getting-started.md), which uses explicitly selected synthetic sensors. Then follow [the actual application](first-application.md) and [service ownership](services.md). Move to [platform configuration](profiles.md) and [NuttX firmware builds](firmware.md) once the host behavior is clear.

The [architecture reference](../../docs/architecture.md) and [HAL ownership guide](../../docs/hal-platform-architecture.md) contain deeper implementation rules. The [reference library](../../docs/README.md) also includes research and historical qualification reports; those reports must be read with their stated revisions and scope.

## Experimental boundaries

A successful host test is not a hardware qualification. A linked MCU image is not evidence that a physical device executed it. A QEMU pass is not an interrupt-latency measurement. Support depends on the exact application, profile, SDK, peripheral provider and test evidence.

Keep performance and memory budgets visible from the beginning. The [measurement guide](performance.md) explains what to record and how to compare reference implementations without turning a narrow result into a general performance claim.
