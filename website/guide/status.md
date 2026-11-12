# Current status

NXRS combines Rust standard-library integration for NuttX, reusable firmware components, platform profiles, and application build tooling.

Use this page to decide which part of the repository to evaluate for your application. Support has three separate questions: does a configuration exist, is the needed provider implemented, and has the required behavior been exercised in that environment? A platform name alone answers none of the latter two.

## Available components

The repository includes Rust resource adapters and drivers, capability HALs, reusable services, and ordinary Cargo applications. The build frontend selects the application and profile, prepares the target integration, and records the resulting configuration and image.

Applications can use standard-library facilities directly. [Standard-library APIs](standard-library.md), [Drivers and HALs](hardware.md), and [Profiles and overlays](profiles.md) describe the main interfaces and responsibilities.

| Area | What is available | Where to begin |
| --- | --- | --- |
| Application authoring | Ordinary Cargo packages, handwritten entry points, host execution, and external-workspace builds | [First application](first-application.md) |
| Target integration | NuttX source builds, maintained Rust integration, native preparation, linking, and artifact checks | [Firmware tutorial](firmware.md) |
| Concurrency | Standard-library threads and synchronization, bounded messaging helpers, and explicit owner lifetimes | [Services](services.md) |
| Device access | Capability contracts, native providers, resource adapters, chip drivers, and named composition for supported device families | [Drivers and HALs](hardware.md) |
| Configuration | Platform defaults, named profiles, inheritance, and ordered composition overlays | [Profiles](profiles.md) |
| Reusable software I/O | Explicit feature-selected replay, record storage, and UDP implementations | [Networking and storage](io.md) |
| Development tooling | Profile inspection, build records, host tests, selected target tests, and prepared Zed configuration | [Testing and debugging](testing.md) |

## Completed validation

Existing validation includes host unit and integration tests, target build and ABI checks, firmware execution in the NuttX simulator and QEMU, and on-board evaluations. Measurements cover channel/event delivery, stacks, heap use, image size, and application workloads.

| Area | Tests and results |
| --- | --- |
| Rust/NuttX integration | [Standard-library integration](../../docs/nuttx-std.md) |
| Application and component behavior | [Repository test index](../../tests/README.md) |
| Simulator execution | [NuttX simulator](../../docs/nuttx-sim.md) |
| Emulated target execution | [NuttX QEMU](../../docs/nuttx-qemu.md) |
| Event delivery | [Channel performance](../../docs/std-channel-performance.md) |
| Resource use | [Rust std footprint](../../docs/rust-std-footprint.md) |
| Reference comparisons | [RTOS comparisons](../../docs/rtos-comparison.md) |

Each report records its tested configuration. Application-specific results are linked from [Examples](examples.md).

The [characterization campaign](../../tests/rtos-characterization/README.md) adds controlled messaging, memory, progress, and lifecycle observations. Read the results' source/image/configuration scope when using them. Historical reports retain their original cohorts; a later source change is not automatically covered by an earlier physical run.

## Target configuration

Rust `std` integration uses maintained patches and target-specific build support. Select the board, OS configuration, device bindings, and resource budgets through the build frontend and profiles.

`cargo nxrs platforms` lists the platform families. The [build reference](../../docs/build-system.md) and board guides describe their setup. A host program using only `std` does not need hardware bindings.

| Family | Intended environment | Boundary to check |
| --- | --- | --- |
| `linux`, `macos`, `windows` | Native host development and implemented host providers | OS-specific capability availability and prerequisites |
| `wroom` | ESP32-S3 WROOM/Freenove NuttX products | The selected product, peripherals, memory, and boot configuration |
| `cores3` | M5Stack CoreS3 NuttX configuration | The selected module/wiring and outstanding physical qualification |
| `pico2w` | Pico 2 W / RP2350 NuttX configuration | The selected radio, device, and firmware feature set |
| `web` | Emscripten thread qualification | Worker/thread setup; hardware HALs remain unbound |

The ESP32-S3 and ARM QEMU profiles are explicit qualification inputs, separate from board-family defaults. Browser threading is a separate execution path from a stock `wasm32-unknown-unknown` build; blocking firmware entry points do not run on the browser UI thread. See [Browser threads](../../docs/browser-threads.md).

## Evaluate NXRS for a product

1. Run a small source application on the host and selected target.
2. List the APIs, devices, and lifecycle behavior the application requires.
3. Check each capability's provider and profile support, then exercise its actual device path.
4. Measure memory, scheduling, throughput, and failure recovery with the intended workload.
5. Keep the selected source revision, configuration, toolchain, image, and test records together.

This makes the missing work concrete: a new provider, BSP resource registration, an API gap, a runtime correction, or additional testing. The [architecture guide](overview.md) explains where each belongs.

## Ongoing work

Development continues on device support, component reuse, build integration, and performance and memory evaluation. The [repository issues](https://github.com/yongkyuns/nxrs/issues) track the active work.
