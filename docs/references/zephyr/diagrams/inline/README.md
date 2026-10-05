# Zephyr inline diagrams

These figures sit beside the sections they explain in the [overview](../../README.md). They are designed for **800 CSS pixels of reading width**, not as thumbnails of the larger architecture maps. Each one answers a focused question; its caption states what is shown and what is omitted.

## Views and sources

| Section | Diagram | What it explains and supporting evidence |
| --- | --- | --- |
| 1 | [Device APIs and kernel APIs serve different needs](overview.svg) · [D2](overview.d2) | Applications normally use native APIs. Optional POSIX compatibility covers selected OS services, not every peripheral contract. [Device model](https://docs.zephyrproject.org/latest/kernel/drivers/index.html) · [POSIX scope](https://docs.zephyrproject.org/latest/services/portability/posix/overview/index.html) |
| 2 | [Language support and runtime support are separate](languages.svg) · [D2](languages.d2) | The Rust row describes the inspected module, not every possible Rust port. All bindings still need valid C-side lifetimes and callback rules. [Pinned Rust module](https://github.com/zephyrproject-rtos/zephyr-lang-rust/blob/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b/README.rst) · [C++ support](https://docs.zephyrproject.org/latest/develop/languages/cpp/index.html) · [Message queues](https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html) |
| 3 | [A device pointer identifies an instance, not ownership](apis.svg) · [D2](apis.d2) | Configuration, operations and mutable state have different jobs. Obtaining a device pointer does not grant exclusive access or prove readiness. [Device structure](https://docs.zephyrproject.org/latest/kernel/drivers/index.html) · [Device lookup and readiness](https://docs.zephyrproject.org/latest/build/dts/howtos.html) |
| 4 | [A shared worker runs one handler at a time](execution.svg) · [D2](execution.d2) | The ISR schedules work rather than running all application logic. The worker is one thread; other threads and interrupts can still execute. [Workqueue execution and coalescing](https://docs.zephyrproject.org/latest/kernel/services/threads/workqueue.html) · [Scheduling](https://docs.zephyrproject.org/latest/kernel/services/scheduling/index.html) |
| 5 | [Object permission and RAM permission are different](memory.svg) · [D2](memory.d2) | This illustrates a user-mode call that uses both a kernel object and a user buffer. Object authorization and buffer access are checked separately. Both applicable validations must succeed; their order is not prescribed here. [System calls](https://docs.zephyrproject.org/latest/kernel/usermode/syscalls.html) · [Object access](https://docs.zephyrproject.org/latest/kernel/usermode/kernelobjects.html) · [Memory domains](https://docs.zephyrproject.org/latest/kernel/usermode/memory_domain.html) |
| 6 | [Copying bytes, linking items and scheduling work differ](data-flow.svg) · [D2](data-flow.d2) | These are three different mechanisms, not stages of one pipeline. A pointer copied by k_msgq still needs a referent-lifetime contract. [Message queues](https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html) · [Intrusive FIFO](https://docs.zephyrproject.org/latest/kernel/services/data_passing/fifos.html) · [Workqueues](https://docs.zephyrproject.org/latest/kernel/services/threads/workqueue.html) |
| 7 | [Fetch/Get and Read/Decode keep samples differently](sensor-data.svg) · [D2](sensor-data.d2) | The upper path shares the driver cache. The lower path exposes a completed encoded buffer. Async behavior and supported operations remain driver-dependent. [Fetch/Get](https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/fetch_and_get.html) · [Read/Decode](https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/read_and_decode.html) |
| 8 | [Selection, readiness and shutdown are separate steps](lifecycle.svg) · [D2](lifecycle.d2) | The build row summarizes static device startup. The work-item row requires a context that can wait safely; it is not a universal peripheral shutdown API. [Device startup](https://docs.zephyrproject.org/latest/kernel/drivers/index.html) · [Build selection](https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html) · [Work cancellation](https://docs.zephyrproject.org/latest/doxygen/html/group__workqueue__apis.html) |
| 9 | [Different tests expose different failures](debugging.svg) · [D2](debugging.d2) | Test scopes are complementary, not a runtime pipeline. Zephyr bus emulation is prior art for stronger testing of nxrs’s existing providers, not a port target. [Peripheral emulation](https://docs.zephyrproject.org/latest/hardware/emulator/bus_emulators.html) · [Native simulation](https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html) · [nxrs tests](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |
| 10 | [Borrow explicit contracts, not another RTOS](nxrs-lessons.svg) · [D2](nxrs-lessons.d2) | Apply these ideas inside nxrs’s Rust/NuttX design. Keep product configuration, lifetime and test requirements explicit without copying Zephyr’s build system. [Device model](https://docs.zephyrproject.org/latest/kernel/drivers/index.html) · [Queue contracts](https://docs.zephyrproject.org/latest/kernel/services/data_passing/message_queues.html) · [nxrs baseline](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |

## Reading the arrows

Blue solid arrows are calls or local execution steps. Teal solid arrows carry records, copies or explicitly described item transfers. Orange dashed arrows are wakeups or requests for later execution. Grey dotted arrows are configuration/dependency, comparison or test-correlation relationships, as named in the caption. They do not imply runtime data transport.

A box is not automatically a thread, process, memory region or queue. Container titles and captions identify the intended boundary. Readiness is not a payload, a copied pointer does not copy its referent, and a lifecycle checklist is not a universal OS API.

## Reproduce and check

Use the **official D2 v0.9.0 binary with bundled TALA**, Bash and Python 3.9+. The Linux amd64 release archive SHA-256 is `5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9`. [Official release](https://github.com/d2lang/d2/releases/tag/v0.9.0).

```sh
D2=/path/to/d2 bash render.sh
python3 check_browser.py --chromium /path/to/chromium --screenshots-dir /tmp/inline-previews
```

Run these commands in this directory. Playwright and Chromium are optional documentation-validation dependencies for the second command, not firmware dependencies.

The `.d2` files own wording, node dimensions, placement and graph topology. `routes.json` owns boundary ports, orthogonal corridors and edge-label offsets. `route_svg.py` applies those routes without moving nodes. The complete input is D2 plus its palette, pinned renderer and routing file; D2 pasted alone into a playground does not reproduce the published routes.

`render.sh` performs two independent raw and routed renders and compares their bytes. It rejects node intersections, wire crossings, shared collinear wire segments, inconsistent topology and incorrect endpoint directions, and runs the seven existing routing regression tests. No crossing is hidden with a painted overlay.

`check_browser.py` loads the SVG's embedded fonts and checks actual text bounds, node containment, text overlaps, clipping and a 3px text-to-connector-centerline margin. It checks that the smallest text stays at least **15px at 800px width**, then exports images at natural and document reading size. See `browser-checks.json` for exact per-figure dimensions, font sizes and the browser version. These measurements do not prove architectural correctness.

The review data is in `routing-checks.json` and `browser-checks.json`. `manifest.json` maps each source to its section, scope and evidence. All files are local to this diagram set; no sibling RTOS reference is needed to render it.
