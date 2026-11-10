# Why NXRS?

NXRS is an experiment in building reusable firmware with ordinary Rust and its standard library, using NuttX for the operating system and board support. It started with a question: how much of the way we develop and test software on a computer can remain useful on a microcontroller, without hiding the constraints that make embedded work different?

This is the reasoning behind the project, not a claim that this approach is better for every device or team. The [architecture overview](overview.md) describes the implementation; [status and limitations](status.md) describes its current boundaries.

## The work around the application

Embedded development often requires adapting otherwise familiar software practices. Memory, timing, power, peripherals and failure recovery matter in ways that are easy to overlook on a larger system. Hardware families, vendor tooling and software ecosystems also differ substantially. Reusing an algorithm is not the same as reusing the surrounding firmware.

At the same time, many projects solve similar problems: acquire data, process it, communicate a result, retain state, and recover when one part stops behaving as expected. A new product still needs those pieces to work together reliably. Sensor acquisition alone does not settle questions about shutdown, network loss, storage failure, updates, testing or resource ownership.

For a team maintaining more than one product, early decisions about modularity and portability can therefore matter well beyond the first working prototype. A reusable subsystem needs a clear contract and test boundary, not just code moved into a library. The motivation for NXRS is to explore those boundaries while keeping the application code familiar.

## Choosing an RTOS also chooses a way of working

An RTOS is more than a scheduler. Its drivers, configuration system, subsystems, build tools and conventions influence how a team structures its code. Buying into an established ecosystem can be the right way to avoid rebuilding that foundation.

[Zephyr](https://docs.zephyrproject.org/latest/introduction/index.html) is a useful example: its integrated device model, configuration and modular subsystems give projects a substantial starting point. NXRS borrows ideas about separating reusable behavior from device and product configuration. That is an architectural influence, not an attempt to reproduce Zephyr's ecosystem or a claim to match its supported hardware.

## Why NuttX is an interesting foundation

NuttX is already used as a foundation in projects such as [PX4](https://docs.px4.io/v1.16/en/concept/architecture), [openvela](https://github.com/open-vela/docs), and [Sony's Spresense SDK](https://developer.sony.com/spresense/development-guides/introduction_en.html). The particular attraction for NXRS is its standards-oriented application interface.

[NuttX emphasizes POSIX and ANSI interfaces](https://nuttx.apache.org/docs/latest/index.html). Files, sockets, threads and synchronization can be expressed through familiar interfaces rather than a separate application API for each operation. This offers a useful common boundary between embedded and host development.

It is not equivalent to running a complete Linux environment on an MCU. Available APIs, device behavior, resources and timing still depend on the architecture, board configuration and workload. Portable application logic can share an interface while requiring different implementations and separate tests underneath it.

That standards-based boundary also makes Rust's standard library worth exploring. The aim is to compile ordinary Rust code against a standard library adapted for NuttX, rather than ask every application to adopt a different thread, socket or file API. NuttX remains responsible for the actual RTOS and BSP.

NXRS currently maintains integration patches and target-specific build support. This is not a claim that every upstream NuttX Rust target supplies a ready-to-use `std` distribution. The [Rust std integration reference](../../docs/nuttx-std.md) records the implementation and qualification scope. Toolchain maintenance is part of the experiment's cost, not something the standard API removes.

## Why ordinary threads rather than a required async runtime?

Async Rust is a useful choice for embedded work. [Embassy](https://embassy.dev/book/) provides an executor and embedded libraries around that model; it is not another conventional RTOS. Suspending an I/O operation without reserving a separate thread stack for every task can suit workloads with many concurrent waits and a tight RAM budget. Actual memory use still depends on the task state and application.

That choice also affects interfaces. Drivers and libraries need to cooperate with the executor, and applications need to account for cancellation, task lifetimes and code that must not block the executor. Those are different concerns from preemptive threads, not evidence that async is inherently unreliable or that threaded code is automatically simpler.

NXRS explores the other tradeoff: ordinary Rust threads, blocking waits, synchronous functions and explicit ownership. Application code can use familiar standard-library facilities where the target supports them, without a required async executor or an application-wide async I/O convention. Threads still have stacks; shared state can still deadlock; an unbounded queue can still exhaust memory. Familiar APIs do not make those costs disappear.

This is also not a strict division between ecosystems. Zephyr supports threads and POSIX-compatible APIs, and NuttX has its own configuration and driver conventions. NXRS makes framework choices too. The intended distinction is to keep standard OS operations standard, and add project-specific structure only where hardware integration and reusable firmware behavior need it.

## The structure NXRS does choose

The application composes components in an ordinary Rust `main()`. Reusable active modules keep state and resources with an owner, receive typed events through bounded delivery paths, and handle each event synchronously. Startup, overload behavior and joined shutdown are explicit parts of that contract.

Drivers, capability HALs, services and applications address different concerns. A chip driver handles the device protocol. A HAL presents a capability such as IMU acquisition. A service adds reusable behavior. The application decides how those pieces participate in the product. Not every operation needs another wrapper or its own thread.

Platform profiles provide device-tree-like configuration: selecting implementations, binding resources and naming logical roles outside reusable application code. They do not replace NuttX's board support, pin configuration or peripheral initialization. The [profiles guide](profiles.md) and [peripheral composition reference](../../docs/peripheral-composition.md) explain the current boundary and remaining work.

The expectation is that this separation will make host tests and component reuse practical. Real applications provide a way to test whether those boundaries hold up as more devices and requirements are added.

## What needs measuring

Using `std` on an MCU is less conventional than an allocation-free `no_std` design. Code size, static data, thread stacks, queue storage and peak heap use all need attention. So do wake-up latency, context switching, overload, shutdown and the work between receiving an event and completing it.

A small channel benchmark cannot by itself qualify a complete camera application. A successful host test cannot establish interrupt latency or peripheral behavior on a board. Comparisons with systems such as Zephyr need comparable hardware, compiler options, workloads and explicitly stated measurement scope.

The [performance guide](performance.md) points to the measurements and their limitations. Results are evidence for particular configurations, not a promise of negligible overhead or a general hard real-time guarantee.

## Where the project is now

NXRS has working application examples, including navigation/service composition and an experimental IoT camera workflow with detection and gateway integration. These are places to exercise the architecture, not finished consumer products or general security guarantees. The [examples index](examples.md) separates runnable host exercises, application integration and board-specific qualification reports.

The project remains experimental because the portability boundaries, resource costs and maintenance burden still need to be tested as applications grow. More examples and measurements are added as that work progresses. The point is to learn whether this is a useful way to build firmware, and to keep the evidence visible when it is not.
