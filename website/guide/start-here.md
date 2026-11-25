# Guide index

Start here to build your first NuttX image, or find a guide for the task you’re working on.

## Get started

1. [Getting started](getting-started.md) covers the host tools and the NXRS checkout.
2. [Your first application](first-application.md) creates an independent Cargo package and runs it on your computer.
3. [Building for NuttX](firmware.md) builds that package for NuttX and runs it in QEMU.

You can complete all three guides without a board.

## Choose your next task

| If you want to… | Read |
| --- | --- |
| Decide whether NXRS fits your project | [Why NXRS?](motivation.md) and [Current status](status.md) |
| See where your code belongs | [Architecture overview](overview.md) and [Interfaces and APIs](interfaces.md) |
| Choose component boundaries and plan for failures | [Designing an application](designing-applications.md) |
| Follow a working program or integration | [Examples](examples.md) |
| Look up a Rust type or method | [Rust API reference](../api.md) |

## Develop firmware

| If you want to… | Read |
| --- | --- |
| Use threads, time, collections, files, or sockets | [Standard-library APIs](standard-library.md) |
| Add a bounded worker and shut it down cleanly | [Services and concurrency](services.md) |
| Send commands and confirm the output changed | [Build a controller](controller.md) |
| Add a peripheral or use a device capability | [Drivers and HALs](hardware.md) |
| Send data or retain records | [Networking and storage](io.md) |
| Understand a firmware build and its outputs | [Build workflow](build-system.md) |
| Select a board, change device wiring, or configure a variant | [Profiles and overlays](profiles.md) |

## Test and evaluate

Use [Testing and debugging](testing.md) to decide what to test on the host, in an emulator, or on a board. [Performance and memory](performance.md) links measured results and helps you choose a workload. If you already have an error, start with [Troubleshooting](troubleshooting.md).

## Read background or examples

Read [Why NXRS?](motivation.md) for the reasoning behind the design. The [examples index](examples.md) includes small Rust programs and complete integrations, including a simulated light you can switch from Home Assistant.

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


## Find a reference

Use the [Rust API reference](../api.md) for types and methods. Use the [engineering reference index](../../docs/README.md) for design contracts, target setup, and measurement reports. On the site, **Reference** lists all the published engineering notes by topic.

Press `/` or use **Search** in the header. On a narrow screen, open **Documentation navigation** to switch pages or **On this page** to jump to a section.
