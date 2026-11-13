# Guide index

Use this guide to take an ordinary Rust program from a host computer to a NuttX target, then grow it into an application with clear component boundaries. Start with the first-build path, or choose the task closest to your current work.

The tutorials use a small, independent Cargo application. The same development concepts apply to a controller, data logger, connected device, or another firmware project. Larger application-specific material is under [Examples](examples.md).

## Get started

Follow these pages in order for the first build:

1. [Getting started](getting-started.md) — install the host tools and run the checked-in program. You should see `Hello from NXRS`.
2. [Your first application](first-application.md) — create an independent Cargo package, change its behavior, and understand its firmware metadata.
3. [Building for NuttX](firmware.md) — prepare the target toolchain, select a profile, build the image, and run the same program at the NuttX shell.

The first programs need no peripherals or application-specific libraries. Host execution checks the program's behavior; the target tutorial checks its integration with NuttX. Physical peripheral testing follows when you add hardware.

## Choose your next task

| I want to… | Start here | What you will learn |
| --- | --- | --- |
| Decide whether this approach fits my project | [Why NXRS?](motivation.md) | The reasons for NuttX, Rust `std`, ordinary threads, and reusable components; costs and limits |
| Understand where my code belongs | [Architecture overview](overview.md) | Application, service, capability, driver, and platform responsibilities |
| Find the interface for an operation | [Interfaces and APIs](interfaces.md) | Development commands, runtime interaction, and the Rust APIs exposed to applications |
| Look up a Rust type or method | [Rust API reference](../api.md) | Library catalog, signatures, symbol search, and source |
| Turn a small program into a structured application | [Designing an application](designing-applications.md) | State ownership, component boundaries, delivery policy, and shutdown |
| Run a controller with an output and explicit command results | [Build a controller](controller.md) | Bounded admission, completion tickets, output state, and joined shutdown |
| Understand a build or change its inputs | [Build workflow](build-system.md) | How application metadata, Cargo, profiles, native preparation, and artifacts fit together |
| Move to another board or device instance | [Profiles and overlays](profiles.md) | Target configuration, bindings, resources, devices, and aliases |
| Investigate a failed command or runtime problem | [Troubleshooting](troubleshooting.md) | Which evidence to collect and where to look next |

## Develop firmware

| Topic | Read when you need to |
| --- | --- |
| [Architecture overview](overview.md) | Understand NuttX, Rust, and component responsibilities |
| [Standard-library APIs](standard-library.md) | Use threads, time, collections, files, and sockets |
| [Services and concurrency](services.md) | Organize event processing and worker lifetimes |
| [Drivers and HALs](hardware.md) | Add a peripheral or reuse a hardware capability |
| [Networking and storage](io.md) | Structure communication and persistent data |
| [Profiles and overlays](profiles.md) | Select devices, resources, and target configuration |

Read [Designing an application](designing-applications.md) before adding several independently running components. Then use the individual guides as needed. A parser, a file operation, and a long-running acquisition component have different ownership and execution needs.

## Test and evaluate

Use [Testing and debugging](testing.md) to choose the test environment and reproduce a failure. Use [Performance and memory](performance.md) to budget stacks, buffers, queues, and execution time. [Troubleshooting](troubleshooting.md) connects common symptoms to the relevant setup or runtime check.

Keep the distinction between a host test, a target build, an emulator run, and a measurement on hardware. Each answers a different question. The guide and linked reports identify which evidence is available.

## Read background or examples

[Why NXRS?](motivation.md) explains the project's motivation and design choices. [Current status](status.md) summarizes the implementation, completed validation, and the limits of that evidence.

[Examples](examples.md) collects the small learning programs, standard-library recipes, and application-specific code. IoT use cases illustrate composition without becoming prerequisites for the guide.

## Terms you will encounter

| Term | Meaning in this guide |
| --- | --- |
| Application | Your Rust entry point and product behavior; it constructs and connects the components it uses |
| Service | Reusable behavior with a defined interface and lifecycle; independent execution is optional |
| Capability HAL | A Rust interface for a subsystem, including resource ownership and lifecycle |
| Driver | A reusable device protocol or resource adapter used where the operation requires one |
| Profile | A declared selection of target, bindings, resources, and devices |
| Overlay | An explicit modification applied while resolving a profile |
| Resource | A declared bus, endpoint, or other native resource used to construct a device |
| Device and alias | A concrete configured instance and a logical application-facing name for it |
| Build receipt | Recorded build inputs and preparation state used to validate a prepared build |

The [architecture](overview.md), [interface](interfaces.md), and [build](build-system.md) guides connect these terms to actual source files and commands.

## Find a reference

The [Rust API reference](../api.md) links generated documentation for capabilities, services, messaging contracts, and chip protocols. Use its library catalog and rustdoc symbol search when you need a type or method signature.

The [engineering reference index](../../docs/README.md) links detailed design documents and evaluation reports. The website's **Reference** navigation also lists all published repository notes, grouped by subject. Use these when you need implementation contracts, target-specific setup, or the methodology behind a result.

Search is available from the header with `/`, `Ctrl+K`, or `Command+K`. On a narrow screen, **Documentation navigation** opens the sidebar and **On this page** opens the section index. The next-page links follow the complete guide across sections. Pages, diagrams, and navigation remain usable without JavaScript.
