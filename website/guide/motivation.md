# Why NXRS?

NXRS started from a practical goal: use familiar Rust programming tools and reusable software components for firmware, while keeping hardware configuration separate from application behavior.

## Reuse in embedded development

Embedded work adds constraints to otherwise familiar software problems. Memory, timing, power, peripherals, and recovery need attention, and hardware families come with different tools and interfaces. At the same time, many products need similar software: device access, processing, communication, storage, and control.

Getting those parts to work reliably together takes time. Choosing clear module interfaces and test boundaries early can save substantial work over a product's life, especially when a team maintains several products or hardware revisions. The software stack is therefore an important design decision, not just a toolchain selection.

## An RTOS brings an ecosystem

An RTOS provides more than scheduling. Its drivers, configuration system, subsystems, and development conventions shape the firmware built around it.

[Zephyr](https://docs.zephyrproject.org/latest/introduction/index.html) combines these facilities into an integrated development environment. Its device configuration and reusable subsystems are useful influences for NXRS. NXRS adopts similar separation between reusable behavior and product configuration, while using NuttX as its operating-system foundation.

## Why NuttX

[NuttX emphasizes POSIX and ANSI interfaces](https://nuttx.apache.org/docs/latest/). Applications can use familiar file, socket, thread, and synchronization interfaces. This makes it a useful foundation for code shared between host and embedded environments.

NuttX is also used by projects such as [PX4](https://docs.px4.io/main/en/concept/architecture), [openvela](https://doc.openvela.com/document?id=198&language=en&version=trunk), and [Sony's Spresense SDK](https://developer.sony.com/spresense/development-guides/introduction_en.html). Those projects apply the operating system to their own requirements; NXRS focuses on a general Rust firmware environment above it.

The standards-oriented interface makes Rust's standard library a natural application boundary. NXRS compiles and integrates `std` for its NuttX targets, so applications can use ordinary Rust threads, synchronization, files, and sockets. NuttX remains responsible for scheduling, board support, and native resources. The [integration reference](../../docs/nuttx-std.md) records the maintained patches and target configuration.

## Ordinary Rust concurrency

Async frameworks such as [Embassy](https://embassy.dev/book/) are useful for embedded workloads with many concurrent I/O waits and tight memory budgets. Async execution also affects driver interfaces, cancellation, and which operations may block an executor.

NXRS takes a different approach: ordinary Rust threads and synchronous functions, with event-driven components where independent execution is needed. Applications do not need an async runtime or async-specific interfaces throughout the stack. Thread stacks, synchronization, and queue storage are included in the performance and memory evaluations.

The choice is about the programming model and reuse of standard APIs. Applications still need deliberate ownership, scheduling, error handling, and resource budgets.

## Reusable components and configuration

Drivers implement device protocols, HALs expose hardware capabilities, services provide reusable behavior, and applications compose the system in a handwritten `main()`. Standard OS operations use `std` directly.

Platform profiles supply device-tree-like composition: device instances, resources, implementations, and logical names are selected outside reusable application code. NuttX's BSP and native configuration continue to own pin setup and controller initialization.

For example, an IoT controller can keep its command processing, record format, and connection policy while changing the physical device bindings for a new board revision. The application determines those behaviors; NXRS supplies the integration and component structure.

## Validation and continued development

Basic validation already includes host tests, target integration checks, simulator/QEMU runs, and on-board evaluations. Existing studies measure event delivery, memory use, and application workloads, including comparisons with reference RTOS implementations.

The remaining work is practical: expand supported configurations, improve components and tooling, and continue measuring their costs as applications grow. [Current status](status.md), [Testing and debugging](testing.md), and [Performance and memory](performance.md) link the implementation and results.
