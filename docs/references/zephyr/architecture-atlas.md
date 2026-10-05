# Zephyr architecture atlas

**Diagram/architecture review: 2026-10-04. Non-normative reference.** This atlas expands the [original Zephyr/nxrs analysis](README.md) into six views. It is not a migration proposal, a driver conformance test, or a benchmark.

## Design brief: questions before drawing

| View | Question and intended picture |
| --- | --- |
| **Z1 — architecture** | Separate application APIs, kernel/device contracts, and driver/hardware implementation. Show native and optional POSIX routes without inventing process boundaries. |
| **Z2 — protection** | Draw an unprivileged caller, syscall verification, and trusted implementation. Distinguish RAM access from kernel-object authorization. |
| **Z3 — sensor ownership** | Contrast Fetch/Get's driver-private cache with Read/Decode's explicit encoded-buffer lifetime. |
| **Z4 — build versus runtime** | Trace devicetree and Kconfig into a linked image, initialization, readiness checks and use. A device pointer is not ownership. |
| **Z5 — event contracts** | Compare copied message records, intrusive item transfer and scheduled work. Show what storage/lifetime and event-counting promises differ. |
| **Z6 — nxrs portability** | Separate a possible Zephyr provider from the additionally required Rust execution/entry environment and behavioral qualification. |

Region titles identify whether a boundary is logical, execution-related, memory-related or a syscall access boundary. Blue solid arrows are calls; teal solid arrows carry data/ownership; orange dashed arrows schedule/notify; grey dotted arrows are configuration/build relationships. Red gates indicate access/protection checks. **The colors do not imply that every component has a separate thread or address space.** Arrow labels and each view's scope are essential parts of the model.

## Z1. Native contracts, optional compatibility, one runtime

[![Zephyr native architecture and device dispatch](diagrams/zephyr-abstractions.svg)](diagrams/zephyr-abstractions.svg)

[Editable D2](diagrams/zephyr-abstractions.d2)

Native applications use Zephyr kernel and device APIs. POSIX is an optional compatibility library over enabled services, not the foundation of all peripheral access. Device-class calls dispatch through the API associated with an instance; they do not require a universal `open/read/ioctl` device layer. The instance separates constant configuration and operations from mutable runtime data. A device object or queue is not itself a worker thread. [POSIX implementation][posix] · [Device model][device]

This view focuses on thread/object and sensor paths, not a complete inventory of filesystem, networking and power-management subsystems. Its application call sites can share a thread. The columns represent dependencies in the ordinary single-image model. They are not user/kernel/process partitions. Even with userspace enabled, an API call is not automatically a new thread, an IPC message or a context switch: generated wrappers choose the appropriate dispatch path according to execution mode. That distinction is expanded in Z2. [System calls][syscalls]

## Z2. Object authorization and memory access are independent

[![Zephyr userspace and access verification](diagrams/memory-protection.svg)](diagrams/memory-protection.svg)

[Editable D2](diagrams/memory-protection.d2)

With `CONFIG_USERSPACE` and a supported architecture, user threads enter system APIs through generated trap/unmarshal/verification machinery; supervisor callers can invoke the implementation directly. A verifier must validate the relevant object and permissions, and validate user-memory arguments before trusted code uses them. `k_object_access_grant()` authorizes operations on an object; memory-domain partitions govern which RAM is accessible. These are **different permissions**. A valid object pointer can be passed as an identifier without permission to dereference its underlying kernel storage. [System calls][syscalls] · [Kernel objects][objects]

Zephyr memory domains select accessible regions within the system's address map; this is not a claim of Linux-style per-process virtual address spaces. Shared partitions are explicit. Stack accessibility details depend on architecture/configuration, and supervisor code is trusted rather than confined by user-domain rules. The map deliberately does not promise that every other user stack is inaccessible on every port. [Memory protection design][domains] · [Userspace overview][user]

## Z3. Sensor interfaces differ in who owns the data

[![Fetch/Get versus Read/Decode](diagrams/sensor-ownership.svg)](diagrams/sensor-ownership.svg)

[Editable D2](diagrams/sensor-ownership.d2)

Fetch/Get is the established blocking acquisition/cache model. `sensor_sample_fetch()` updates driver-private sample state; `sensor_channel_get()` obtains channels from that cached acquisition. Multiple clients must synchronize the **whole fetch/get transaction**, not just individual calls, or another acquisition can replace the intended sample. This is neither a per-client event queue nor a universal measurement timestamp. [Fetch/Get contract][fetch]

Read/Decode exposes a buffer-oriented model with RTIO integration: acquisition produces encoded data that can be decoded without another device access. Storage and request state must remain valid until completion, and the completed buffer cannot be recycled while consumers still need it. Actual asynchronous operation, streaming and bus behavior depend on the backend. Trigger callbacks are a separate execution concern: their supported thread/workqueue context is not user mode, and a trigger is not a retained sample queue. [Read/Decode contract][decode] · [Fetch/Get and triggers][fetch]

## Z4. Hardware selection is not runtime construction or ownership

[![Zephyr build and initialization boundaries](diagrams/zephyr-build-selection.svg)](diagrams/zephyr-build-selection.svg)

[Editable D2](diagrams/zephyr-build-selection.d2)

Devicetree plus overlays/bindings describes hardware instances and properties; Kconfig selects software and its dependencies. Compilation/linking combines metadata, selected code, device objects and initialization entries into the configured image. Neither source description alone proves that a functioning driver exists in the binary. [Devicetree versus Kconfig][dt-kconfig] · [Device model][device]

The right-hand runtime lane is deliberately separate. `DEVICE_DT_GET` obtains a pointer; `device_is_ready()` checks successful initialization. Neither grants exclusive use or solves synchronization, power state, missing operations or lifetime policy. The diagram chooses the common statically configured MCU path; dynamic buses and deferred initialization need their specific rules. [Device acquisition][dt-get]

## Z5. Copy, transfer and scheduling are different contracts

[![Zephyr queues and work-item lifetime](diagrams/queue-contracts.svg)](diagrams/queue-contracts.svg)

[Editable D2](diagrams/queue-contracts.d2)

`k_msgq` transfers fixed-size byte records into bounded storage or directly to a waiting receiver. The sender's original bytes and the receiver's copy are separate storage, but a pointer inside the record is still a pointer—not a deep copy of its referent. Full-queue waiting/error policy and ISR no-wait constraints must be respected. This C byte-copy behavior is not automatically safe for arbitrary owning Rust values. [Message queues][msgq]

Ordinary intrusive `k_fifo_put` links caller-owned items rather than copying payloads. The first word is reserved for the FIFO linkage, and an item must stay valid while queued and must not be simultaneously enqueued twice. The FIFO itself has no fixed element-capacity bound; a bounded allocation pool can provide one. The separately available allocating variants are outside this illustrated path. [FIFO contract][fifo]

`k_work` schedules a handler on a workqueue thread. A work item already queued is not duplicated for every submit, so it is not a lossless sample/event counter. Work and associated state must remain alive until their execution is quiescent; a blocking handler holds up subsequent work on that same queue. `k_poll()` reports readiness of supported kernel objects, not ownership or arbitrary fd/Rust-channel readiness. Consumers must acquire/dequeue, handle races and reset poll state as required. [Workqueue semantics][work] · [Polling][poll]

## Z6. A provider is not an execution port

[![Nxrs provider versus execution portability](diagrams/nxrs-direction.svg)](diagrams/nxrs-direction.svg)

[Editable D2](diagrams/nxrs-direction.d2)

This final view is **design direction**, not implemented Zephyr support. An optional provider can hide Zephyr device pointers, acquisition APIs and normalization below the Rust capability contract. The product-platform/features choice is a build decision. Independent event capacities and one logical blocking selection point refer to the separately pinned nxrs concurrency proposal; they are not a claim that the current kernel already supplies the chosen Rust channel semantics. [Existing nxrs analysis and proposal sources](README.md)

Entry, threads, waits, clocks and allocation remain a separate obligation. The previously reviewed official Rust module snapshot documents `no_std` plus optional `alloc`, not a Rust `std` port. `native_sim` still executes the Zephyr kernel, unlike OS-independent native service tests. Neither one driver adapter nor a compile proves native, MCU and browser equivalence. Keep the original NuttX direction unless a concrete deployment justifies another qualified backend. [Pinned Rust allocator][rust] · [Native simulator][native]

## Evidence and limits

Primary Zephyr documentation linked below was reviewed for this atlas on 2026-10-04; URLs under `latest` are moving documentation, not immutable release pins. The [existing evidence index](sources.md) retains its exact sample, Rust-module and nxrs proposal commits. The atlas does not imply that those independently inspected snapshots are a tested release pair. No runtime benchmark, firmware build, memory-safety proof or target conformance test is claimed.

[Full-size viewing, geometry checks and reproduction](diagrams/README.md). All PX4 sources, SVGs and routing tools remain untouched.

[posix]: https://docs.zephyrproject.org/latest/services/portability/posix/implementation/index.html
[device]: https://docs.zephyrproject.org/latest/kernel/drivers/index.html
[syscalls]: https://docs.zephyrproject.org/latest/kernel/usermode/syscalls.html
[objects]: https://docs.zephyrproject.org/latest/kernel/usermode/kernelobjects.html
[domains]: https://docs.zephyrproject.org/latest/kernel/usermode/memory_domain.html
[user]: https://docs.zephyrproject.org/latest/kernel/usermode/overview.html
[fetch]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/fetch_and_get.html
[decode]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/read_and_decode.html
[dt-kconfig]: https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html
[dt-get]: https://docs.zephyrproject.org/latest/build/dts/howtos.html
[msgq]: https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html
[fifo]: https://docs.zephyrproject.org/latest/kernel/services/data_passing/fifos.html
[work]: https://docs.zephyrproject.org/latest/kernel/services/threads/workqueue.html
[poll]: https://docs.zephyrproject.org/latest/kernel/services/polling.html
[rust]: https://github.com/zephyrproject-rtos/zephyr-lang-rust/blob/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b/zephyr/src/alloc_impl.rs
[native]: https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html
