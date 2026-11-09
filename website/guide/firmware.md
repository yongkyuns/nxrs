# Building for NuttX

A host pass is the first step. An MCU image additionally needs the selected NuttX source, board configuration, compiler, Rust target integration and ABI checks. Use the repository frontend rather than manually assembling a different target command for every application.

## Discover and inspect before building

From the repository root:

```sh
cargo nxrs apps
cargo nxrs platforms
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock --dry-run
```

A dry run describes a build plan. It does not qualify the compiler, SDK, dependency resolution, target ABI or physical resources. Keep the actual build evidence separate.

## Prepare the target environment

The examples here use a POSIX shell and the repository’s existing ESP32-S3 QEMU setup. Install the host/native prerequisites described by the [build reference](../../docs/build-system.md) and [QEMU guide](../../docs/nuttx-qemu.md). Do not assume the host-only Rust toolchain includes an Xtensa compiler.

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
```

The installation script obtains the pinned target tools. Review its output and preserve the selected versions. Cargo build scripts do not fetch missing native source trees on your behalf.

NuttX std integration uses checked private SDK copies and maintained patches. Do not patch the globally installed compiler as a substitute. The target checks bind the actual headers, libc and std used for that image.

## Build the event demo

```sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock
```

The default output directory is:

```text
target/build/event-demo/esp32s3-qemu-mock/
```

The ESP32-S3 merged image used by the existing test runner is:

```text
target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin
```

Inspect the build logs and receipts as well as the binary. A filename left by an earlier attempt is not evidence that the latest attempt succeeded.

## Execute the emulated image

```sh
source target/qemu-tools/environment.sh
python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-xtensa)" \
  --machine esp32s3 \
  --image target/build/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin \
  --log target/build/event-demo/esp32s3-qemu-mock/console.log
```

This runs the demo qualification path with synthetic sensor providers. Read the runner result and saved console log. QEMU execution does not establish physical sensor support or absolute hardware timing.

## Other qualification profiles

```sh
cargo nxrs build --app event-demo --profile mps2-an521-mock
cargo nxrs build --app event-demo --profile pico2-mock
```

The MPS2 configuration is a Cortex-M33 execution proxy, **not RP2350 emulation**. The Pico 2 profile separately builds and checks the actual board image. A build/link/ABI result alone is not a physical-board execution result.

For a real CoreS3 product, begin with the [CoreS3 guide](../../docs/m5stack-cores3.md). Choose the exact board/profile and verify the wiring and device evidence before flashing. This guide intentionally does not supply a generic flashing command: erase ranges, image layout and board ownership are target-specific.

## Incremental builds and cleanup

The frontend retains compatible Cargo, NuttX and prepared SDK state. It still checks the selected inputs and regenerates success evidence. `--clean` is available for an intentional fresh build; use the owning command rather than deleting arbitrary shared tool directories.

Builds and packaging are not permission to flash, provision keys or change eFuses. Those are separate operations with board-specific procedures. See the [build-system reference](../../docs/build-system.md) and [Rust std integration report](../../docs/nuttx-std.md).
