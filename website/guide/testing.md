# Testing and debugging

NXRS uses host tests, target build checks, simulator and QEMU runs, and on-board tests. Start with the tests for the component you changed, then run the relevant platform and application tests.

## Start with the host checks

```sh
cargo test --locked
python3 tools/check-architecture.py
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

The default workspace tests cover host-compatible members. Target-specific suites have separate commands in the [test index](../../tests/README.md).

For HAL or native-interface changes, follow the focused test requirements in [AGENTS.md](../../AGENTS.md). The architecture checker verifies module and dependency rules; the corresponding runtime tests check behavior.

## Test lifecycle and failure handling

Test full queues, slow consumers, malformed input, source faults, partial initialization, cancellation during a blocking wait and explicit shutdown. Check returned values, counters and ownership. Verify that callbacks, workers and DMA operations have stopped before releasing their resources.

Navigation service tests are under `service/navigation/tests/`. The [demo reference](../../docs/event-driven-demo.md) describes control and teardown behavior. Processing pause and physical acquisition are separate operations and should be tested accordingly.

## Check target integration

Native ABI checks compare the configured target headers with the Rust artifacts used in the build. Record the source revision, configuration and firmware image with the result.

After linking, run the firmware with the corresponding simulator, emulator or device test runner. Review the full transcript for faults, startup, expected output and cleanup.

## Tests and evaluations

| Test or evaluation | Coverage |
| --- | --- |
| Host unit and behavior tests | Algorithms, component ownership, service lifecycle and injected failures |
| Target build and ABI checks | Compilation, linking and native interface compatibility |
| NuttX simulator and QEMU runs | Firmware startup, application execution and shutdown |
| On-board tests | Drivers, peripherals, interrupts, recovery and application behavior |
| Performance and memory evaluations | Event latency, throughput, code size, stacks, queues and heap use |

The [test index](../../tests/README.md), [NuttX simulator guide](../../docs/nuttx-sim.md) and [QEMU guide](../../docs/nuttx-qemu.md) contain setup and execution instructions. The [performance guide](performance.md) links to measurements and comparisons.

## Diagnose a failure

Record the repository revision, application, profile, overlay order, compiler/SDK version, complete command and full log. Identify the failing stage: configuration, dependency selection, native compilation, linking, startup, acquisition, processing or shutdown.

Reproduce the failure with a small test case using the same configuration. Keep the original log and compare against a passing run. Fix the underlying behavior rather than suppressing errors, increasing every timeout or silently selecting a mock provider.

## Editor setup

Use the [editor configuration guide](../../docs/editor-setup.md) for Rust and clangd setup. Generated target headers and native compile flags must match the selected build.
