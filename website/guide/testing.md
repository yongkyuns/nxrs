# Testing and debugging

Use the smallest test that can establish the behavior you changed, then move across the relevant target boundary. Keep functional correctness, native ABI, emulated execution and physical measurements separate.

## Start with the host checks

```sh
cargo test --locked
python3 tools/check-architecture.py
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

The root workspace has host-compatible default members. Target-only and destructive probes are separately selected; do not infer that the default test command exercised every board or fixture.

For HAL or native-boundary changes, run the corresponding focused suites described in [AGENTS.md](../../AGENTS.md) and the [test index](../../tests/README.md). The architecture checker is useful, but a source-boundary pass is not a device behavior test.

## Test lifecycle, not just the happy path

Exercise full queues, slow consumers, malformed input, source faults, partial initialization, cancellation during a blocking wait and explicit shutdown. Check returned values, counters and ownership. Verify that no callback, worker or DMA operation can still access a released resource.

The navigation service tests are under `service/navigation/tests/`; the [demo reference](../../docs/event-driven-demo.md) explains the intended control and teardown behavior. Test pause/resume semantics without pretending that processing pause stops physical acquisition.

## Cross the native boundary explicitly

A native ABI gate checks the actual configured target headers against the Rust artifacts used in that build. Host layout assumptions do not prove a NuttX ABI. Preserve the source pins, configuration and image identity with the result.

After linking, use the target runner to check the complete transcript, not just the appearance of an expected success word. Unexpected faults, missing cleanup or a runtime identity mismatch remain failures.

## Keep an evidence ladder

| Evidence | What it can establish | What it does not establish by itself |
| --- | --- | --- |
| Host unit/behavior tests | Portable algorithms, ownership and injected failure handling | Device wiring or target ABI |
| Target build and ABI inspection | Compilation, linking and the checked native layout/interface facts | Physical execution |
| Simulator/QEMU run | Behavior under that emulated environment | Physical interrupt latency or throughput |
| Controlled device run | Observed behavior on the recorded image and hardware | Universal timing guarantees or every operating condition |

The [test guide](../../tests/README.md), [NuttX simulator guide](../../docs/nuttx-sim.md) and [QEMU guide](../../docs/nuttx-qemu.md) identify their different scopes.

## Debug from the failed boundary

Record the repository revision, app/profile, overlay order, compiler/SDK identity, complete command, build receipt and full log. First establish which stage failed: configuration, dependency selection, native compilation, linking/ABI, startup, acquisition, processing or shutdown.

Do not “fix” a failed runtime by suppressing the warning, increasing every timeout or silently selecting a mock. Reduce to the smallest fixture that retains the failing boundary, preserve the failed evidence and compare against a named control.

## Editor setup

Use the [editor configuration guide](../../docs/editor-setup.md) for the current Rust and clangd setup. Generated target headers and native compile flags must match the selected build; editor diagnostics against an unrelated host configuration can be misleading.
