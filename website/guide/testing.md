# Testing and debugging

Use host tests for application and component behavior, target builds for integration, and device runs for hardware behavior and resource measurements.

## Run the host checks

```sh
cargo test --locked
python3 tools/check-architecture.py
```

The workspace's default members are host-compatible. For the standard-library application:

```sh
cargo test --locked -p nxrs-std-demo
cargo run --locked -p nxrs-std-demo
```

The small application template is tested independently:

```sh
cargo test --locked --manifest-path website/examples/basic-firmware/Cargo.toml --all-targets
```

The [test index](../../tests/README.md) lists additional suites and prerequisites. Use the owning package's tests when changing a driver, HAL, or service.

## Test component behavior

Keep processing logic and state transitions in code that can run without the physical device. Supply test inputs or implementations through the component's normal interface. Test valid operations, malformed input, partial initialization, queue saturation, disconnects, and shutdown.

For active components, check that stop requests wake blocked waits and that workers are joined before their resources are released. For I/O, include timeouts, short transfers, and recovery from an interrupted operation.

## Build and run firmware

Target builds check the selected compiler, Rust/NuttX integration, native headers, linking, and image configuration. Simulator and QEMU runners execute firmware and inspect the console output. Physical tests cover the board, peripheral behavior, and timing.

See [Building for NuttX](firmware.md) for a standard-library application build and ARM QEMU runner. The [simulator guide](../../docs/nuttx-sim.md) and [QEMU guide](../../docs/nuttx-qemu.md) cover the other execution setups.

## Record a useful failure report

Include the revision, application, profile, overlay order, host or board, toolchain versions, complete command, and relevant logs. Check the stage that failed: configuration, compilation, linking, startup, I/O, processing, or shutdown.

Reduce the failure to a small reproducible case. Preserve the original error while checking a change, and add a regression test when practical.

## Measure resources

Record stack and heap use, queue storage, image size, and execution time with the tested workload and configuration. [Performance and memory](performance.md) explains the measurements and comparison fixtures.

## Editor setup

The [editor guide](../../docs/editor-setup.md) covers Rust and clangd configuration. Use the generated headers and compiler settings from the selected target build when inspecting native bindings.
