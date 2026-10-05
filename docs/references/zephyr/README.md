# Zephyr and nxrs: abstraction boundaries and design direction

**Research reference · 2026-09-30 · non-normative.** This note reviews Zephyr's device/configuration model and derives recommendations for nxrs. It does not propose replacing NuttX or claim a working nxrs-on-Zephyr port. [Sources and exact snapshots](sources.md) distinguish implemented nxrs behavior from the unmerged concurrency proposal.

## Expanded architecture atlas

The **2026-10-04 [architecture atlas](architecture-atlas.md)** adds six source-backed views covering API/contract boundaries, memory/protection, execution/data ownership and portability. Read the atlas for each figure's scope and assumptions; open the linked SVGs at the documented standalone reading width.

## Main finding

**Zephyr provides reusable hardware APIs within Zephyr; nxrs adds a product-facing boundary intended to survive changes of OS and backend.** These are complementary layers. Borrow Zephyr's explicit hardware descriptions, device-class interfaces and driver testing practices, but keep Zephyr-specific types and configuration below nxrs capability facades. A second kernel is not required to obtain those architectural benefits. [Device model][device-model] · [nxrs HAL architecture][nxrs-hal]

## 1. POSIX is an optional path, not Zephyr's foundation

![Zephyr native and optional POSIX API routes](diagrams/zephyr-abstractions.svg)

*Expanded architecture view (Z1): solid blue arrows are calls; POSIX is optional compatibility, not a POSIX interface for every peripheral or a separate process. [D2 source](diagrams/zephyr-abstractions.d2).*

Zephyr's POSIX implementation is an opt-in compatibility library over its kernel and shared subsystems. Native applications can instead use `k_thread_*`, `k_msgq_*`, device APIs and other Zephyr services directly. POSIX support must be checked against the selected options and individual functions; it is not a promise that any POSIX application or Rust standard library will run unchanged. [POSIX design][posix-design] · [Configuration and scope][posix-overview]

The same include-path distinction as in OpenVela applies:

| Header or symbol | What upper code depends on |
| --- | --- |
| `<zephyr/kernel.h>` and `k_*` | Public, Zephyr-specific OS services, not automatically private kernel internals. |
| `<zephyr/device.h>` and `<zephyr/drivers/sensor.h>` | Device instances and a hardware-independent **Zephyr** device-class contract. |
| `<zephyr/devicetree.h>` and `DT_*` | Build-time hardware descriptions; not an OS-neutral application API. |
| Generated `zephyr/syscalls/...` headers | Public API dispatch support; not evidence of an IPC or privilege transition on every call. |

The upstream accelerometer sample uses public device, sensor and kernel headers together. For APIs declared `__syscall`, generated wrappers call implementations directly without userspace, while user-mode calls require validation and privilege transitions. Thus neither header spelling nor the word “syscall” alone establishes runtime overhead or broken layering. [Sample source][accel-sample] · [System calls][syscalls]

## 2. Hardware reuse: device APIs plus configured instances

The device model selects a driver's operations through its API table and initializes devices configured into the image. Sensor APIs hide sensor-specific register handling; bus APIs hide controller differences. This is conceptually related to NuttX's device-class/driver separation, but does not require routing peripheral access through `open/read/ioctl`. [Device model][device-model] · [Sensor example][accel-sample]

A concrete example is the accelerometer sample: `DT_ALIAS(accel0)` identifies a role, `DEVICE_DT_GET(...)` obtains its device, and `device_is_ready(...)` checks initialization before sensor access. Changing the hardware mapping can preserve the client code when the replacement implements the required operations. Obtaining a device pointer does **not** initialize it, establish readiness, or transfer exclusive ownership. The driver must actually be enabled and successfully initialized. [Device acquisition][dt-howtos]

For nxrs, the corresponding boundary is `service → capability facade → selected provider`. A future Zephyr IMU provider could call the existing sensor API; a NuttX provider can continue using NuttX drivers. Neither provider's device pointer, bus settings or foreign types should become the public IMU contract. [nxrs HAL architecture][nxrs-hal]

**Semantic compatibility needs more than matching signatures.** Zephyr's established Fetch/Get API blocks while updating driver-private sample state; multiple callers require synchronization across fetch/get. Its Read/Decode path instead exposes caller/buffer-oriented acquisition with RTIO integration; realizing the full asynchronous benefits depends on driver and bus support. Neither makes every sensor's FIFO, trigger or timestamp behavior identical. [Fetch/Get][fetch-get] · [Read/Decode][read-decode]

Recommendation: one provider owns acquisition and normalizes units, axes, validity, sequence gaps and measurement-time semantics. Do not equate a timestamp taken after a blocking read with physical measurement time. Keep raw register/protocol parsing below HAL, while calibration/fusion product policy remains in the service. This follows the proposed nxrs ownership boundary rather than requiring another processing thread for every stage. [nxrs concurrency proposal][nxrs-events]

## 3. Devicetree and Kconfig solve different selection problems

![Zephyr hardware and software inputs resolved at build time](diagrams/zephyr-build-selection.svg)

*Expanded build/runtime view (Z4): grey dotted build relationships lead to initialization and explicit readiness checks, not a runtime service registry. [D2 source](diagrams/zephyr-build-selection.d2).*

Devicetree describes hardware instances and initial configuration: buses, addresses, pins, interrupts and device roles through aliases/chosen nodes. Bindings describe allowed properties. Kconfig selects software features and drivers. Zephyr combines board/application configuration during the build; a devicetree node alone is not proof that a functioning driver was linked. [Devicetree versus Kconfig][dt-kconfig] · [Build flow][build] · [Acquisition checks][dt-howtos]

**Borrow the separation, not necessarily the machinery.** Nxrs already separates app-owned `main()` and firmware-entry metadata from product-platform settings, capability-local provider selection and NuttX configuration. Keep that model. Hardware binding metadata is not a global runtime HAL object, a service registry or a generated application graph. [nxrs architecture][nxrs-hal] · [Current entry/build model][nxrs-readme]

Near-term improvement: validate each product profile's required capabilities, provider/target compatibility, resource identity and omitted providers before building. Keep physical instances distinct from provider implementation selection: selecting one provider implementation does not imply one device instance. Introduce a devicetree frontend only when wiring complexity justifies it; do not build a second Kconfig or copy Zephyr's C macro interface into portable Rust services.

## 4. Execution portability is a separate decision

Zephyr's `k_msgq` supports bounded, fixed-size copied messages. `k_poll()` waits for supported **kernel objects**, not arbitrary POSIX descriptors or Rust channel receivers. It reports readiness, not ownership: the caller must still acquire the object, handle contention and correctly reset event state. These are useful implementation mechanisms, not a drop-in replacement for nxrs's channel contract. A Rust adapter must also preserve move/drop ownership; a C byte-copy queue is not automatically safe for arbitrary owning Rust values. [Message queues][msgq] · [Polling semantics][poll]

![Proposed nxrs provider delivery into capacity-isolated queues and one selection point](diagrams/nxrs-direction.svg)

*Expanded provider/execution view (Z6): proposed nxrs event delivery, not an implemented Zephyr backend. Queues have separate stop/important/ordinary capacity; arrows are delivery through injected typed sinks, not HAL dependencies on private service enums. [D2 source](diagrams/nxrs-direction.d2).*

The reviewed nxrs proposal requires **one logical blocking selection point**, not one physical queue for everything. Important events and ordinary measurements have independent bounded capacity; shutdown may have its own reserved queue. Crossbeam bounded channels/selection are the preferred qualification candidate for these multi-queue cases; existing std bounded channels remain valid for simple single-queue cases. This is still a proposed baseline with target qualification pending. [Pinned proposal][nxrs-events]

Keep device waits, callbacks, parsing and normalization behind HAL. A provider may use a worker, an existing driver context, or a shared provider loop; a thread per device is not mandatory. Do not add a service-side `k_poll` reactor or a relay thread just to translate HAL output. Keep tightly coupled processing in direct calls/borrows. Large streams use explicit bounded buffers with a tested notification path into the same selection point; ordinary traffic must not consume important-event capacity. [Pinned proposal][nxrs-events]

### Rust support is not automatically Rust `std`

The reviewed official `zephyr-lang-rust` module documents a `no_std` integration, with optional `alloc` via `CONFIG_RUST_ALLOC`; its allocator documentation explicitly says `std` is not supported by that integration. It supplies Zephyr-specific facilities instead. This does not rule out a separate/custom std port, nor prove every Zephyr target has identical Rust support. [Module guide][rust-guide] · [Pinned allocator source][rust-alloc] · [Integration documentation][rust-docs]

Therefore adding a `hal/*/zephyr` provider would not by itself port nxrs's ordinary-main/std-thread applications. A Zephyr target needs a separately qualified execution/entry strategy. **Do not impose a global no_std or async rewrite merely to add a reference backend.** Continue the existing NuttX/std direction; consider narrow execution adapters or a qualified std port only when an actual deployment warrants that work. Current nxrs target probes are not proof that every full application already works on every target. [nxrs execution scope][nxrs-readme]

## 5. Comparison and direction for nxrs

| Concern | Zephyr mechanism | Direction for nxrs |
| --- | --- | --- |
| OS portability | Native Zephyr APIs; optional POSIX compatibility | Retain qualified std/POSIX paths; contain target-specific operations. |
| Hardware reuse | Device-class APIs and configured driver instances | Reuse OS drivers inside capability providers; avoid rebuilding the driver stack. |
| Build selection | Devicetree/bindings + Kconfig | Keep app metadata separate from checked product-platform/provider bindings. |
| Service execution | Kernel threads, queues, polling and optional subsystems | Preserve ordinary Rust ownership, isolated queue capacity and one logical wait. |
| Host testing | `native_sim` runs the Zephyr kernel on a host | Distinguish OS integration tests from genuinely OS-independent service tests. |
| New target claim | Requires relevant driver/runtime support | Qualify provider semantics **and** execution; a compile is not a behavioral proof. |

*Mechanisms: [POSIX][posix-design], [devices][device-model], [configuration][dt-kconfig], [polling][poll], [native simulator][native-sim]. Nxrs direction is this note's recommendation, grounded in the [existing architecture][nxrs-hal] and [unmerged proposal][nxrs-events].*

Zephyr's `native_sim` compiles applications **together with Zephyr's kernel and libraries** into a Linux executable. It is not equivalent to replacing the OS dependency with native Rust std, and it is not evidence of browser/WASM compatibility. Its peripheral-emulation framework can exercise real peripheral drivers against emulated buses/devices, including fault injection. This complements, rather than replaces, capability-level mocks and real-hardware tests. [Native simulator][native-sim] · [Peripheral emulation][emulation]

**Recommended sequence:** first strengthen nxrs's product-profile and provider conformance tests; then qualify the proposed multi-queue execution and one real event-producing HAL. Add a Zephyr proof-of-concept only for a concrete target need, reusing the same service behavior and acceptance tests. Keep it optional and below the existing boundaries.

The minimum acceptance evidence should cover independent device instances/ownership, acquisition and initialization failure, timestamps and data gaps, isolated queue capacity, fairness/deadlines under load, full-queue shutdown, cancellation of blocked device waits, and quiescent callbacks before reclamation. Measure construction, first blocking use, steady-state allocations, latency, stack/heap use and **final linked** flash/RAM separately. Verify unused provider/driver exclusion. No performance, allocation-freedom or cross-target support claim is established by this research. [Existing qualification direction][nxrs-events] · [Provider exclusion contract][nxrs-hal]

**Bottom line:** adopt Zephyr's separation of hardware description, software selection and device APIs; preserve nxrs's explicit boundary around product-facing data, ownership and execution. This is a reference, not a migration plan.

[Diagram layout and reproduction](diagrams/README.md) · [Evidence and snapshot index](sources.md)

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
[nxrs-hal]: https://github.com/yongkyuns/EmbeddedRust/blob/bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943/docs/hal-platform-architecture.md
[nxrs-readme]: https://github.com/yongkyuns/EmbeddedRust/blob/bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943/README.md
[nxrs-events]: https://github.com/yongkyuns/EmbeddedRust/blob/7a98e1862fee4a286c4d60869803ef51a67c75bb/docs/concurrency-event-communication.md
