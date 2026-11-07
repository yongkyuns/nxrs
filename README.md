# nxrs

Portable Rust firmware apps composed from reusable services, with device and
operating-system I/O behind common HAL contracts.

[Measuring Rust std in nxrs's event-driven core](docs/std-channel-performance.md)
explains the bounded inbox architecture and its current timing and memory
evidence on ESP32-S3.

For measured platform tradeoffs on ESP32-S3, start with the independent
[NuttX, Zephyr and Embassy analysis](docs/rtos-comparison.md). The secondary
[Rust std footprint analysis](docs/rust-std-footprint.md) examines application
costs. The [before/after demo](tests/service-footprint/README.md) provides build
recipes. Other RTOSes remain isolated experiments, not production dependencies.

## App-owned main

`app/` contains alternative firmware apps. Each production MCU image selects
exactly one app. The app owns its handwritten `src/main.rs`, service composition,
workflows and lifecycle. One app may use several services and threads; Recorder
and Monitor are workflows, not separate applications. There is no platform
launcher, app registry, required App trait, or service-graph configuration language.

Each HAL capability is one Cargo crate: for example, package `nxrs-camera` is
imported as Rust crate `camera`. The crate reexports its contract and selected
provider types; implementation modules remain private. One `NXRS_PLATFORM`
profile selects all HAL bindings for a build. With no profile, only portable
contracts are selected and no mock is implicit.

The desktop OS profiles acquire camera, storage and transport through those
capability crates. `app/nxrs/src/main.rs` owns product composition, while
concrete replay/file/UDP providers stay inside the HALs. Filesystem and socket
construction remain inside providers; ordinary timekeeping uses `std::time`
directly.

```sh
NXRS_PLATFORM=linux cargo run --locked -p nxrs-applications \
  \
  --bin nxrs -- \
  input.gray 2 2 gray8 10 new-recording-directory 127.0.0.1:9000
```

The app's `cli` feature admits the binary and its std-backed bounded owner wait
transport. Select the actual OS with `NXRS_PLATFORM=linux`, `macos`, or
`windows`; these HALs share the same Rust replay/file/socket behavior. The existing no_std library target selects
neither the CLI runtime nor a provider unless a profile is explicitly supplied.
It is a migration/testing compatibility target, not a mandatory app framework.
The application package is named `nxrs-applications` under `app/nxrs`.

This does **not** yet port the camera app binary to NuttX or the browser. The
separate ordinary-main/std probes qualify those execution environments. The
native app now has one app-owned wait point: replay advertises the next useful
camera poll deadline, normal idle time blocks until that deadline, and Busy sink
retries use an explicit bounded retry timer without re-polling the camera. The
camera/recorder/monitor remain co-located so borrowed frames do not cross a
thread boundary.

## Source ownership

| Directory | Responsibility |
| --- | --- |
| `app/nxrs` | Normal binary, portable workflow modules and existing composition |
| `service` | Reusable camera, recording and telemetry service modules |
| `hal/<capability>` | One crate per capability; contracts in `src/api.rs`, private implementations in `src/platform/`, and minimal ABI glue beside each provider in its `ffi/` directory |
| `hal/common` | Shared BLE/Wi-Fi request identities and bounded payload storage |
| `shared/messaging` | Layer-neutral generic channels and owned delivery results |
| `driver` | Repository-owned protocols; upstream drivers remain in `external/nuttx` |
| `platform/build`, `platform/nuttx` | Cargo firmware frontend plus board/build profiles and target integration; not an app host |
| [`upstream`](upstream/README.md) | Dependency patchsets, revision pins and upstreaming proposals; no vendored source trees |
| [`tests`](tests/README.md) | Guide to portable scenarios, shared comparison tools and isolated qualification images |
| `tools`, `docs`, `external` | Focused scripts, documentation and pinned upstream sources |

The normative resource-ownership model is documented in
[HAL capability architecture](docs/hal-platform-architecture.md). Applications
normally compose services; services normally acquire the capabilities they own
internally, while an app may access a capability directly when that is the
simpler ownership boundary. A single profile selects providers for the whole
build, such as `linux`, `mock`, or `host-demo`. There is no global `hal/platform`
package, provider registry, giant HAL object, xtask, generated app manifest, or
general runtime framework.

The [peripheral composition design](docs/peripheral-composition.md) describes
reusable Rust chip drivers, embedded-hal resource adapters and validated profile
overlays. Its [implementation plan](docs/peripheral-composition-plan.md) gives
ordered tasks and acceptance checks. The new configuration syntax and simulator
are staged work; existing build commands below describe current behavior.

## Event-driven architecture demo

`app/event-demo` is the reference app for service-owned execution **and
service-owned HAL resources**. The app knows only services and their connections;
it does not depend on IMU/GNSS HALs, mock providers, or concrete devices.

```sh
NXRS_PLATFORM=host-demo cargo build --locked \
  -p nxrs-event-demo -p nxrs-imu -p nxrs-gnss \
  --bin event-demo
target/debug/event-demo --duration-ms 2000
```

The `host-demo` profile selects mock IMU and GNSS implementations alongside
the native desktop bindings. `ImuService::start()` and `GnssService::start()`
acquire those capabilities internally and move the resources into owner
threads. Selecting future physical providers does not change app/service source.

IMU, GNSS, and fusion use the shared std-backed `nxrs-messaging`
transport: one bounded inbox and one wait point per active owner, cloned typed
producers for fan-in, run-to-completion handlers, explicit overload behavior,
and joined shutdown. `HealthService` remains synchronous. HAL contracts/facades remain `no_std`; active
services use the qualified Rust `std` execution model. See
[event-driven demo](docs/event-driven-demo.md).

### Multi-app / multi-instance proof

`app/dual-imu-demo` is a second ordinary firmware app that reuses the same
`nxrs-navigation-services` package but composes a different product graph:
two `ImuService` instances and two `FusionService` instances. Each IMU service
independently calls the HAL acquisition path at `start()`; with the mock
platform both instances begin at sequence 1, proving there is no shared singleton
sensor state.

~~~sh
NXRS_PLATFORM=host-imu cargo build --locked \
  -p nxrs-dual-imu-demo -p nxrs-imu \
  --bin dual-imu-demo
target/debug/dual-imu-demo
~~~

The `host-imu` profile selects native camera/storage/transport and a mock IMU;
GNSS remains unbound. This qualifies synthetic sensor ownership without
implying that other radios or sensors are implemented.

The demo pauses one IMU while the other continues, then resumes it. CI also
checks that the IMU-only image does not acquire the GNSS capability and that
running without an IMU binding fails explicitly instead of silently selecting
a mock.

Together with `app/event-demo` already running unchanged across the
ESP32-S3, MPS2/Cortex-M33 and Pico 2 product-platform profiles, this qualifies
the intended multi-app/multi-instance configuration model without a registry,
instance-ID framework, or generated service graph.

## Cargo firmware builds

Cargo is the canonical developer-facing firmware build interface. App execution
metadata lives in the app's `Cargo.toml`; board/target/HAL bindings stay in the
selected product-platform profile.

~~~sh
# Discover buildable firmware apps and platforms.
cargo nxrs apps
cargo nxrs platforms

# Raspberry Pi Pico 2
cargo nxrs build --app event-demo --profile pico2-mock

# ESP32-S3 QEMU profile (install the pinned ESP/QEMU tools once first)
bash tools/install-qemu-tools.sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock

# ARMv8-M / Cortex-M33 QEMU proxy
cargo nxrs build --app event-demo --profile mps2-an521-mock
~~~

The default artifacts are under:

~~~text
target/build/<binary>/<profile>/
~~~

For example:

~~~text
target/build/event-demo/pico2-mock/nuttx/nuttx.bin
target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin
target/build/event-demo/mps2-an521-mock/nuttx/nuttx
~~~

`cargo nxrs build` is a repository Cargo alias backed by the host-only
`nxrs-build` Rust controller. It resolves:

~~~text
app Cargo metadata
        +
product platform
        |
        v
common NuttX build backend
        |
        v
Cargo Rust build + NuttX final image
~~~

The app's firmware metadata table owns binary name, NuttX command name, priority
and stack size. Optional `required-capabilities` metadata supplies early admission
checks; Cargo dependencies select components. The platform profile owns board,
Rust target, toolchain, Kconfig and HAL bindings. Developers do not pass
provider-specific Cargo features, target triples or Kconfig values.

The Rust controller in `platform/build` owns NuttX configuration, Cargo/Make
invocation, ABI inspection and final image linking. The common
`tools/build-nuttx-std-app.sh` is a compatibility adapter to that controller;
there are no per-app/per-target build wrapper scripts. Ordinary additive Cargo
features select optional components. See [build and platform selection](docs/build-system.md)
for feature controls and the remaining modularity, incremental-build and reuse work.

The MPS2 profile remains a Cortex-M33 execution proxy rather than RP2350
emulation. The Pico 2 profile separately builds and ABI-checks the actual
`raspberrypi-pico-2:nsh` image. Current profiles still bind mock IMU/GNSS
providers, so this is software/build portability rather than physical sensor
qualification.

For direct QEMU execution after a build:

~~~sh
source target/qemu-tools/environment.sh
python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-xtensa)" \
  --machine esp32s3 \
  --image target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin \
  --log target/build/event-demo/esp32s3-qemu-mock/console.log

python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-arm)" \
  --machine mps2-an521 \
  --image target/build/event-demo/mps2-an521-mock/nuttx/nuttx \
  --log target/build/event-demo/mps2-an521-mock/console.log
~~~

See [HAL capability architecture](docs/hal-platform-architecture.md),
[build system](docs/build-system.md), and
[event-driven demo](docs/event-driven-demo.md).

## Development and qualification

For Zed/clangd configuration, see [editor setup](docs/editor-setup.md).

The root toolchain is Rust 1.90.0. Default workspace members are host-compatible;
target-only fixtures require explicit selection. The destructive
NuttX resource probe is a non-default workspace member and builds its own image.

```sh
cargo test --locked
NXRS_PLATFORM=mock cargo test --locked -p nxrs-simulator
NXRS_PLATFORM=mock cargo run --locked -p nxrs-simulator
python3 tools/check-architecture.py
NXRS_PLATFORM=linux python3 tests/host/test-native-runner.py
cargo check --locked -p nxrs-applications --no-default-features --lib \
  --target thumbv6m-none-eabi
```

The independent native test invokes the actual app binary and decodes persisted
records and UDP datagrams. Existing portable scenario, small-stack, storage
failure, queue saturation and lifecycle assertions remain in place.

```sh
git submodule update --init
bash tools/build-nuttx-sim.sh
python3 tests/host/test-nuttx-sim.py target/nuttx-sim/nuttx/nuttx

bash tools/install-qemu-tools.sh
bash tools/build-nuttx-qemu.sh
source target/qemu-tools/environment.sh
python3 tests/host/test-nuttx-sim.py "$(command -v qemu-system-xtensa)" \
  --qemu-image target/nuttx-qemu/nuttx/nuttx.merged.bin \
  --log target/nuttx-qemu/console.log
```

These core-only pipelines run real NuttX with synthetic sensors, tmpfs, loopback
UDP and fault injection. They are distinct from the [ordinary-main/std tests](docs/nuttx-std.md),
which use explicitly patched SDK copies: RV32 and ESP32-S3 execute under QEMU;
Pico 2 currently has build/link/ABI evidence only. The [browser pthread probe](docs/browser-threads.md)
runs in actual Chrome and Safari. Neither emulator nor browser results establish
physical-device behavior, all libc APIs, production timing, or full app/HAL portability.

See [source architecture](docs/architecture.md), [service contracts](docs/portable-services.md),
[native I/O semantics](docs/native-backend.md), [buffer placement](docs/buffer-placement.md),
[NuttX simulator](docs/nuttx-sim.md), [QEMU](docs/nuttx-qemu.md),
[descriptor ownership](docs/nuttx-ownership.md), and [storage faults](docs/nuttx-storage.md).
