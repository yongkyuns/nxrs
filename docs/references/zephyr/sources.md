# Zephyr sources and evidence

This index supports the [architecture study](README.md) and [diagram atlas](architecture-atlas.md). Sources are primary project documentation and implementation examples. The [shared review scope](../README.md) is architectural prior art for nxrs, not a Zephyr deployment proposal.

## Exact snapshots

| Source | Snapshot | Scope |
| --- | --- | --- |
| Zephyr example | [`fa4f8fb0e470210aee0ae6fb069a281bc6ac887a`](https://github.com/zephyrproject-rtos/zephyr/tree/fa4f8fb0e470210aee0ae6fb069a281bc6ac887a) | Accelerometer sample: public device/kernel APIs, devicetree alias, readiness and sensor calls. |
| Official Rust module | [`b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b`](https://github.com/zephyrproject-rtos/zephyr-lang-rust/tree/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b) | Documented no_std/optional alloc integration, not an audit of every possible Rust runtime. |
| Shared nxrs architecture baseline | [`5c0d6360ef5190346ddfd41aec766895800ba287`](https://github.com/yongkyuns/nxrs/tree/5c0d6360ef5190346ddfd41aec766895800ba287) | Capability/provider architecture, application ownership and proposed concurrency/qualification baseline used in all three studies. |
| Historical nxrs examples | [`bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943`](https://github.com/yongkyuns/nxrs/tree/bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943) and [`7a98e1862fee4a286c4d60869803ef51a67c75bb`](https://github.com/yongkyuns/nxrs/blob/7a98e1862fee4a286c4d60869803ef51a67c75bb/docs/concurrency-event-communication.md) | Provenance of the HAL examples and queue proposal; architectural comparisons use the shared baseline above. |

The shared-baseline concurrency document is present in the sampled main but still labels implementation qualification as pending. Language support, proposed event-producing HALs and a particular channel's target qualification must not be conflated. The independently reviewed Zephyr/Rust snapshots are not a tested release pair.

## Mechanism-to-source map

| Question | Primary evidence and what it establishes |
| --- | --- |
| Native APIs versus compatibility | [Device model](https://docs.zephyrproject.org/latest/kernel/drivers/index.html), [POSIX implementation](https://docs.zephyrproject.org/latest/services/portability/posix/implementation/index.html), pinned sample: native device/kernel operations and optional compatibility. |
| Language versus runtime | [C++ support](https://docs.zephyrproject.org/latest/develop/languages/cpp/index.html), pinned official Rust module: application-language availability is distinct from standard-library, allocator, entry and threading support. |
| Execution and scheduling | [Scheduling](https://docs.zephyrproject.org/latest/kernel/services/scheduling/index.html), [threads](https://docs.zephyrproject.org/latest/kernel/services/threads/index.html), [workqueues](https://docs.zephyrproject.org/latest/kernel/services/threads/workqueue.html): execution owners, priorities, ISR/deferred work and serial-handler consequences. |
| Memory and protection | [Userspace](https://docs.zephyrproject.org/latest/kernel/usermode/overview.html), [syscalls](https://docs.zephyrproject.org/latest/kernel/usermode/syscalls.html), [kernel objects](https://docs.zephyrproject.org/latest/kernel/usermode/kernelobjects.html), [memory domains](https://docs.zephyrproject.org/latest/kernel/usermode/memory_domain.html), [slabs](https://docs.zephyrproject.org/latest/kernel/memory_management/slabs.html): object authorization versus RAM permission; bounded pools are not whole-program allocation proofs. |
| Acquisition and timestamps | [Fetch/Get](https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/fetch_and_get.html), [Read/Decode](https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/read_and_decode.html): cached transactions versus encoded-buffer/completion lifetime and backend-specific timing/streaming support. |
| Communication and lifecycle | [Message queues](https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html), [FIFOs](https://docs.zephyrproject.org/latest/kernel/services/data_passing/fifos.html), [polling](https://docs.zephyrproject.org/latest/kernel/services/polling.html), [workqueue API](https://docs.zephyrproject.org/latest/doxygen/html/group__workqueue__apis.html): copies versus pointers, intrusive lifetime, readiness races, coalescing and synchronous cancellation constraints. |
| Configuration versus construction | [Devicetree/Kconfig](https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html), [device access](https://docs.zephyrproject.org/latest/build/dts/howtos.html), [application build](https://docs.zephyrproject.org/latest/develop/application/index.html), [system threads](https://docs.zephyrproject.org/latest/kernel/services/threads/system_threads.html): selection, linking, initialization and use are different states. |
| Observability and test environment | [Tracing](https://docs.zephyrproject.org/latest/services/tracing/index.html), [thread analyzer](https://docs.zephyrproject.org/latest/services/debugging/thread-analyzer.html), [native_sim](https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html), [peripheral emulation](https://docs.zephyrproject.org/latest/hardware/emulator/bus_emulators.html): configured observability and kernel/driver testing, not MCU timing proof or OS-independent application execution. |
| Lessons for nxrs | Shared-baseline [HAL architecture](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/hal-platform-architecture.md) and [concurrency baseline](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md). Borrow contract/lifetime/configuration discipline while keeping the existing NuttX direction. |

## Evidence scope and qualification

The sample/Rust examples were source-reviewed on 2026-09-30; userspace and detailed ownership mechanisms on 2026-10-04; scheduling, lifecycle, languages and observability documentation on 2026-10-05. `latest` links are rolling manuals, not immutable release pins. Pinned examples support their stated claims only.

No Zephyr target port, physical-device run, firmware timing benchmark or memory-safety proof is claimed. [Rendering and document checks](diagrams/README.md) validate reference assets, not kernel/backend conformance. Recommendations are separated from observed behavior; no proposed Zephyr provider, execution adapter or cross-OS milestone is an output of this study.
