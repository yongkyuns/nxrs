# Building for NuttX

A firmware build uses the selected NuttX source, board configuration, compiler and Rust target integration. The `cargo nxrs` frontend prepares these inputs, builds the application and runs the target ABI checks.

## Inspect the build configuration

From the repository root:

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock --dry-run
```

`--dry-run` prints the build plan without compiling or running the firmware.

## Prepare the target environment

These examples use a POSIX shell and the ESP32-S3 QEMU setup. Install the prerequisites in the [build reference](../../docs/build-system.md) and [QEMU guide](../../docs/nuttx-qemu.md), including the target compiler.

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
```

The script installs the pinned target tools. Initialize the source submodules before building; Cargo build scripts use the existing checkout.

The Rust/NuttX integration uses private SDK copies with maintained patches. Keep these separate from the globally installed compiler. Target checks use the headers, libc and standard library selected for the firmware image.

## Build the event demo

```sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock
```

Build outputs are placed under:

```text
target/build/event-demo/esp32s3-qemu-mock/
```

The ESP32-S3 test runner uses this merged image:

```text
target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin
```

Check that the build completed successfully before running its output. The directory also contains build logs and records of the selected inputs.

## Run the emulated image

```sh
source target/qemu-tools/environment.sh
python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-xtensa)" \
  --machine esp32s3 \
  --image target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin \
  --log target/build/event-demo/esp32s3-qemu-mock/console.log
```

The runner tests the application with synthetic sensor providers in QEMU. Results and the console transcript are saved with the build.

## Other target profiles

```sh
cargo nxrs build --app event-demo --profile mps2-an521-mock
cargo nxrs build --app event-demo --profile pico2-mock
```

The MPS2 configuration runs on a Cortex-M33 QEMU model; it does not emulate the RP2350. The Pico 2 profile builds and checks the actual board image. On-board execution uses the corresponding device test procedure.

For CoreS3, follow the [CoreS3 guide](../../docs/m5stack-cores3.md). Confirm the board profile, wiring, image layout and flashing procedure before writing to the device.

## Incremental builds and cleanup

The frontend reuses compatible Cargo, NuttX and prepared SDK state while checking the selected inputs. Use `--clean` for a fresh build.

Building and packaging do not modify a device. Flashing, key provisioning and eFuse changes use separate board-specific procedures. See the [build-system reference](../../docs/build-system.md) and [Rust std integration report](../../docs/nuttx-std.md).
