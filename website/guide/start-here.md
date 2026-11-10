# Guide index

Start with the section that matches what you want to do.

## Understand the project

[Why NXRS?](motivation.md) explains the motivation and the NuttX and Rust choices. The [architecture overview](overview.md) describes the components, and [Current status](status.md) summarizes the implementation, tests and ongoing work.

## Run and change a host application

Start with [Getting started](getting-started.md) to run the navigation example with synthetic inputs on your computer.

[Your first application](first-application.md) walks through the entry point. [Services and concurrency](services.md) explains event ownership, bounded delivery, overload handling and shutdown.

## Connect hardware and build firmware

[Drivers and HALs](hardware.md) explains where device-specific code belongs. [Profiles and overlays](profiles.md) covers device and resource selection. [Building for NuttX](firmware.md) describes toolchain setup and firmware builds.

Use the selected board's configuration and driver documentation to check which peripherals are available.

## Look at applications

The [NXRS examples](examples.md) cover navigation services and the camera/detection/gateway application, with links to their source code, configuration and test results.

## Test behavior and measure costs

[Testing and debugging](testing.md) covers host tests, target builds, emulation and on-board tests. [Performance and memory](performance.md) describes the benchmarks and resource measurements. Use [Troubleshooting](troubleshooting.md) to diagnose build or runtime failures.

## Look up a design decision

The [repository documentation index](../../docs/README.md) links to the engineering documentation. The website's Reference section contains design notes, implementation details and evaluation reports.

Search is available from the header with `/` or `Ctrl+K` / `Command+K`. On a narrow screen, use **Documentation navigation** to show the grouped sidebar. Reading pages and following navigation links works without JavaScript.
