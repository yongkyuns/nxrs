# Performance and memory

Existing NXRS evaluations cover event delivery, channel storage, thread stacks, code size and heap use. These measurements help track the cost of using Rust `std` and reusable components within an application's resource budget.

## Existing evaluations

The [event-path measurements](../../docs/std-channel-performance.md) cover channel operations and scheduling, including on-board results. The [Rust std footprint study](../../docs/rust-std-footprint.md) and [service-footprint fixture](../../tests/service-footprint/README.md) provide memory analysis and build recipes.

The [RTOS comparison](../../docs/rtos-comparison.md) compares reference workloads on NuttX, Zephyr and Embassy. Its NuttX fixture uses native RTOS adapters. The [MPSC comparison fixture](../../tests/mpsc-comparison/README.md) tests Rust std channels against Zephyr `k_msgq`.

## Define the workload and budget

Record event sources, rates, bursts, payload sizes, queue capacities, processing work and deadlines. Include startup, idle, overload and shutdown. Use isolated channel benchmarks to measure transport costs and application tests to measure the complete processing path.

Use the same measurement points in each comparison. Record producer admission time, wake-to-handler latency, handler CPU time and end-to-end delivery separately. A caller's elapsed time can include work done by a preempting consumer.

## Measure memory use

Record deployed code and read-only data, mutable static data, queue and payload storage, allocated thread stacks, heap high-water marks and retained buffers. Include alignment, allocator behavior and memory placement where they affect the result.

Measure deployed sections rather than the total ELF file size. Record stack allocation and measured stack use separately. Include buffers and other allocations outside the message queues.

## Measure event delivery

Test ready and blocked consumers separately. Record admitted and rejected transfers and verify payload order before calculating latency. Keep the source revision, compiler version, configuration and firmware image with the result.

Scheduler policy, effective priorities, locking and endpoint lifetimes affect delivery behavior. The [event communication reference](../../docs/concurrency-event-communication.md) describes the relevant transport and lifecycle requirements.

## Compare equivalent configurations

Use comparable workloads, hardware, CPU/cache settings, compiler options, memory placement and scheduling. Record whether hot code runs from flash or internal RAM.

Keep results from separate boots and configurations identifiable. Calculate component costs from matching measurements rather than subtracting unrelated percentiles. Report median, tail latency and observed maxima separately; a measured maximum is not a worst-case timing bound.

## Record the results

Include commands, raw data, configuration, workload, results and any failed runs. State whether timing uses calibrated time or raw counters, and whether execution was on a host, emulator or physical board.

Repeat the relevant evaluations when services, payloads, devices or toolchains change.
