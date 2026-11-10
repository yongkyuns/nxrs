# Guide index

The documentation has several entry points. Choose the question you have now; there is no need to read every engineering report before running an example.

## Understand the project

[Why NXRS?](motivation.md) explains the motivation, the NuttX and Rust choices, and the tradeoffs with other approaches. Continue with the [architecture overview](overview.md) for the component boundaries and [status and limitations](status.md) for what remains experimental.

## Run and change a host application

Start with [Getting started](getting-started.md). It runs a finite navigation example with synthetic inputs, without needing a board or a cross-toolchain.

Then read [Your first application](first-application.md), which follows the real entry point. [Services and concurrency](services.md) explains event ownership, bounded delivery, overload and shutdown. These are application contracts to understand before adding more producers or threads.

## Connect hardware and build firmware

Read [Drivers and HALs](hardware.md) to locate the device-specific code. [Profiles and overlays](profiles.md) explains how a product selects devices and resources. [Building for NuttX](firmware.md) then covers preparing the selected toolchain and producing a firmware image.

A profile name is not a promise that every peripheral is implemented or physically qualified. Check the referenced board configuration and its evidence before treating a build as hardware support.

## Look at applications

The [NXRS examples index](examples.md) connects the small navigation exercise to the camera/detection/gateway work. It identifies what each example is useful for and links to the implementation and qualification notes rather than presenting every example as equally mature.

## Test behavior and measure costs

Use [Testing and debugging](testing.md) for the validation layers, [Performance and memory](performance.md) for measurement scope, and [Troubleshooting](troubleshooting.md) when a command or runtime behavior differs from the guide.

## Look up a design decision

The [repository documentation index](../../docs/README.md) points to the maintained engineering documentation. The website's Reference navigation lists those documents separately from this introductory guide. Historical reports apply to their stated revisions and configurations; they are not a current, universal support matrix.

Search is available from the header with `/` or `Ctrl+K` / `Command+K`. On a narrow screen, use **Documentation navigation** to show the grouped sidebar. Reading pages and following navigation links works without JavaScript.
