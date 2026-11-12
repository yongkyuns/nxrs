# Testing and debugging

Use host tests for application and component behavior, target builds for integration, and device runs for hardware behavior and resource measurements.

Choose the smallest test that answers the question you are working on. A parsing change should be reproducible on the host. A change to native device control needs the matching target integration and device test as well.

## Choose a test level

| Question | Test level | What a pass establishes |
| --- | --- | --- |
| Does this algorithm, parser, or state machine behave correctly? | Portable unit tests | Behavior for the tested inputs |
| Does an owner handle overload, cancellation, and restart? | Host component/lifecycle tests | The tested ownership and failure paths |
| Are target features, native headers, and symbols compatible? | A selected NuttX build and ABI checks | The selected image compiles, links, and passes its build checks |
| Does the firmware execute through the target runtime? | NuttX simulator or QEMU | Functional behavior in that execution environment |
| Does the device work with the actual wiring and timing? | Physical board tests | The measured configuration and workload on that board |

Keep these results distinct. Host or emulator success cannot establish physical I/O operation, interrupt timing, or stack headroom on another target.

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

For example, these software capabilities have explicit feature-selected host tests:

```sh
cargo test --locked -p nxrs-camera --features replay
cargo test --locked -p nxrs-storage --features recording
cargo test --locked -p nxrs-transport --features udp
```

They exercise replay, file recording, and UDP behavior through their owning packages. Their [camera](../../hal/camera/README.md), [storage](../../hal/storage/README.md), and [transport](../../hal/transport/README.md) references describe the assertions and limits. A feature enabling software I/O is separate from a profile admitting hardware acquisition.

When changing architectural boundaries, run the audit and its regression suite:

```sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p 'test_architecture.py'
```

Avoid replacing the default host command with `cargo test --workspace` without reading the target fixtures' requirements. The workspace also contains firmware packages with native SDK or selected-profile prerequisites.

## Test component behavior

Keep processing logic and state transitions in code that can run without the physical device. Supply test inputs or implementations through the component's normal interface. Test valid operations, malformed input, partial initialization, queue saturation, disconnects, and shutdown.

For active components, check that stop requests wake blocked waits and that workers are joined before their resources are released. For I/O, include timeouts, short transfers, and recovery from an interrupted operation.

| Path | Useful assertion |
| --- | --- |
| Normal work | The output corresponds to the admitted input and preserves the required ordering |
| Full inbox | The documented rejection or backpressure policy occurs; rejected ownership is accounted for |
| Producer disconnect | The consumer wakes, drains or cancels according to its contract, and exits |
| Partial startup | Previously created workers and resources are retired after a later step fails |
| Stop during a blocked wait | Cancellation reaches the waiter without requiring another data event |
| Stop with pending work | Accepted work is completed or explicitly cancelled; no callback uses freed state |
| Restart | The next acquisition has no stale callbacks, handles, or terminal state from the previous lifetime |

Use synchronization or controlled injected failures to reach these states. A sleep that happens to make the test pass is weaker evidence than an explicit signal that the worker reached the intended boundary.

## Build and run firmware

Target builds check the selected compiler, Rust/NuttX integration, native headers, linking, and image configuration. Simulator and QEMU runners execute firmware and inspect the console output. Physical tests cover the board, peripheral behavior, and timing.

See [Building for NuttX](firmware.md) for the complete ESP32-S3 QEMU tutorial. The [simulator guide](../../docs/nuttx-sim.md) and [QEMU guide](../../docs/nuttx-qemu.md) cover the other execution setups, including the separate ARM target.

The first-firmware runner checks source creation, a host run, a target boot, a greeting edit, and the worker extension. Each step names the image and checks the application output and exit status. For your own application, begin with an equally small witness before adding the full device workload.

Follow the owning fixture's boot and restart procedure. A tutorial command designed to run repeatedly in one NSH boot does not establish that every Rust workload, target, or qualification image supports the same restart pattern.

## Record a useful failure report

Include the revision, application, profile, overlay order, host or board, toolchain versions, complete command, and relevant logs. Check the stage that failed: configuration, compilation, linking, startup, I/O, processing, or shutdown.

Reduce the failure to a small reproducible case. Preserve the original error while checking a change, and add a regression test when practical.

| Failure stage | Useful evidence |
| --- | --- |
| Profile resolution | Exact profile, overlay order, source files, and inspection output |
| Cargo or native build | First meaningful error, selected toolchain, and full build log |
| Image startup | Image identity, complete console output from reset, and boot procedure |
| Peripheral acquisition | Resolved endpoint, BSP registration, wiring, and acquisition error |
| Event processing | Offered/admitted/rejected work, sequence information, and owner state |
| Shutdown | Last completed transition, outstanding waits/callbacks, and join result |

The [build output guide](build-system.md#read-the-build-output) identifies the configuration and provenance files to retain. Treat a successful earlier build as a different result if the current command failed.

## Measure resources

Record stack and heap use, queue storage, image size, and execution time with the tested workload and configuration. [Performance and memory](performance.md) explains the measurements and comparison fixtures.

## Editor setup

The [editor guide](../../docs/editor-setup.md) covers Rust and clangd configuration. Use the generated headers and compiler settings from the selected target build when inspecting native bindings.

For Zed, `cargo nxrs editor host` selects the current host profile, and `cargo nxrs editor status` reports the active choice. For an in-workspace NuttX application, build first and use `cargo nxrs editor --build-dir <output-directory>` with that completed build. The command updates this checkout's Zed rust-analyzer settings; the current prepared-target editor path requires an application inside the NXRS workspace.

Keep the editor target consistent with the build you are diagnosing. A dimmed module or missing generated header can indicate the wrong editor profile rather than a missing firmware implementation.
