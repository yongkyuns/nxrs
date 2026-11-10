<h1 align="center">
  <img src="docs/assets/nxrs-logo.png" alt="NXRS" width="360">
</h1>

<p align="center">
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/portable.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/portable.yml/badge.svg?branch=main" alt="Portable services CI"></a>
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/security.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/security.yml/badge.svg?branch=main" alt="Security checks"></a>
  <a href="https://github.com/yongkyuns/nxrs/actions/workflows/website.yml"><img src="https://github.com/yongkyuns/nxrs/actions/workflows/website.yml/badge.svg?branch=main" alt="Website build and deployment"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-73B9E2" alt="MIT license"></a>
  <a href="#why-this-project"><img src="https://img.shields.io/badge/status-experimental-F08A4B" alt="Experimental project"></a>
</p>

<p align="center">
  <a href="https://yongkyuns.github.io/nxrs/">Website</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/motivation/">Why NXRS?</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/start-here/">User guide</a> ·
  <a href="https://yongkyuns.github.io/nxrs/guide/examples/">Examples</a> ·
  <a href="https://yongkyuns.github.io/nxrs/reference/">Engineering reference</a>
</p>

**NXRS is an experimental Rust-based firmware development environment on top of
[Apache NuttX](https://nuttx.apache.org/).**

It explores ordinary Rust `std`, threads and event-driven components on embedded
MCUs. NuttX supplies the RTOS and board support. NXRS brings together the Rust
integration, peripheral wrappers, reusable firmware components and platform
profiles needed to try that approach in applications.

## Why this project?

Embedded projects often solve similar problems, but reusing the surrounding
firmware is difficult. Hardware constraints, vendor tooling, device APIs and
failure behavior vary. Making acquisition, processing, communication and
recovery work together reliably takes more than getting the first prototype to
run. Early decisions about portability, ownership and test boundaries can be
worthwhile investments across a product's life.

An established RTOS ecosystem is one way to make that investment. NXRS borrows
ideas from Zephyr's modular subsystems and device configuration, while exploring
a different application boundary: NuttX's standards-oriented C/POSIX interfaces
under an ordinary Rust standard library. The aim is to reuse familiar APIs and
application logic where practical, without pretending that an MCU is a complete
Linux environment.

Async frameworks such as Embassy address another useful tradeoff, particularly
for I/O-heavy workloads on small devices. NXRS instead explores ordinary threads
and synchronous code, without requiring applications and drivers to share an
async execution model. That choice has costs of its own: stacks, synchronization,
queues, allocation and toolchain maintenance all need attention.

Read [Why NXRS?](website/guide/motivation.md) for the fuller reasoning and primary
references. [Status and limitations](website/guide/status.md) explains what remains
experimental. Rust `std` support currently relies on maintained integration
patches, and API availability and hardware qualification depend on the target
and profile. NXRS is independent of the NuttX and Rust projects.

## Architecture

<!-- Regenerate both themes with: node docs/assets/render-architecture.mjs -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/nxrs-architecture-dark.svg">
  <img src="docs/assets/nxrs-architecture.svg" alt="NXRS architecture: reusable Rust firmware uses Rust std directly and peripheral wrappers where needed, above the NuttX RTOS and BSP. Platform profiles select hardware bindings at build time." width="1200">
</picture>

- **NuttX** supplies the actual RTOS, scheduling, board support packages,
  controller drivers and hardware resource registration through its C/POSIX
  environment.
- **Rust `std` and peripheral wrappers** connect Rust code to that foundation.
  Use ordinary files, sockets, threads, synchronization and time where supported;
  standard OS operations do not need an additional NXRS HAL.
- **Drivers, HALs, services and applications** separate device protocols,
  hardware capabilities, reusable behavior and product composition. These are
  responsibility boundaries, not a requirement to wrap every operation.
- **Platform profiles and overlays** select devices, resources, implementations
  and logical roles at build time. This is device-tree-like composition. NuttX
  still owns pin wiring, controller initialization and device registration.

Read the [architecture overview](website/guide/overview.md),
[HAL and platform boundaries](docs/hal-platform-architecture.md) and
[peripheral composition reference](docs/peripheral-composition.md) for details.

## Programming model

Applications use an ordinary, handwritten Rust `main()` to construct components,
connect them and own their lifecycle. There is no required application trait or
generated application graph.

Concurrency uses **ordinary Rust threads, blocking waits and synchronization**.
An async executor is not required. Active owners keep state and resources
together, receive typed events through bounded delivery paths, and handle each
event synchronously. Queue capacity, overload behavior, cancellation and joined
shutdown are explicit parts of component composition.

The intention is to keep reusable behavior separate from a particular board or
product policy while leaving concurrency and resource costs visible. The
[event-driven example](docs/event-driven-demo.md) shows the pattern; the
[event communication reference](docs/concurrency-event-communication.md) explains
ownership, delivery and lifecycle contracts.

## Getting started

### Run the host example

With Rust installed, clone the repository and use its pinned toolchain:

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

The explicit `host-demo` profile supplies synthetic IMU and GNSS inputs. The app
composes sensor, fusion and health services, exercises pause/resume, and shuts
down after the requested interval. It selects a logical IMU using
`devices::primary_imu()`; the profile supplies its concrete construction.
Physical sensor qualification is separate.

`NXRS_PLATFORM` selects a host, board or fixture profile. Without one, HAL crates
expose portable contracts and do not silently select mocks. Follow the
[getting-started guide](website/guide/getting-started.md) and
[application walkthrough](website/guide/first-application.md) for context.

### Build for NuttX

The repository's `cargo nxrs` frontend discovers applications and profiles:

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --help
```

Follow the [build reference](docs/build-system.md) for host dependencies,
toolchains and artifacts. After installing the required dependencies, the
following builds the event example for ESP32-S3 QEMU with synthetic sensors:

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock
```

The event-example qualification runner can execute the resulting image:

```sh
source target/qemu-tools/environment.sh
python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-xtensa)" \
  --machine esp32s3 \
  --image target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin \
  --log target/build/event-demo/esp32s3-qemu-mock/console.log
```

The [NuttX QEMU reference](docs/nuttx-qemu.md) covers additional integration
fixtures. Host tests, build/link checks, emulation and physical measurements
establish different kinds of evidence.

### Development checks

```sh
cargo test --locked
python3 tools/check-architecture.py
```

Run these from the repository root. Target-specific and instrumented suites have
their own setup; see [tests and qualification](tests/README.md).

## Application examples

The navigation example is a small exercise in service composition. The
experimental IoT camera workflow uses capture, detection, gateway delivery and
client-visible state to exercise a larger application. See the
[examples index](website/guide/examples.md) and
[camera delivery reference](docs/camera-detection-delivery.md).

These are functional experiments rather than finished consumer products. Model,
board, memory and failure-case evidence belongs to each application's reports.
More examples are documented as the project develops.

## Performance and memory

The use of `std`, threads and reusable components needs to be evaluated against
real application budgets. Track deployed code and static data, queue and payload
storage, thread stacks, peak heap use, and end-to-end event latency and
throughput. Compare representative workloads with references such as Zephyr,
with hardware, compiler settings and measurement scope made explicit.

Existing studies describe particular configurations, not universal overhead or
production qualification:

- [Rust std footprint](docs/rust-std-footprint.md): code size, API selection and
  memory accounting.
- [Std channel performance](docs/std-channel-performance.md): delivery paths,
  queue storage, thread costs and the scope of measurements.
- [RTOS comparison](docs/rtos-comparison.md): reference workloads for NuttX,
  Zephyr and Embassy. Its native-adapter NuttX fixture does not measure the full
  NXRS `std` model.
- [MPSC comparison fixture](tests/mpsc-comparison/README.md): the Rust workload
  using NuttX `std::sync::mpsc::sync_channel` and Zephyr's native `k_msgq`.

## Documentation

Use the [guide index](website/guide/start-here.md) to choose a reading path.
The website separates project background, usage, examples and measurements.
Engineering-reference pages are rendered directly from the repository's
Markdown sources; historical reports retain their qualification limits.

| Where to look | What it covers |
| --- | --- |
| [Why NXRS?](website/guide/motivation.md) | Motivation, choices and tradeoffs |
| [Guide index](website/guide/start-here.md) | Practical reading paths |
| [Examples](website/guide/examples.md) | Application context and evidence |
| [Documentation index](docs/README.md) | Architecture and implementation references |
| [Build system](docs/build-system.md) | Applications, profiles, toolchains and artifacts |
| [Rust std on NuttX](docs/nuttx-std.md) | Integration and supported configurations |
| [Tests and qualification](tests/README.md) | Fixture and comparison guides |
| [Upstream sources](upstream/README.md) | Pinned sources and maintained patches |
| [Editor setup](docs/editor-setup.md) | Local development configuration |

See [AGENTS.md](AGENTS.md) for implementation conventions,
[SECURITY.md](SECURITY.md) for vulnerability reporting, and
[website maintenance](website/README.md) for site changes and publication.

## License

See [LICENSE](LICENSE).
