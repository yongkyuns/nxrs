# Troubleshooting

Start with the command that failed, the selected application and profile, and the complete error output.

## `cargo nxrs` is not recognized

Run it from the NXRS repository root and check that `.cargo/config.toml` is present. It is a repository Cargo alias. For external applications, keep the application in its own workspace and pass its manifest to the frontend from the NXRS checkout.

## A host example requests a board or device

The introductory `hello`, `worker`, and `std-demo` examples do not need a hardware profile. Check that you are running the command from [Getting started](getting-started.md), rather than a device-specific application.

Remove an old `NXRS_PLATFORM` assignment when returning to standard-library-only tests. When using a HAL, select the intended host or hardware profile explicitly.

## A HAL is unavailable

Check the selected profile and its capability bindings. Without a profile, the HAL exposes its portable contract and leaves hardware implementations unbound. Confirm that the required OS implementation and resources are included in the selected configuration.

See [Profiles and overlays](profiles.md) and the HAL's module documentation.

## A Rust target, compiler, or linker is missing

Install the target's prerequisites, then check the toolchain selected by the build frontend. The host Rust compiler is separate from an MCU SDK. Use the versions recorded in the [Rust/NuttX integration guide](../../docs/nuttx-std.md).

## Cargo reports a lockfile change

`--locked` rejects dependency-resolution changes. When deliberately changing a manifest, regenerate its lockfile with Cargo, review it, and restore locked builds. The introductory example has its own lockfile, separate from the NXRS workspace.

## The image or emulator cannot be found

Check that the build completed and use the output directory for the application and profile you built. When using the installed ESP32-S3 tools, source `target/qemu-tools/environment.sh` in the shell that runs the emulator.

ARM MPS2 and ESP32-S3 use different emulator binaries and image formats. Follow the matching commands in [Building for NuttX](firmware.md).

## A peripheral does not respond

Check board wiring, BSP initialization, device registration, the resolved resource bindings, and the device's expected identity. Inspect the owning HAL's I/O and readiness errors before changing service behavior.

## Shutdown hangs

Check which component still owns a sender, blocking I/O operation, callback, or worker. A shutdown path must wake waits and join producers before releasing the resources they use. [Services and concurrency](services.md) describes the ownership pattern.

## Performance differs from a report

Compare the hardware, image, compiler settings, clocks, memory placement, workload, and measurement method. Use the test commands attached to the relevant report.

## Report a problem

Include a small reproducer and the setup information listed above. Use [GitHub issues](https://github.com/yongkyuns/nxrs/issues) for bugs and the repository's [security policy](../../SECURITY.md) for vulnerability reports. Remove credentials and private device data from logs before sharing them.
