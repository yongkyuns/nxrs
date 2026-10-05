# Zephyr architectural analysis

Zephyr illustrates an integrated RTOS with explicit kernel-object, device, configuration and access-control contracts. Its value to nxrs is **the discipline of separating selection, ownership, readiness, data lifetime and execution**. This reference borrows those ideas; it does not propose a Zephyr backend or a port of nxrs.

[Comparative framework](../README.md) · [Architecture atlas](architecture-atlas.md) · [Sources](sources.md) · [Diagram reproduction](diagrams/README.md)

## 1. Role and architectural position

Zephyr combines a kernel, device drivers, configurable subsystems and build integration. Native kernel/device APIs are the ordinary programming surface; POSIX is optional compatibility over enabled facilities, not the architecture underneath every peripheral API. This is an OS/device foundation, unlike PX4's flight-domain state owners and topic conventions. Zephyr does not prescribe nxrs's product-level decomposition, GNSS/EKF policy or service graph. [POSIX design][posix-design] · [Device model][device-model]

The examined mechanisms answer concrete architectural questions: device dispatch and configuration; threads/workqueues; memory/object access; copied versus linked data; acquisition buffers; and host/driver testing. A subsystem not examined here is not therefore absent from Zephyr. In particular, the queue comparison is not an exhaustive inventory of every available IPC or optional messaging subsystem.

## 2. Languages and runtime model

The inspected kernel/device surface is C: `struct device`, operation tables, kernel objects, buffers and callback functions. Object addresses identify many kernel resources; they are not automatic ownership tokens. Native clients can use public Zephyr interfaces without going through a uniform descriptor API. Generated syscall wrappers implement mode-dependent dispatch, not a universal serialization boundary. [Device model][device-model] · [System calls][syscalls]

The inspected official Rust module documents **`no_std` with optional `alloc`**, including `CONFIG_RUST_ALLOC`, not Rust std support in that integration. This is a pinned observation, not a statement that a separate/custom std port is impossible or that all targets have identical support. It demonstrates why source language, language runtime, kernel ABI and application architecture are separate dimensions. [Pinned module guide][rust-guide] · [Pinned allocator source][rust-alloc] · [Language integration][rust-docs]

**Ownership implication:** byte-copy C queues do not automatically implement Rust move/drop semantics. Copying the representation of an owning value can duplicate pointers rather than transfer the resource safely. Conversely, explicit C lifetime protocols can be sound when obeyed. Nxrs should use language-level ownership where applicable while testing FFI, cancellation, callback and buffer obligations at its existing NuttX boundary. [Message queues][msgq] · [Nxrs baseline][nxrs-events]

Zephyr also supports configured C++ applications, with toolchain/library and initialization restrictions; its C++ guide separates application use from kernel, driver and system-initialization code. A language front end and its library/runtime support are separate compatibility questions. This matters when comparing C APIs, PX4's C++ composition and nxrs's Rust ownership model. [C++ support][cpp]

## 3. APIs, contracts and portability boundaries

[![Zephyr native APIs and device dispatch](diagrams/zephyr-abstractions.svg)](diagrams/zephyr-abstractions.svg)

*Z1: dependency and call paths, not implicit process boundaries. [D2](diagrams/zephyr-abstractions.d2) · [Atlas](architecture-atlas.md#z1-native-contracts-optional-compatibility-one-runtime).*

| Header or symbol | Contract or dependency |
| --- | --- |
| `<zephyr/kernel.h>` and `k_*` | Public Zephyr-specific OS services, not automatically private kernel internals. |
| `<zephyr/device.h>` and `<zephyr/drivers/sensor.h>` | Hardware-independent device-class operations within the Zephyr model. |
| `<zephyr/devicetree.h>` and `DT_*` | Build-time hardware description, not a product-facing OS-neutral API. |
| Generated `zephyr/syscalls/...` headers | Public dispatch/verification support; not proof of a trap on every call. |

The accelerometer example uses kernel, device and sensor interfaces together. For `__syscall` APIs, generated wrappers select direct or user-mode dispatch as appropriate; permission validation is separate from the device-class abstraction. Public OS-specific headers are not by themselves broken layering. [Pinned sample][accel-sample] · [System calls][syscalls]

The device instance separates constant configuration/API operations from mutable runtime data. Drivers hide chip details behind class operations; bus APIs hide controller details. Neither the common call signature nor an optional POSIX library supplies uniform timestamp, FIFO, trigger, lifetime or error behavior for every device. [Device model][device-model] · [Sensor contracts][fetch-get]

**Portability boundary:** client reuse across supported hardware differs from independence from Zephyr APIs. The lesson for nxrs is to make its own product-facing contract intentional and contain implementation types. POSIX compatibility likewise requires checking enabled functions and semantics rather than assuming a complete application/runtime will work unchanged. [POSIX scope][posix-overview]

## 4. Execution, scheduling and ISR boundaries

A Zephyr thread owns an execution stack and kernel scheduling state. The scheduler distinguishes cooperative and preemptible threads; readying a thread is not equivalent to an immediate execution guarantee. Interrupts are separate contexts, and allowed no-wait operations must be checked per API. On multicore configurations, other contexts may run concurrently: same-thread serialization is not global mutual exclusion. [Threads][threads] · [Scheduling][scheduling]

A workqueue uses a thread to execute queued handlers serially. The system workqueue and additional application/subsystem queues are placement choices. Repeated submission of an already queued item does not retain one execution per event, although a running item may have follow-up work queued. A long or blocking handler delays later work on that queue. Zephyr permits blocking APIs in appropriate workqueue thread contexts, but that permission does not make the interference acceptable. [Workqueues][work]

For a representative sensor path, hardware indicates readiness; a driver-specific trigger/deferred context notifies or performs acquisition; a client fetches cached channels or receives a completed encoded buffer; application state processing runs in its chosen owner. The trigger callback's context depends on the driver/configuration and is not itself a retained sample queue. No mandatory broker or one-thread-per-sensor model follows from the API. [Fetch/Get and triggers][fetch-get] · [Read/Decode][read-decode]

This differs from PX4's product policy of principal IMU triggers and supporting GNSS inputs. Zephyr supplies mechanisms; the application decides which inputs wake processing, which values are retained and what deadlines matter. A task/queue count alone does not describe that policy.

## 5. Memory, ownership and protection

[Z2 shows userspace and access verification](architecture-atlas.md#z2-object-authorization-and-memory-access-are-independent). In the ordinary single-image model, a device/component/queue is not a separate process. With `CONFIG_USERSPACE` and suitable architecture support, user calls traverse generated validation and privilege gates, while supervisor callers can use direct implementation paths. Verifiers check relevant object type/authorization and user-memory arguments. [System calls][syscalls] · [Userspace][user]

`k_object_access_grant()` permits operations on a kernel object; a memory domain controls accessible RAM regions. These are distinct permissions: a usable object identifier is not permission to dereference all its storage. Domains are not a claim of Linux-style per-process virtual address spaces. Shared partitions and architecture/configuration-dependent stack access need explicit treatment; supervisor code remains trusted. [Kernel objects][objects] · [Memory domains][domains]

Storage contracts are equally important without userspace. `k_msgq` copies fixed-size records; ordinary intrusive `k_fifo` links caller-supplied items; Fetch/Get uses driver-private sample state; Read/Decode exposes encoded-buffer lifetime. Heap allocation is not required for every object: memory slabs provide fixed-size blocks with explicit finite availability. A bounded pool limits those blocks, not all program allocations, fragmentation or stack use. [Queues][msgq] · [FIFO][fifo] · [Memory slabs][slabs] · [Sensor ownership view](architecture-atlas.md#z3-sensor-interfaces-differ-in-who-owns-the-data)

Rust ownership, buffer bounds, allocator budgets, DMA/coherence and hardware privilege therefore remain separate review questions. A service boundary or a successful borrow check is not an MPU boundary or a timing proof.

## 6. Data flow and communication contracts

[Z5 compares the actual storage and scheduling contracts](architecture-atlas.md#z5-copy-transfer-and-scheduling-are-different-contracts).

| Mechanism | Storage / transfer contract | Overload and completion implications |
| --- | --- | --- |
| `k_msgq` | Fixed-size byte records in bounded storage, or direct copy to a waiting receiver | Full-queue wait/error policy; a copied pointer does not copy its referent; receiving is not application acknowledgment. |
| Ordinary `k_fifo_put` | Intrusive caller-owned item; linkage uses its first word | Item must remain alive and not be simultaneously enqueued twice; capacity comes from supplied items/pool, not a fixed FIFO element limit. |
| `k_work` | Schedulable handler state, not a measurement history | Pending submissions can coalesce; work/state must live through execution and cancellation. |
| `k_poll` | Readiness of supported kernel objects | Not acquisition/ownership; dequeue/take, race handling and event-state reset remain necessary. |

These mechanisms are documented in [message queues][msgq], [FIFO][fifo], [workqueues][work] and [polling][poll]. Allocating FIFO variants have different costs and are outside the ordinary intrusive path above. `k_poll` is not a general wait on arbitrary POSIX descriptors or Rust channel receivers.

**Architectural inference:** sending records through one queue can order those accepted records, but does not create a transactional snapshot across other queues, device caches or independently produced data. A readiness signal need not equal a sample, and a work completion need not mean a physical operation or service request completed. Define the application's exact retention, loss, ordering and completion policy rather than labeling every mechanism “events.”

## 7. Hardware integration and measurement semantics

The sample's `DT_ALIAS(accel0)` names a role, `DEVICE_DT_GET(...)` obtains its configured device pointer, and `device_is_ready(...)` checks initialization. The driver must be enabled and initialized; a pointer neither acquires exclusive ownership nor guarantees every requested operation exists. Changing hardware can preserve client code only when the replacement supplies the required behavior. [Device acquisition][dt-howtos] · [Pinned sample][accel-sample]

**Fetch/Get:** `sensor_sample_fetch()` performs acquisition into driver-private sample state; `sensor_channel_get()` returns channels from that cached sample. Multiple callers must synchronize the whole transaction, not just individual API calls, to avoid another fetch replacing the intended sample. This is not independent retained history for each client. [Fetch/Get][fetch-get]

**Read/Decode:** acquisition produces encoded data that can be decoded without a second hardware read, with RTIO-oriented submission/completion and buffer lifetime obligations. Preserve request/storage until completion and retain the completed buffer until consumers finish. Streaming, bus behavior and actual asynchronous operation depend on the backend; an async-facing interface does not turn a blocking driver into nonblocking hardware. [Read/Decode][read-decode]

For nxrs, borrow the explicit acquisition/ownership distinction. Provider code should normalize units, axes, validity, source gaps and measurement-time meaning; product calibration/fusion stays with the service. Do not fabricate unavailable timestamps or equate consumer execution time with measurement time. This does not require another processing thread for each helper. [Nxrs ownership baseline][nxrs-events]

## 8. Configuration, startup and lifecycle

[![Build-time selection versus runtime device state](diagrams/zephyr-build-selection.svg)](diagrams/zephyr-build-selection.svg)

*Z4: descriptions and selected code lead to an image; readiness and ownership remain runtime questions. [D2](diagrams/zephyr-build-selection.d2).*

Devicetree/overlays describe hardware instances and properties: buses, addresses, pins, interrupts, aliases and chosen roles. Bindings constrain properties; Kconfig selects software/features and dependencies. CMake/build integration produces compiled device objects and initialization entries. A hardware node is not evidence that the driver exists in the image or initialized successfully. [Devicetree versus Kconfig][dt-kconfig] · [Build][build] · [Device model][device-model]

The system main thread performs initialization and calls application `main()`; the chosen sample/device path has explicit readiness checks. Common static MCU configuration is not a claim that every bus/device has the same dynamic lifecycle or that readiness transfers ownership. [System threads][system-threads] · [Device acquisition][dt-howtos]

For deferred work, pending cancellation and in-flight completion differ. `k_work_cancel_sync()` waits for cancellation/completion under its documented thread/context constraints; a concurrent producer can submit again afterward. Prevent new submissions before reclamation and do not invoke the synchronous wait from the workqueue executing that same work. Generic cancellation cannot alone certify a driver-specific trigger or peripheral has stopped. [Workqueue API][work-api]

Borrow the **selection/initialization/ownership/quiescence distinction**, not the entire devicetree/Kconfig machinery. Nxrs's app metadata, product-platform bindings and capability-local providers already form its composition boundary. Validate resource identities and excluded providers; do not create a global HAL object, generated service graph or second Kconfig merely for similarity. [Nxrs HAL/build architecture][nxrs-hal]

## 9. Timing, observability and qualification

Analyze acquisition/bus time, ISR deferral, workqueue wait, handler duration, consumer wait and sample age separately. Cooperative execution and shared workqueues make handler bounds especially important; preemption and time slicing do not prove deadline or fairness requirements. Fixed storage gives a capacity bound, not a response-time guarantee. This is an evaluation framework rather than a Zephyr benchmark. [Scheduling][scheduling] · [Workqueues][work]

Zephyr's thread analyzer reports configured stack usage and thread statistics; tracing hooks expose kernel/subsystem execution. Add semantic timestamps, source IDs/gaps and operation outcomes to understand a product path. Measure instrumentation overhead and dropped trace data, rather than treating observability as free. [Thread analyzer][analyzer] · [Tracing][tracing]

`native_sim` runs Zephyr's kernel and libraries in a host executable; it is not OS-independent native Rust service testing or proof of browser compatibility. Peripheral emulation exercises real drivers against modeled devices/buses, complementing capability mocks and physical tests. Borrow that **test layering**, not a new nxrs target. [Native simulator][native-sim] · [Peripheral emulation][emulation]

Nxrs acceptance still needs full-queue stop, blocked-read cancellation, callback quiescence, initialization failure, multi-instance ownership, gaps/out-of-order time, fairness and deadlines. Measure construction, first blocking use, repeated blocking/timeouts, steady-state allocation and final linked flash/RAM/stack/heap separately. A source review, successful compilation or rendered diagram is not that evidence. [Qualification baseline][nxrs-events]

## 10. Lessons for nxrs

**Borrow:** explicit device/configuration separation, distinct queue/storage contracts, readiness versus ownership, synchronous cancellation obligations, and driver-emulation/trace practices. **Adapt:** express those obligations in nxrs's existing Rust/NuttX capability and service model. **Do not import by default:** Zephyr APIs into product logic, a Zephyr port, a global service graph, a mandatory async/no_std rewrite, or a second build-description system.

[![Applying Zephyr's contract lessons within nxrs](diagrams/nxrs-direction.svg)](diagrams/nxrs-direction.svg)

*Z6: a design comparison and acceptance checklist, not an nxrs-on-Zephyr architecture. [D2](diagrams/nxrs-direction.d2) · [Shared borrowing decisions](../README.md#borrowing-decisions-and-acceptance-criteria).*

Retain provider-owned waiting/parsing/normalization, narrow typed delivery sinks and one service state owner. The proposed independent stop/important/ordinary capacities use one logical blocking selection point; simple single-queue services can retain std channels. Crossbeam is a qualification candidate for selection, not proof of universal allocation freedom or ISR safety. Bulk buffers need a notification contract that cannot strand data after rejection or partial draining. These remain nxrs design/qualification choices, not behavior acquired from Zephyr. [Concurrency baseline][nxrs-events]

The next architectural work is to strengthen product-profile validation and contract/overload/lifecycle evidence on nxrs's existing intended environments. **No Zephyr proof-of-concept or execution-port milestone follows from this reference.** The purpose is to improve nxrs using prior-art insight without copying an RTOS or its application framework. [Evidence and scope](sources.md)
[device-model]: https://docs.zephyrproject.org/latest/kernel/drivers/index.html
[posix-design]: https://docs.zephyrproject.org/latest/services/portability/posix/implementation/index.html
[posix-overview]: https://docs.zephyrproject.org/latest/services/portability/posix/overview/index.html
[accel-sample]: https://github.com/zephyrproject-rtos/zephyr/blob/fa4f8fb0e470210aee0ae6fb069a281bc6ac887a/samples/sensor/accel_polling/src/main.c
[syscalls]: https://docs.zephyrproject.org/latest/kernel/usermode/syscalls.html
[dt-howtos]: https://docs.zephyrproject.org/latest/build/dts/howtos.html
[fetch-get]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/fetch_and_get.html
[read-decode]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/read_and_decode.html
[dt-kconfig]: https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html
[build]: https://docs.zephyrproject.org/latest/build/cmake/index.html
[msgq]: https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html
[poll]: https://docs.zephyrproject.org/latest/kernel/services/polling.html
[rust-guide]: https://github.com/zephyrproject-rtos/zephyr-lang-rust/blob/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b/README.rst
[rust-alloc]: https://github.com/zephyrproject-rtos/zephyr-lang-rust/blob/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b/zephyr/src/alloc_impl.rs
[rust-docs]: https://docs.zephyrproject.org/latest/develop/languages/rust/index.html
[native-sim]: https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html
[emulation]: https://docs.zephyrproject.org/latest/hardware/emulator/bus_emulators.html
[nxrs-hal]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/hal-platform-architecture.md
[nxrs-readme]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/README.md
[nxrs-events]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md
[posix]: https://docs.zephyrproject.org/latest/services/portability/posix/implementation/index.html
[device]: https://docs.zephyrproject.org/latest/kernel/drivers/index.html
[objects]: https://docs.zephyrproject.org/latest/kernel/usermode/kernelobjects.html
[domains]: https://docs.zephyrproject.org/latest/kernel/usermode/memory_domain.html
[user]: https://docs.zephyrproject.org/latest/kernel/usermode/overview.html
[fetch]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/fetch_and_get.html
[decode]: https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/read_and_decode.html
[dt-get]: https://docs.zephyrproject.org/latest/build/dts/howtos.html
[fifo]: https://docs.zephyrproject.org/latest/kernel/services/data_passing/fifos.html
[work]: https://docs.zephyrproject.org/latest/kernel/services/threads/workqueue.html
[rust]: https://github.com/zephyrproject-rtos/zephyr-lang-rust/blob/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b/zephyr/src/alloc_impl.rs
[native]: https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html
[nxrs-device]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/nuttx-device-access.md
[scheduling]: https://docs.zephyrproject.org/latest/kernel/services/scheduling/index.html
[threads]: https://docs.zephyrproject.org/latest/kernel/services/threads/index.html
[system-threads]: https://docs.zephyrproject.org/latest/kernel/services/threads/system_threads.html
[slabs]: https://docs.zephyrproject.org/latest/kernel/memory_management/slabs.html
[work-api]: https://docs.zephyrproject.org/latest/doxygen/html/group__workqueue__apis.html
[tracing]: https://docs.zephyrproject.org/latest/services/tracing/index.html
[analyzer]: https://docs.zephyrproject.org/latest/services/debugging/thread-analyzer.html

[cpp]: https://docs.zephyrproject.org/latest/develop/languages/cpp/index.html
