# Current status

NXRS combines Rust standard-library integration for NuttX, reusable firmware components, platform profiles, and application build tooling.

## Available components

The repository includes Rust resource adapters and drivers, capability HALs, reusable services, and ordinary Cargo applications. The build frontend selects the application and profile, prepares the target integration, and records the resulting configuration and image.

Applications can use standard-library facilities directly. [Standard-library APIs](standard-library.md), [Drivers and HALs](hardware.md), and [Profiles and overlays](profiles.md) describe the main interfaces and responsibilities.

## Completed validation

Existing validation includes host unit and integration tests, target build and ABI checks, firmware execution in the NuttX simulator and QEMU, and on-board evaluations. Measurements cover channel/event delivery, stacks, heap use, image size, and application workloads.

| Area | Tests and results |
| --- | --- |
| Rust/NuttX integration | [Standard-library integration](../../docs/nuttx-std.md) |
| Application and component behavior | [Repository test index](../../tests/README.md) |
| Simulator execution | [NuttX simulator](../../docs/nuttx-sim.md) |
| Emulated target execution | [NuttX QEMU](../../docs/nuttx-qemu.md) |
| Event delivery | [Channel performance](../../docs/std-channel-performance.md) |
| Resource use | [Rust std footprint](../../docs/rust-std-footprint.md) |
| Reference comparisons | [RTOS comparisons](../../docs/rtos-comparison.md) |

Each report records its tested configuration. Application-specific results are linked from [Examples](examples.md).

## Target configuration

Rust `std` integration uses maintained patches and target-specific build support. Select the board, OS configuration, device bindings, and resource budgets through the build frontend and profiles.

`cargo nxrs platforms` lists the platform families. The [build reference](../../docs/build-system.md) and board guides describe their setup. A host program using only `std` does not need hardware bindings.

## Ongoing work

Development continues on device support, component reuse, build integration, and performance and memory evaluation. The [repository issues](https://github.com/yongkyuns/nxrs/issues) track the active work.
