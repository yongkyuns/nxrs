# Troubleshooting

Begin with the exact command, app, profile, revision and complete error output. Fix the boundary that failed instead of adding a fallback that changes what the application is testing.

## `cargo nxrs` is not recognized

Run it from the repository root and confirm that the checkout includes `.cargo/config.toml`. It is a repository alias backed by the build controller, not necessarily a globally installed Cargo subcommand. Use the [build reference](../../docs/build-system.md) for detached consumers.

## The HAL is unavailable

Check the selected profile. A no-profile build intentionally exposes contracts without silently enabling mocks. Use `host-demo` for the first synthetic navigation demo. A profile can select only implemented/admitted bindings; a platform name does not imply every sensor or radio exists.

Do not add concrete device dependencies to app/service source to bypass profile selection. Review [profiles and overlays](profiles.md) and the selected HAL’s ownership contract.

## Rust target or linker errors appear

Separate the host toolchain from the target toolchain. The checkout’s host Rust pin is not an Xtensa SDK. Install the selected target’s prerequisites, check the frontend’s discovered compiler/SDK, and retain the complete build output.

An unsupported std API or target ABI mismatch is not solved by replacing the pinned target with an unrelated newer compiler. Use the [Rust std integration reference](../../docs/nuttx-std.md) and qualify a deliberate toolchain change separately.

## The lockfile needs updating

`--locked` deliberately rejects a dependency resolution change. Confirm that you are building a consistent checkout. When intentionally adding or renaming a package, regenerate the lockfile through the normal Cargo workflow, review the change and restore locked validation. Do not quietly allow lockfile drift in CI.

## QEMU or a generated image cannot be found

After installing the tools, source `target/qemu-tools/environment.sh` in the shell that runs the emulator. Use the output path for the actual app/profile you built, not an older fixture’s directory. Confirm that the build completed before running its output.

The [firmware guide](firmware.md) contains the matching build and execution commands for the first ESP32-S3 demo.

## A physical sensor produces no data

Verify the board wiring, supported BSP registration, effective NuttX configuration, resolved resource/alias bindings and detected chip identity. Then examine readiness and acquisition errors before changing service behavior.

Service processing pause does not stop acquisition. An unwired timer path and a configured interrupt path are distinct configurations. Do not silently turn a broken IRQ setup into polling.

## Shutdown hangs or resources remain busy

Check every producer and cloned endpoint, blocked wait, callback registration and joined worker. Review whether stop drains admitted work or cancels it. A dropped handle may request cancellation without waiting for cleanup.

Keep resource quarantine and terminal cleanup failures visible. Resetting counters or discarding the error does not establish safe resource release. See [service lifecycle](services.md).

## Performance differs from a published result

Compare exact image/source identity, workload, schedule, compiler, cache/clock settings and memory placement. Confirm that you are measuring the same boundary. Re-running a historical script on a different image does not reproduce its original qualification.

## Report an actionable problem

Include the failing command, revision, profile/overlays, toolchain identities, host/board model, full relevant logs and the smallest reproducer. Exclude credentials, private device backups and secrets. Use [GitHub issues](https://github.com/yongkyuns/nxrs/issues) for ordinary bugs and [SECURITY.md](../../SECURITY.md) for vulnerability reporting.
