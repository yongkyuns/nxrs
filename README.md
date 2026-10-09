# nxrs

Portable Rust firmware apps composed from reusable services, with device and
operating-system I/O behind common HAL contracts.

For measured platform tradeoffs on ESP32-S3, start with the independent
[NuttX, Zephyr and Embassy analysis](docs/rtos-comparison.md). The
[Rust std footprint analysis](docs/rust-std-footprint.md) and
[nxrs before/after walkthrough](tests/service-footprint/DEVELOPMENT.md)
explain application-level choices. Other RTOSes remain isolated experiments,
not production dependencies.

## App-owned main

`app/` contains alternative firmware apps. Each production MCU image selects
exactly one app. The app owns its handwritten `src/main.rs`, service composition,
workflows and lifecycle. One app may use several services and threads; Recorder
and Monitor are workflows, not separate applications. There is no platform
launcher, app registry, required App trait, or service-graph configuration language.

The native replay profile acquires camera, storage and transport through the
capability-local `nxrs-camera`, `nxrs-storage`, and
`nxrs-transport` facades. `app/nxrs/src/main.rs` still owns product
composition, while concrete replay/file/UDP providers stay below those facades.
Filesystem and socket construction remain inside providers; ordinary timekeeping
uses `std::time` directly.

```sh
cargo run --locked -p nxrs-applications \
  --features nxrs-applications/cli,nxrs-camera/native,nxrs-storage/native,nxrs-transport/native \
  --bin nxrs -- \
  input.gray 2 2 gray8 10 new-recording-directory 127.0.0.1:9000
```

The app's `cli` feature admits the binary and its std-backed bounded owner wait
transport. Provider selection remains independent on each HAL capability facade.
The existing no_std library target selects neither the CLI runtime nor a provider.
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
| `hal/imu`, `hal/gnss`, `hal/camera`, `hal/storage`, `hal/transport` | Capability-local public facades, provider-independent `api/` contracts, and optional concrete providers |
| `hal/common`, `hal/support/nuttx` | Shared error values and narrow NuttX provider support |
| `driver` | Repository-owned protocols; upstream drivers remain in `external/nuttx` |
| `platform/firmware`, `platform/nuttx` | Cargo firmware frontend plus board/build profiles and target integration; not an app host |
| [`upstream`](upstream/README.md) | Dependency patchsets, revision pins and upstreaming proposals; no vendored source trees |
| `tests` | Portable scenarios, host oracles and isolated target qualification images |
| `tools`, `docs`, `external` | Focused scripts, documentation and pinned upstream sources |

The normative resource-ownership model is documented in
[HAL capability architecture](docs/hal-platform-architecture.md). Applications
normally compose services; services normally acquire the capabilities they own
internally, while an app may access a capability facade directly when that is the
simpler ownership boundary. Each facade selects its own physical, native, web,
replay, or mock provider at build time. There is no global `hal/platform`
package, provider registry, giant HAL object, xtask, generated app manifest, or
general runtime framework.

## Event-driven architecture demo

`app/event-demo` is the reference app for service-owned execution **and
service-owned HAL resources**. The app knows only services and their connections;
it does not depend on IMU/GNSS HALs, mock providers, or concrete devices.

```sh
cargo build --locked \
  -p nxrs-event-demo -p nxrs-imu -p nxrs-gnss \
  --features nxrs-imu/mock,nxrs-gnss/mock --bin event-demo
target/debug/event-demo --duration-ms 2000
```

The build explicitly selects `nxrs-imu/mock` and `nxrs-gnss/mock`.
`ImuService::start()` and `GnssService::start()` acquire those capabilities
internally and move the resources into owner threads. Selecting future physical
providers does not change app/service source.

IMU, GNSS, and fusion use the shared std-backed `nxrs-service-event`
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
cargo build --locked \
  -p nxrs-dual-imu-demo -p nxrs-imu \
  --features nxrs-imu/mock --bin dual-imu-demo
target/debug/dual-imu-demo
~~~

The demo pauses one IMU while the other continues, then resumes it. CI also
proves that the IMU-only image does not inherit `nxrs-gnss-mock` from
`app/event-demo`, and that running without an IMU provider fails explicitly
instead of silently selecting a mock.

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
cargo firmware --list-apps
cargo firmware --list-platforms

# Raspberry Pi Pico 2
cargo firmware --app event-demo --platform pico2-mock

# ESP32-S3 QEMU profile (install the pinned ESP/QEMU tools once first)
bash tools/install-qemu-tools.sh
cargo firmware --app event-demo --platform esp32s3-qemu-mock

# ARMv8-M / Cortex-M33 QEMU proxy
cargo firmware --app event-demo --platform mps2-an521-mock
~~~

The default artifacts are under:

~~~text
target/firmware/<app>/<platform>/
~~~

For example:

~~~text
target/firmware/event-demo/pico2-mock/nuttx/nuttx.bin
target/firmware/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin
target/firmware/event-demo/mps2-an521-mock/nuttx/nuttx
~~~

`cargo firmware` is a repository Cargo alias backed by the small
`nxrs-firmware` host tool. It resolves:

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

The app manifest owns only firmware-entry properties such as binary name,
NuttX command name, priority and stack size. The platform profile owns board,
Rust target, toolchain, Kconfig and HAL provider selection. Developers do not
pass individual `nxrs-imu/mock`, target triples or Kconfig values.

The common `tools/build-nuttx-std-app.sh` remains an internal backend because
NuttX configuration, Make, ABI inspection and final image linking are not Cargo
operations. It now accepts explicit arguments from the Cargo frontend; there are
no per-app/per-target build wrapper scripts or deployment profiles.

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
  --image target/firmware/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin \
  --log target/firmware/event-demo/esp32s3-qemu-mock/console.log

python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-arm)" \
  --machine mps2-an521 \
  --image target/firmware/event-demo/mps2-an521-mock/nuttx/nuttx \
  --log target/firmware/event-demo/mps2-an521-mock/console.log
~~~

See [HAL capability architecture](docs/hal-platform-architecture.md),
[build system](docs/build-system.md), and
[event-driven demo](docs/event-driven-demo.md).

## Development and qualification

The root toolchain is Rust 1.90.0. Default workspace members are host-compatible;
target-only fixtures require explicit selection. The destructive
NuttX resource probe is a non-default workspace member and builds its own image.

```sh
cargo test --locked
cargo run --locked -p nxrs-simulator
python3 tools/check-architecture.py
python3 tests/host/test-native-runner.py
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
