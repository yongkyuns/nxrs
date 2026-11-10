# Why NXRS?

NXRS uses ordinary Rust and its standard library to build reusable firmware, with NuttX providing the operating system and board support. The motivation is to carry familiar software development and testing practices into embedded work while accounting for the hardware's memory, timing and peripheral requirements.

The [architecture overview](overview.md) describes the implementation. [Current status](status.md) covers the available components, completed validation and ongoing work.

## The work around the application

Embedded development often requires adapting otherwise familiar software practices. Memory, timing, power, peripherals and failure recovery need careful attention. Hardware families, vendor tooling and software ecosystems also differ substantially, making it difficult to reuse the surrounding firmware even when the application logic is similar.

Many projects need the same basic operations: acquire data, process it, communicate a result, retain state, and recover from failures. Bringing a new product up reliably means integrating these operations with shutdown, network recovery, storage, updates and testing.

Early decisions about modularity and portability can save substantial work over a product's life, especially for teams maintaining several products. Clear subsystem interfaces make it easier to reuse components, test them independently and change hardware without rewriting application behavior. This is the starting point for NXRS.

## Choosing an RTOS also chooses a way of working

An RTOS ecosystem includes drivers, configuration, subsystems, build tools and conventions. Adopting that foundation gives a team a common way to organize its firmware and reduces the amount of integration work it has to maintain itself.

[Zephyr](https://docs.zephyrproject.org/latest/introduction/index.html) provides an integrated device model, configuration system and modular subsystems. NXRS borrows ideas from this approach, particularly the separation of reusable behavior from device and product configuration.

## Why NuttX

NuttX is used by projects such as [PX4](https://docs.px4.io/v1.16/en/concept/architecture), [openvela](https://github.com/open-vela/docs), and [Sony's Spresense SDK](https://developer.sony.com/spresense/development-guides/introduction_en.html). Its particular attraction for NXRS is its standards-oriented application interface.

[NuttX emphasizes POSIX and ANSI interfaces](https://nuttx.apache.org/docs/latest/index.html). Files, sockets, threads and synchronization use familiar APIs. This gives host and embedded applications a common set of interfaces, with platform-specific hardware access kept underneath them. Available facilities still depend on the board and NuttX configuration.

Those interfaces also provide a basis for compiling Rust's standard library for NuttX. Applications can use ordinary Rust thread, socket and file APIs while NuttX handles scheduling, operating-system services and board support.

NXRS maintains the integration patches and target-specific build support needed for its supported configurations. The [Rust std integration reference](../../docs/nuttx-std.md) documents those patches, available APIs and tests.

## Why ordinary threads

Async Rust is well suited to many embedded workloads. [Embassy](https://embassy.dev/book/) provides an executor and embedded libraries around that model. Tasks can suspend while waiting for I/O without reserving a separate thread stack for every task, which is useful for concurrent I/O on devices with limited RAM.

Async also changes how drivers, libraries and application code interact. They must cooperate with the executor and handle cancellation, task lifetimes and operations that must not block it. Threaded code has a different set of concerns, including stack allocation, synchronization and scheduling.

NXRS uses ordinary Rust threads, blocking waits and synchronous functions. The intention is to retain familiar standard-library interfaces without requiring an application-wide async I/O model. Queue capacities, thread stacks and shutdown behavior are part of the design and are included in testing and measurement.

Zephyr also supports threads and POSIX-compatible APIs, and NuttX has its own configuration and driver conventions. The emphasis in NXRS is on using standard Rust APIs directly for ordinary OS operations, with additional structure for hardware integration and reusable firmware components.

## Reusable components and product configuration

An application composes components in an ordinary Rust `main()`. Active modules own their state and resources, receive typed events through bounded delivery paths, and handle each event synchronously. Startup, overload handling and joined shutdown are explicit parts of the component interfaces.

A chip driver handles the device protocol. A capability HAL provides an interface such as IMU acquisition. A service adds reusable behavior, and the application connects these components. Ordinary file and socket operations use `std` directly; a component needs a worker thread only when it has concurrent work to perform.

Platform profiles provide device-tree-like configuration by selecting implementations, binding resources and naming logical devices outside reusable application code. NuttX retains responsibility for board support, pin configuration and peripheral initialization. The [profiles guide](profiles.md) and [peripheral composition reference](../../docs/peripheral-composition.md) describe the implementation.

This organization supports host testing and reuse across applications. The navigation and camera applications use it for service composition, acquisition, processing and communication.

## Tests and evaluations

NXRS has basic validation in place: host tests, NuttX simulator and QEMU execution, and physical-device measurements. Existing evaluations cover event delivery, channel storage, thread stacks, memory footprint and the camera application. The [testing guide](testing.md) and [performance guide](performance.md) link to the commands and results.

Using `std` on a microcontroller requires attention to code size, static data, thread stacks, queue storage and heap use. These costs continue to be measured as applications grow. Comparisons with Zephyr use recorded hardware, compiler options, workloads and scheduling settings so the results can be reproduced.

## Applications and ongoing work

The [application examples](examples.md) include navigation services and an IoT camera with detection and gateway integration. Further work adds devices and applications, improves reusable subsystems, and extends performance and memory evaluations.
