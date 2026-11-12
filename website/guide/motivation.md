# Why NXRS?

NXRS started from a practical goal: use familiar Rust programming tools and reusable software components for firmware, while keeping hardware configuration separate from application behavior.

The central idea is to extend the standards-oriented development model provided by NuttX into Rust. An application should be understandable as a Rust program: its dependencies are in Cargo, its behavior is in ordinary source, and its resources have visible owners. This page explains the tradeoffs behind that choice. The [architecture overview](overview.md) describes how it is implemented.

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

### What event-driven means here

A component blocks until it has work or a deadline expires, then handles the event with its private state. Related synchronous operations can run in the same handler. Separate workers are useful when independent timing, blocking I/O or isolation of mutable state calls for them.

Message passing makes ownership transfers and backlog explicit. It does not imply broadcasting every update, using a middleware broker, or putting a queue between every pair of functions. A command can have a dedicated destination and acknowledgement. A replaceable status update can use a different delivery policy. The application chooses the topology and the interface states what delivery means.

See [Services and concurrency](services.md) for a runnable example and for the distinction between admission and completion.

## Reusable components and configuration

Drivers implement device protocols, HALs expose hardware capabilities, services provide reusable behavior, and applications compose the system in a handwritten `main()`. Standard OS operations use `std` directly.

Platform profiles supply device-tree-like composition: device instances, resources, implementations, and logical names are selected outside reusable application code. NuttX's BSP and native configuration continue to own pin setup and controller initialization.

For example, an IoT controller can keep its command processing, record format, and connection policy while changing the physical device bindings for a new board revision. The application determines those behaviors; NXRS supplies the integration and component structure.

## Design choices and their costs

| Choice | What it makes easier | What the application still has to decide |
| --- | --- | --- |
| Use Rust `std` where the target supports it | Reuse host-side code and familiar APIs | Which OS facilities are enabled and what their resource cost is |
| Keep dependencies in Cargo | See and review the software composition | Feature choices, native dependencies and target compatibility |
| Separate profiles from behavior | Reuse an application across supported board variants | Required devices, wiring, capabilities and qualification |
| Use synchronous APIs and explicit workers | Trace execution, ownership and shutdown | Stack budgets, priorities, blocking limits and deadlines |
| Give queues finite capacity | Bound admitted backlog | Backpressure, rejection, replacement and recovery policy |
| Keep native glue near its owner | Isolate target ABI and SDK operations | Callback lifetime, resource release and target testing |

These choices also limit where NXRS is a natural fit. A small system that cannot afford NuttX and thread stacks may be better served by a smaller execution model. A crate requiring an unsupported OS facility needs adaptation. A hard deadline needs measured timing and scheduling evidence; safe Rust and a bounded queue do not establish that deadline by themselves.

The [performance guide](performance.md) explains the budgets to measure. The [standard-library guide](standard-library.md) explains how to assess dependencies without treating host success as target qualification.

## What portability promises

Portability here means that reusable behavior depends on a stable Rust contract and receives its concrete resources from the selected integration. It does not mean every board has the same devices or that a missing device is silently simulated.

A supported profile can provide a capability. An unbound capability reports its documented unavailable result. Mock and simulation profiles are explicit test choices. Optional software functionality, such as file replay or UDP submission, is selected through additive Cargo features and its explicit constructor.

Those distinctions let an application make a useful decision: require a capability before building, handle an optional capability at acquisition, or exercise portable behavior in a test fixture. [Profiles and overlays](profiles.md) and [Choose an interface](interfaces.md) cover those paths.

## Keep the application in control

NXRS supplies integration and components. The application owns startup order, relationships between services, fault response and shutdown. There is no mandatory application trait, generated service graph or registration macro needed for the introductory program.

Start with a small source project, introduce a component when its boundary becomes useful, and add a worker when the workload needs independent execution. The [application design guide](designing-applications.md) applies this approach to a generic controller with storage and communication.

## Validation and continued development

Basic validation already includes host tests, target integration checks, simulator/QEMU runs, and on-board evaluations. Existing studies measure event delivery, memory use, and application workloads, including comparisons with reference RTOS implementations.

The remaining work is practical: expand supported configurations, improve components and tooling, and continue measuring their costs as applications grow. [Current status](status.md), [Testing and debugging](testing.md), and [Performance and memory](performance.md) link the implementation and results.
