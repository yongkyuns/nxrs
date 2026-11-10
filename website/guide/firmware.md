# Building for NuttX

A firmware build combines the Rust application, its dependencies, a NuttX configuration, and the target toolchain. The `cargo nxrs` frontend prepares these inputs and produces the image and build records.

Use the general-purpose `std-demo` application for the first target build. Its standard-library recipes do not require application-specific sensors or services.

## Inspect the build configuration

From the repository root:

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock --dry-run
```

`--app` names an application in the checkout. `--profile` selects a target configuration. `--dry-run` prints the plan without compiling the image.

For your own package, use `--manifest-path`, as shown in [Your first application](first-application.md).

## Prepare the tools

The commands in this page use a POSIX shell. Install the native dependencies listed in the [build reference](../../docs/build-system.md) and the [NuttX QEMU guide](../../docs/nuttx-qemu.md). The host Rust installation and the MCU compiler are separate toolchains.

For the existing ESP32-S3 setup:

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
```

The build frontend uses the selected SDK and a private patched copy for Rust/NuttX integration. It leaves the globally installed SDK unchanged. The [Rust std integration reference](../../docs/nuttx-std.md) describes the source pins and target checks.

## Build the standard-library application

```sh
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock
```

The default output directory is:

```text
target/build/std-demo/esp32s3-qemu-mock/
```

It contains the image and records for the selected configuration. On ESP32-S3, the merged image is under `nuttx/nuttx.merged.bin`.

Follow the target's launch instructions in the [QEMU guide](../../docs/nuttx-qemu.md) to boot the image. At the NuttX shell, the application's registered command is:

```text
nsh> std_demo --case channels
```

The application prints its channel checks and ends with `STD_DEMO PASS cases=1`. Run `std_demo` without arguments for all recipes.

## ARM QEMU test runner

The repository also has an automated runner for the Cortex-M33 `mps2-an521` configuration. Prepare the ARM tools and emulator described in the [standard-application workflow](../../.github/workflows/std-apps-self-hosted.yml), then build and run:

```sh
cargo nxrs build --app std-demo --profile mps2-an521-mock
python3 tests/host/run-std-apps-nuttx.py \
  --qemu "$(command -v qemu-system-arm)" \
  --app std-demo \
  --image target/build/std-demo/mps2-an521-mock/nuttx/nuttx \
  --log target/build/std-demo/mps2-an521-mock/console.log
```

This runner is specific to MPS2 ARM QEMU; it does not accept the ESP32-S3 merged image. It runs the application repeatedly, checks output and cleanup, and saves the console log. The workflow records the required QEMU UART support and compiler versions.

## Build for a physical board

Choose a board and profile from `cargo nxrs platforms` and the checked-in configuration. For example, `pico2-mock` builds a Pico 2 image; it is separate from the MPS2 emulator configuration.

Check the board guide for wiring, image layout, and flashing commands. [CoreS3 setup](../../docs/m5stack-cores3.md) is one hardware-specific reference. Flashing, key provisioning, and eFuse programming are separate steps from building.

## Rebuilds and cleanup

Normal builds reuse compatible Cargo, NuttX, and SDK output. Use `--clean` when a fresh build is needed. Keep the app, profile, toolchain, and logs together when investigating a failure.

Continue with [Profiles and overlays](profiles.md) for configuration and [Testing and debugging](testing.md) for test procedures.
