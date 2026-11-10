# Guide index

The main guide covers general firmware development: build a Rust application, organize its components, select a target, and test it. Application-specific material is under [Examples](examples.md).

## Get started

Follow these pages in order for the first build:

1. [Getting started](getting-started.md) — install the host tools and run a small program.
2. [Your first application](first-application.md) — copy a self-contained Cargo package and understand its firmware metadata.
3. [Building for NuttX](firmware.md) — prepare a target toolchain, select a profile, and build an image.

The first programs need no sensors or application-specific libraries.

## Develop firmware

| Topic | Read when you need to |
| --- | --- |
| [Architecture overview](overview.md) | Understand NuttX, Rust, and component responsibilities |
| [Standard-library APIs](standard-library.md) | Use threads, time, collections, files, and sockets |
| [Services and concurrency](services.md) | Organize event processing and worker lifetimes |
| [Drivers and HALs](hardware.md) | Add a peripheral or reuse a hardware capability |
| [Networking and storage](io.md) | Structure communication and persistent data |
| [Profiles and overlays](profiles.md) | Select devices, resources, and target configuration |

## Test and evaluate

Use [Testing and debugging](testing.md) for commands and procedures, [Performance and memory](performance.md) for resource measurements, and [Troubleshooting](troubleshooting.md) for common setup and runtime errors.

## Read background or examples

[Why NXRS?](motivation.md) explains the project's motivation and design choices. [Current status](status.md) summarizes the implementation and completed validation.

[Examples](examples.md) collects the small learning programs, standard-library recipes, and application-specific code. IoT use cases illustrate composition without becoming prerequisites for the guide.

## Find a reference

The [engineering reference index](../../docs/README.md) links detailed design documents and evaluation reports. The website also provides a Reference index in the main navigation.

Search is available from the header with `/`, `Ctrl+K`, or `Command+K`. On a narrow screen, **Documentation navigation** opens the sidebar. Pages and navigation links remain usable without JavaScript.
