# Troubleshooting

Start with the command that failed, the selected application and profile, and the complete error output.

## Find the failing boundary

| Where it fails | First check | Continue with |
| --- | --- | --- |
| Before the frontend runs | Working directory, Cargo alias, and Rust installation | [Getting started](getting-started.md) |
| During selection | Manifest, profile name, required capabilities, and overlay order | [Profiles and overlays](profiles.md) |
| During target preparation | Pinned sources, toolchain, SDK, Kconfig tools, and Python environment | [Building for NuttX](firmware.md#prepare-ubuntu) |
| During compilation or linking | The first compiler/linker error and selected target | [Build workflow](build-system.md) |
| After boot | Loaded image, NSH command, and complete boot log | [Firmware tutorial](firmware.md#boot-qemu) |
| During device I/O or shutdown | Resource ownership and the capability's error/lifecycle contract | [Drivers and HALs](hardware.md) and [Services](services.md) |

Change one input at a time. Keep the failed command and original output so you can tell whether the next attempt addresses the same problem.

## `cargo nxrs` is not recognized

Run it from the NXRS repository root and check that `.cargo/config.toml` is present. It is a repository Cargo alias. For external applications, keep the application in its own workspace and pass its manifest to the frontend from the NXRS checkout.

`cargo nxrs --help` checks whether the controller can run. If ordinary `cargo` is missing too, finish the Rust installation and open a shell whose PATH includes the installed toolchain.

## A host example requests a board or device

The introductory `hello`, `worker`, and `std-demo` examples do not need a hardware profile. Check that you are running the command from [Getting started](getting-started.md), rather than a device-specific application.

Remove an old `NXRS_PLATFORM` assignment when returning to standard-library-only tests. When using a HAL, select the intended host or hardware profile explicitly.

## A HAL is unavailable

Check the selected profile and its capability bindings. Without a profile, the HAL exposes its portable contract and leaves hardware implementations unbound. Confirm that the required OS implementation and resources are included in the selected configuration.

See [Profiles and overlays](profiles.md) and the HAL's module documentation.

Distinguish an unbound capability from a configured device that failed to initialize. Optional software functions such as file replay or queued recording have their own Cargo features and explicit constructors. Enabling a hardware profile does not enable every software feature, and an absent device must not silently become mock data.

## A profile or overlay is rejected

Inspect the exact profile with `cargo nxrs profile --platform <name>` and repeat the same `--overlay` arguments in the same order as the failed build. Check unknown keys, removed aliases, missing resources, and conflicts in physical addresses or native pin claims.

Changing a device `compatible` or a resource `binding` replaces the complete record. Supply the required fields for the replacement. Changing a wake table or resource Kconfig table replaces that table as a unit. An overlay cannot change the OS, target, BSP, or composition mode; create a named profile for those changes.

## Cargo reports missing or stale prepared NuttX inputs

Build the selected NuttX configuration through `cargo nxrs build`. Plain `cargo check` cannot substitute for native headers, target configuration, and resource archives prepared by the frontend. Repeat the frontend build after changing a profile, binding declaration, compiler, or native source.

Do not copy a receipt from another target or edit generated files to silence the error. If you are configuring an editor, select the completed build as described in [Testing and debugging](testing.md#editor-setup).

## A Rust target, compiler, or linker is missing

Install the target's prerequisites, then check the toolchain selected by the build frontend. The host Rust compiler is separate from an MCU SDK. Use the versions recorded in the [Rust/NuttX integration guide](../../docs/nuttx-std.md).

The pinned ESP32-S3 tutorial bundle requires Linux x86-64. Its installer and the active Python environment are both part of the setup. If a host build unexpectedly invokes the Xtensa compiler, open a fresh shell: the emulator environment should be sourced only inside the tutorial's subshell.

## Cargo reports a lockfile change

`--locked` rejects dependency-resolution changes. When deliberately changing a manifest, regenerate its lockfile with Cargo, review it, and restore locked builds. The introductory example has its own lockfile, separate from the NXRS workspace.

## The image or emulator cannot be found

Check that the build completed and use the output directory for the application and profile you built. When using the installed ESP32-S3 tools, source `target/qemu-tools/environment.sh` inside the QEMU subshell shown in [Building for NuttX](firmware.md#boot-qemu). This keeps target-compiler environment changes out of later host commands.

ARM MPS2 and ESP32-S3 use different emulator binaries and image formats. The first-firmware tutorial uses ESP32-S3; the [QEMU reference](../../docs/nuttx-qemu.md) describes the other target setup.

## NSH runs an old application or cannot find the command

Check `[package.metadata.nxrs.firmware]` for the shell command name. Cargo package names, Rust binary names, and NSH command names can differ. For example, the repository's `std-demo` binary registers the `std_demo` command.

Stop the old emulator, confirm the new build succeeded, and boot the exact image from that build. A running emulator keeps executing the previously loaded image. Check the NSH exit status with `echo $?` after invoking the command, and keep startup output if it fails before printing the expected result.

## A peripheral does not respond

Check board wiring, BSP initialization, device registration, the resolved resource bindings, and the device's expected identity. Inspect the owning HAL's I/O and readiness errors before changing service behavior.

`ls /dev` in NSH checks whether an endpoint was registered. It does not establish electrical wiring, bus address, pin routing, or successful device initialization. Compare the profile with the board schematic and run the owning device's acquisition test. A configured interrupt failure should remain visible instead of being treated as an implicit request for polling.

## A queue fills or an owner stops making progress

Record offered, accepted, rejected, and completed work separately. Check the queue capacity, handler duration, blocking I/O, and scheduling priorities. Increasing capacity absorbs a larger burst but does not fix a producer that permanently outruns its consumer.

A blocked `send` waits for capacity. Check that the consumer can run and that no circular wait prevents it from draining. For a nonblocking policy, account for the returned rejected value and report the failure according to the application's contract. [Services and concurrency](services.md) explains the delivery choices.

## Shutdown hangs

Check which component still owns a sender, blocking I/O operation, callback, or worker. A shutdown path must wake waits and join producers before releasing the resources they use. [Services and concurrency](services.md) describes the ownership pattern.

A `Stop` command can wait behind a full data inbox. Closing one sender does not disconnect a channel while other sender clones remain. Check the capability's explicit cancellation path and retained terminal error before assuming that dropping a handle has joined its worker.

## Performance differs from a report

Compare the hardware, image, compiler settings, clocks, memory placement, workload, and measurement method. Use the test commands attached to the relevant report.

Read loss and throughput together, and distinguish allocated stack from observed stack use. A smaller configured stack is not evidence that a workload fits. [Performance and memory](performance.md) gives a measurement checklist and links the relevant fixtures.

## Report a problem

Include a small reproducer and the setup information listed above. Use [GitHub issues](https://github.com/yongkyuns/nxrs/issues) for bugs and the repository's [security policy](../../SECURITY.md) for vulnerability reports. Remove credentials and private device data from logs before sharing them.
