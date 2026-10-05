# Embedded architecture prior art

These references evaluate **where nxrs fits among embedded software architectures and which ideas are worth borrowing**. They are not a plan to run nxrs on Zephyr, OpenVela or PX4, nor a proposal to replace NuttX. Cross-OS portability is examined as a property of an interface, not as a deployment objective.

The comparison deliberately spans different architectural levels. **NuttX/OpenVela** shows an OS and device foundation extended with product frameworks; **Zephyr** shows an integrated RTOS, device/configuration model and kernel contracts; **PX4** shows a domain application stack and middleware built above an OS. PX4 is not another RTOS kernel. Treating all three as interchangeable kernels would obscure the most useful lessons. See each reference's [role analysis](#common-review-questions).

## Reading the collection

Each system's **README** answers the same ten architectural questions. Its **architecture atlas** contains detailed diagrams and mechanism-specific explanations; its **sources** distinguish pinned code from rolling manuals; its **diagram README** records viewing sizes, rendering and validation. Detail belongs with the system it describes. Shared comparison does not require shared rendering dependencies or a universal component diagram.

| Reference | Architectural analysis | Detailed views | Evidence |
| --- | --- | --- | --- |
| OpenVela / NuttX | [Analysis](openvela/README.md) | [Five-view atlas](openvela/architecture-atlas.md) | [Sources](openvela/sources.md) |
| Zephyr | [Analysis](zephyr/README.md) | [Six-view atlas](zephyr/architecture-atlas.md) | [Sources](zephyr/sources.md) |
| PX4 | [Analysis](px4/README.md) | [Execution and data-flow atlas](px4/architecture-atlas.md) | [Sources](px4/sources.md) |

## Common review questions

These links are also the coverage map. Equal coverage means answering the same question, **not inventing equivalent features or identical execution paths**. The atlases retain the sensor, Bluetooth, ownership and control details that distinguish the systems.

| Question | OpenVela / NuttX | Zephyr | PX4 |
| --- | --- | --- | --- |
| What layer is this, and what problem does it solve? | [Role](openvela/README.md#1-role-and-architectural-position) | [Role](zephyr/README.md#1-role-and-architectural-position) | [Role](px4/README.md#1-role-and-architectural-position) |
| Which languages, runtimes and lifetime rules apply? | [Languages](openvela/README.md#2-languages-and-runtime-model) | [Languages](zephyr/README.md#2-languages-and-runtime-model) | [Languages](px4/README.md#2-languages-and-runtime-model) |
| What do API and portability boundaries actually promise? | [Contracts](openvela/README.md#3-apis-contracts-and-portability-boundaries) | [Contracts](zephyr/README.md#3-apis-contracts-and-portability-boundaries) | [Contracts](px4/README.md#3-apis-contracts-and-portability-boundaries) |
| Who executes, blocks, schedules and handles interrupts? | [Execution](openvela/README.md#4-execution-scheduling-and-isr-boundaries) | [Execution](zephyr/README.md#4-execution-scheduling-and-isr-boundaries) | [Execution](px4/README.md#4-execution-scheduling-and-isr-boundaries) |
| Where do state and buffers live; what is protected? | [Memory](openvela/README.md#5-memory-ownership-and-protection) | [Memory](zephyr/README.md#5-memory-ownership-and-protection) | [Memory](px4/README.md#5-memory-ownership-and-protection) |
| What is copied, retained, consumed, notified or acknowledged? | [Data flow](openvela/README.md#6-data-flow-and-communication-contracts) | [Data flow](zephyr/README.md#6-data-flow-and-communication-contracts) | [Data flow](px4/README.md#6-data-flow-and-communication-contracts) |
| Where are hardware differences and measurement semantics resolved? | [Acquisition](openvela/README.md#7-hardware-integration-and-measurement-semantics) | [Acquisition](zephyr/README.md#7-hardware-integration-and-measurement-semantics) | [Acquisition](px4/README.md#7-hardware-integration-and-measurement-semantics) |
| How are instances selected, started, stopped and reclaimed? | [Lifecycle](openvela/README.md#8-configuration-startup-and-lifecycle) | [Lifecycle](zephyr/README.md#8-configuration-startup-and-lifecycle) | [Lifecycle](px4/README.md#8-configuration-startup-and-lifecycle) |
| What can be observed or tested; what remains unproved? | [Qualification](openvela/README.md#9-timing-observability-and-qualification) | [Qualification](zephyr/README.md#9-timing-observability-and-qualification) | [Qualification](px4/README.md#9-timing-observability-and-qualification) |
| What should nxrs borrow, adapt or avoid, and why? | [Lessons](openvela/README.md#10-lessons-for-nxrs) | [Lessons](zephyr/README.md#10-lessons-for-nxrs) | [Lessons](px4/README.md#10-lessons-for-nxrs) |

## Where nxrs fits

The useful interpretation of **architectural evolution** is a progression of concerns, not a chronology or a ranking of projects:

1. **Execution and hardware mechanisms:** scheduling, interrupts, memory, buses, drivers and basic IPC.
2. **Reusable contracts:** device classes, standardized OS APIs, subsystem interfaces and typed measurements.
3. **Application composition:** state ownership, execution placement, triggers, retention, lifecycle and observability.
4. **Explicit, testable obligations:** language-level ownership where applicable, bounded admission, measurement-time semantics, cancellation, and measured cost.

All three prior-art systems address more than one concern. C/C++ systems are not inherently missing ownership disciplines, and adopting Rust does not prove real-time behavior, memory isolation or bounded resource use. The question is **which obligations an interface encodes, which its implementation enforces, and which still depend on application policy and measurement**.

Nxrs belongs primarily at the application-composition and capability-contract level above existing OS facilities. Its baseline uses ordinary Rust `main()`, capability-local facades and selected providers, state-owning services and direct local computation. The concurrency document proposes provider-owned acquisition and typed delivery into independently bounded admission classes with one logical blocking selection point. It does not define a replacement RTOS, a mandatory actor framework or a global publish/subscribe graph. [HAL architecture][nxrs-hal] · [Concurrency baseline][nxrs-events]

**Its intended value is not merely wrapping POSIX or using Rust syntax.** The architectural hypothesis is that product-facing contracts, explicit ownership and small composition boundaries can preserve the useful structure of mature embedded frameworks without requiring their entire middleware or configuration stack. That hypothesis needs evidence of correctness, understandable behavior and acceptable linked/RAM/timing cost; this collection does not establish superiority over the prior art.

### Compare obligations, not slogans

| Obligation | OpenVela / NuttX | Zephyr | PX4 | Nxrs evaluation |
| --- | --- | --- | --- | --- |
| Reuse below product logic | Device classes and subsystem adapters | Device-class operations and configured instances | Bus/driver facilities and normalized reports | Reuse proven drivers; define only the product-facing difference. |
| Execution ownership | Tasks/pthreads, worker contexts, subsystem loops | Threads and workqueues selected by the application/subsystem | Dedicated tasks plus shared serial work-item execution | Keep one state owner; add an independent context only for a real blocking, isolation or timing reason. |
| Data lifetime | Driver buffers, reader state, subsystem transport contracts | Copied records, intrusive items, cached samples or explicit buffers | Topic retention plus independent reader generations | Declare move/copy/borrow, capacity, loss and recovery per path. |
| Isolation | Selected NuttX memory organization | Optional userspace and object/memory permissions | Normally shared application memory in the reviewed paths | Do not confuse Rust ownership or a service boundary with MPU/MMU isolation. |
| Completion | Device/framework-specific | Queue/work API-specific | Publication, scheduling and processing are distinct | Admission, processing, acknowledgment, stop and join are separate milestones. |
| Evidence | OS and driver tests, subsystem traces | Kernel traces, driver emulation, native_sim | Worker/topic diagnostics and product simulation | Correlate OS execution with semantic sample age, gaps and lifecycle outcomes. |

The cells summarize the linked per-system analyses, not a claim of whole-product equivalence. Device selection does not imply exclusive ownership; a shared buffer does not imply a broker; a callback does not imply a context switch; a language binding does not imply a runtime port.

## Borrowing decisions and acceptance criteria

These are design recommendations, not newly implemented nxrs behavior.

| Decision | Prior-art lesson | What to retain or change in nxrs | Evidence needed before adoption |
| --- | --- | --- | --- |
| **Borrow: layered device reuse** | OpenVela separates generic device behavior from chip/platform adaptation. | Keep existing NuttX access inside capability providers; contain foreign types and only necessary ABI glue. Do not create another generic POSIX wrapper. | Real device tests for initialization, errors, units, timestamps and resource ownership. |
| **Borrow: selection is not lifetime** | Zephyr distinguishes descriptions, compiled devices, readiness and actual access. | Validate product bindings and resource identities; make session readiness/ownership explicit. | Reject missing/duplicate incompatible resources and verify unused-provider exclusion. |
| **Borrow: data is not a wakeup** | PX4 and NuttX retain sensor data separately from execution notification; Zephyr work can coalesce. | Give each input a retention and trigger policy. Preserve source gaps and ensure partial drains cannot strand data. | Burst, overrun, repeated-wakeup, stale-data and partial-drain tests. |
| **Adapt: bounded communication** | Byte queues, intrusive queues, topic rings and work lists have different contracts. | Preserve Rust move/drop semantics and separate stop/important/ordinary capacity where needed. No universal queue abstraction that hides these differences. | Full/closed outcomes, overload progress, correct destruction, stop under saturation and fairness tests. |
| **Defer: shared workers** | PX4 and Zephyr trade fewer stacks for same-worker interference. | Retain direct computation within an owner; introduce a shared executor only for a measured need. | Matched tail latency, handler bounds, stack/heap and final linked-image measurements. |
| **Borrow: layered observability** | OS execution alone does not explain application data age; topic rate alone does not explain scheduling. | Correlate ISR/acquisition, admission, dispatch and output using timestamps, sequence IDs and lifecycle events. | Measured instrumentation overhead and trace-loss visibility; reproducible fault cases. |
| **Exclude from this work: new RTOS ports** | Interface reuse and whole-runtime support are different obligations. | Learn from Zephyr/OpenVela/PX4 without adding a target port, global service graph or copied framework. | No port milestone is implied by this research. |

The existing nxrs multi-queue candidate remains bounded Crossbeam plus selection; simpler std bounded channels remain valid. Neither is assumed ISR-safe or allocation-free in every phase. Keep hardware waits/parsing/normalization below the service, pass only normalized semantic events through typed sinks, and use direct calls/borrows for tightly coupled calculations. Independent reserved capacity is not infinite capacity, CPU reservation or preemption of an active handler. [Concurrency baseline][nxrs-events]

## Shared terminology and evidence rules

Use **execution context** for an actual ISR, task, thread or worker; **component/service** for a logical owner; **work item** for scheduled handler state; and **queue/ring** for the specified storage or runnable list. Name which of those is meant by “event.” Use **contract** for behavior as well as signatures: units, time, lifetime, error outcomes, loss, cancellation and completion. HAL means nxrs's capability/provider boundary here; a NuttX lower half or OpenVela VHAL is a distinct, explicitly named mechanism.

Distinguish **observed source behavior**, **documented API guarantees**, **architectural inference**, and **nxrs recommendation** in the surrounding wording. Pinned examples take precedence for exact implementation details; rolling manuals provide qualified context. All nxrs comparisons use the same [baseline source][nxrs-readme], while earlier snapshots remain identified in the source indexes only where needed to preserve example provenance. A proposed architecture present in the repository is not automatically implemented or target-qualified.

The prose is English; C, C++, Rust and upstream API identifiers retain their technical meaning. Diagram legends identify dependency, data/ownership, notification and protection separately; actual colors and renderer settings are documented per atlas. Source dates, commit pins and validation provenance are recorded separately in the evidence/reproduction documents.

**Not established here:** benchmark rankings, worst-case execution guarantees, universal firmware support, a memory-safety proof, or the qualification of a new physical HAL. Existing native/browser demonstrations remain useful test evidence within their stated scope, not a reason to introduce another RTOS target. [Nxrs qualification boundary][nxrs-events]
[nxrs-hal]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/hal-platform-architecture.md
[nxrs-events]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md
[nxrs-readme]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/README.md
[nxrs-device]: https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/nuttx-device-access.md
