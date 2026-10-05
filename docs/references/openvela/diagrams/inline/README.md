# Openvela inline diagrams

These figures sit beside the sections they explain in the [overview](../../README.md). They are designed for **800 CSS pixels of reading width**, not as thumbnails of the larger architecture maps. Each one answers a focused question; its caption states what is shown and what is omitted.

## Views and sources

| Section | Diagram | What it explains and supporting evidence |
| --- | --- | --- |
| 1 | [OpenVela extends the NuttX foundation](overview.svg) · [D2](overview.d2) | A representative framework call path. These are software responsibilities, not three processes. [OpenVela overview](https://github.com/open-vela/docs) · [Bluetooth example](https://github.com/open-vela/frameworks_bluetooth/tree/c423c51e69acad244b1d44f138918cbedbc70d40) |
| 2 | [Different languages can use the same C device ABI](languages.svg) · [D2](languages.d2) | C and configured C++ applications can call the C device interface; the ABI does not select a new execution model. [C++ support](https://nuttx.apache.org/docs/latest/guides/cpp_cmake.html) · [Task groups](https://nuttx.apache.org/docs/latest/implementation/tasks_vs_threads.html) · [Sensor interface](https://nuttx.apache.org/docs/latest/components/drivers/special/sensors/sensors_uorb.html) |
| 3 | [Familiar file calls; a NuttX-specific device contract](apis.svg) · [D2](apis.d2) | The LED example separates the file operation from the device-specific meaning of the request. [LED example](https://github.com/open-vela/docs/blob/dev/en/quickstart/development_board/STM32F411.md) |
| 4 | [Defer acquisition; let the application run separately](execution.svg) · [D2](execution.d2) | Orange arrows request later execution. The upper-half readiness notification does not run the application or carry the sample. [Sensor model](https://nuttx.apache.org/docs/latest/components/drivers/special/sensors/sensors_uorb.html) · [Workqueue restrictions](https://nuttx.apache.org/docs/latest/reference/os/wqueue.html) |
| 5 | [Three build choices, not three runtime layers](memory.svg) · [D2](memory.d2) | Flat builds share memory; protected builds separate kernel and user privilege; kernel builds can provide distinct user address environments. [Pinned memory options](https://github.com/open-vela/nuttx/blob/9e79ad292fd103d3b9ef737757081a3f7fbbf9a4/Kconfig) · [Protected builds](https://nuttx.apache.org/docs/13.0.0/guides/protected_build.html) |
| 6 | [Retained samples and reader wakeups are different](data-flow.svg) · [D2](data-flow.d2) | Teal arrows copy records. The orange notification changes readiness; it does not contain a sample. The second reader has independent progress. [Buffered sensor model](https://nuttx.apache.org/docs/latest/components/drivers/special/sensors/sensors_uorb.html) |
| 7 | [Sample time, read-out time and arrival time differ](sensor-data.svg) · [D2](sensor-data.d2) | A FIFO read may return several samples. One interrupt, bus transaction, record and application dispatch are not necessarily one-to-one. [Sensor modes and records](https://nuttx.apache.org/docs/latest/components/drivers/special/sensors/sensors_uorb.html) · [nxrs measurement contract](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |
| 8 | [Device startup and safe shutdown](lifecycle.svg) · [D2](lifecycle.d2) | Startup combines build choices and runtime actions. Safe shutdown must stop new production and finish in-flight use before reclaiming state. [LED startup](https://github.com/open-vela/docs/blob/dev/en/quickstart/development_board/STM32F411.md) · [Task lifetime](https://nuttx.apache.org/docs/latest/implementation/tasks_vs_threads.html) · [nxrs shutdown requirements](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |
| 9 | [Correlate execution traces with the data path](debugging.svg) · [D2](debugging.d2) | OS events show who ran; data timestamps and sequence counters show what happened to a measurement. Correlating both exposes delays and gaps. [NuttX tracing](https://nuttx.apache.org/docs/latest/debugging/tasktraceuser.html) · [nxrs measurement plan](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |
| 10 | [Reuse drivers; make the product boundary explicit](nxrs-lessons.svg) · [D2](nxrs-lessons.d2) | Reuse NuttX below the provider. Normalize device data in the provider; keep product calibration and fusion in the state-owning service. [nxrs device access](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/nuttx-device-access.md) · [Capability contracts](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/hal-platform-architecture.md) · [Concurrency proposal](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md) |

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
