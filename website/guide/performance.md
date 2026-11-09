# Performance and memory

Rust `std`, threads and reusable components are useful only within a product’s resource budget. NXRS treats their cost as an ongoing measurement question, not an assumption of zero overhead or a universal disadvantage.

## Define a workload and budget

Specify the event sources, rates, bursts, payload sizes, queue capacities, processing work and deadlines. Include startup, idle, overload and shutdown behavior. A synthetic queue transfer can answer a narrow question; it does not replace an end-to-end application workload.

Use the same accounting boundaries in every comparison. Separate producer admission duration, wake-to-handler latency, handler CPU time and end-to-end delivery. A caller’s elapsed time can include a preempting consumer’s work.

## Measure the whole memory budget

Record deployed code and read-only data, mutable static data, queue and payload storage, allocated thread stacks, heap high-water marks and retained buffers. Include target configuration, alignment, allocator behavior and memory placement where they affect the result.

A smaller ELF file is not automatically less deployed flash. A configured stack size is not a measured high-water mark. A bounded inbox is not a bound on every allocation elsewhere in the application.

The [Rust std footprint study](../../docs/rust-std-footprint.md) and [service-footprint fixture](../../tests/service-footprint/README.md) provide existing analysis and build recipes.

## Measure event delivery under an explicit schedule

Test ready and blocked consumers separately. Record all admitted/rejected transfers and verify payload/order correctness before interpreting latency. Preserve source, compiler, configuration, image and measurement identities.

A queue API that does not wait for capacity can still have internal progress restrictions. Scheduler policy, effective priorities, reservation ordering, lock dependencies and endpoint lifetime all matter. Read the [event communication contract](../../docs/concurrency-event-communication.md) and [std-channel evidence](../../docs/std-channel-performance.md) before adopting a transport based on its name.

## Compare with references fairly

Zephyr is a useful reference, but compare equivalent workloads with explicit hardware, CPU/cache configuration, compiler settings, memory placement, scheduling and feature selection. Code placement in flash versus internal RAM can materially change what a result means.

The [NuttX/Zephyr/Embassy comparison](../../docs/rtos-comparison.md) uses a NuttX Rust fixture with native RTOS adapters; it is not a measurement of the complete NXRS std programming model. The [MPSC comparison fixture](../../tests/mpsc-comparison/README.md) covers a different named boundary using std channels and Zephyr `k_msgq`.

Do not pool unrelated boot distributions or subtract percentiles to infer a component’s cost. A faster median does not establish a worst-case bound, and a changed instrumentation layout can perturb the workload.

## Keep conclusions scoped

Keep raw counter observations distinct from independently calibrated time. Record failures alongside successes. Treat absolute timing, scheduling progress and physical execution as separate acceptance questions.

A useful report says which decision the measurement supports, which assumptions it needs, and what remains unknown. Continue monitoring the chosen architecture as services, payloads, devices and target toolchains change; do not turn one snapshot into a permanent ranking of operating systems.
