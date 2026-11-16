<h1 align="center">
  <img src="docs/assets/nxrs-logo.png" alt="NXRS" width="360">
</h1>

<p align="center">
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/portable.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/portable.yml/badge.svg?branch=main" alt="Portable services CI"></a>
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/security.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/security.yml/badge.svg?branch=main" alt="Security checks"></a>
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/website.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/website.yml/badge.svg?branch=main" alt="Website build and deployment"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-73B9E2" alt="MIT license"></a>
</p>

<p align="center">
  <a href="https://yongkyuns.github.io/nxrs/">Website</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/getting-started/">Getting started</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/start-here/">User guide</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/motivation/">Why NXRS?</a> ·
  <a href="https://yongkyuns.github.io/nxrs/reference/">Reference</a>
</p>

**NXRS is an experimental, general-purpose Rust firmware development environment built on [Apache NuttX](https://nuttx.apache.org/).**

NuttX supplies the RTOS and board support. NXRS brings together Rust standard-library integration, peripheral interfaces, reusable firmware components, and platform profiles. Applications use ordinary Rust `std`, threads, and synchronous functions, with event-driven services where independent execution is needed.

## Why this project?

Embedded projects often need similar software: device access, processing, communication, storage, and control. Hardware constraints and fragmented tooling make that software harder to reuse. Separating application behavior, hardware integration, and product configuration helps with testing and later hardware revisions.

NXRS uses NuttX's standards-oriented C/POSIX interfaces underneath the Rust standard library. This allows applications to use familiar Rust APIs while NuttX handles operating-system and board responsibilities. The component and profile design also borrows ideas from Zephyr's reusable subsystems and declarative device configuration.

Concurrency uses ordinary threads rather than requiring an async runtime. Active components own their state and receive typed events; other behavior remains in regular functions and objects. Applications choose their communication and delivery policies.

Read [Why NXRS?](website/guide/motivation.md) for the reasoning and [Current status](website/guide/status.md) for the implementation and tests. NXRS is an independent project built on NuttX.

## Getting started

With Git, Rust, and your host's native build tools installed:

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --bin hello
```

The program prints `Hello from NXRS`. It is an ordinary, self-contained Cargo application with no hardware dependencies. [Your first application](website/guide/first-application.md) explains its manifest and how to use it as a starting point.

The standard-library application provides short examples of collections, channels, synchronization, and deadlines:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

Neither example requires a platform profile. Follow the [getting-started guide](website/guide/getting-started.md) for setup and the [firmware guide](website/guide/firmware.md) for NuttX builds.

## Architecture

<!-- Regenerate both themes with: node docs/assets/render-architecture.mjs -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/nxrs-architecture-dark.svg">
  <img src="docs/assets/nxrs-architecture.svg" alt="NXRS architecture: reusable Rust firmware uses Rust std and peripheral interfaces above the NuttX RTOS and BSP. Platform profiles select hardware bindings at build time." width="1200">
</picture>

- **NuttX** provides scheduling, board support, controller drivers, and native resources.
- **Rust std and peripheral wrappers** expose OS facilities and hardware access to Rust.
- **Drivers, HALs, services, and applications** separate device protocols, capabilities, reusable behavior, and product composition.
- **Profiles and overlays** select devices, resources, and target configuration. NuttX retains native pin and controller setup.

Use `std` directly for operations it already describes. A handwritten Rust `main()` constructs the components needed by an application; there is no required generated application graph.

See the [architecture overview](website/guide/overview.md), [services guide](website/guide/services.md), and [hardware guide](website/guide/hardware.md).

## Building firmware

The repository's `cargo nxrs` frontend selects the application and target configuration:

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock --dry-run
```

After installing the target prerequisites, remove `--dry-run` to build the image. [Building for NuttX](website/guide/firmware.md) covers setup, outputs, and execution. [Profiles and overlays](website/guide/profiles.md) explains the configuration inputs.

Use `cargo nxrs config --app event-demo --platform cores3` to search, review, and save native NuttX settings for a selected build. The [configuration guide](website/guide/profiles.md#configure-nuttx) covers prerequisites, shared fragment ownership, Save & sync, and scriptable checks. The next ordinary build consumes the saved settings.

## Tests and evaluations

Basic validation includes host tests, target build and ABI checks, simulator/QEMU execution, and on-board performance and memory evaluations.

```sh
cargo test --locked
python3 tools/check-architecture.py
```

Performance and memory remain part of ongoing development. Evaluations include image size, stacks, heap use, queues, event latency, and throughput, with reference comparisons such as Zephyr. See the [test index](tests/README.md), [performance guide](website/guide/performance.md), [event-path measurements](docs/std-channel-performance.md), [footprint study](docs/rust-std-footprint.md), and [RTOS comparisons](docs/rtos-comparison.md).

## Documentation and examples

The [user guide](website/guide/start-here.md) covers general firmware development. [Examples](website/guide/examples.md) collects small learning programs and optional application-specific code, including IoT applications. These examples use the framework; they do not define its application domain.

| Topic | Documentation |
| --- | --- |
| Project background | [Why NXRS?](website/guide/motivation.md) |
| Application development | [Getting started](website/guide/getting-started.md), [Your first application](website/guide/first-application.md) |
| Design and interfaces | [Designing an application](website/guide/designing-applications.md), [Interfaces and APIs](website/guide/interfaces.md) |
| Standard APIs and execution | [Standard library](website/guide/standard-library.md), [Services](website/guide/services.md) |
| Hardware and configuration | [Drivers and HALs](website/guide/hardware.md), [Profiles](website/guide/profiles.md) |
| Build workflow | [From inputs to firmware](website/guide/build-system.md) |
| Communication and files | [Networking and storage](website/guide/io.md) |
| Tests and debugging | [Testing guide](website/guide/testing.md), [Troubleshooting](website/guide/troubleshooting.md) |
| Implementation details | [Engineering reference](docs/README.md) |

See [AGENTS.md](AGENTS.md) for implementation conventions, [SECURITY.md](SECURITY.md) for vulnerability reporting, and [website maintenance](website/README.md) for site changes.

## License

See [LICENSE](LICENSE).
