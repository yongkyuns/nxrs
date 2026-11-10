# Current status

NXRS has working applications and basic validation of its Rust/NuttX integration and event-driven firmware components.

## Available components

The repository includes ordinary Rust application entry points, reusable services, hardware capability interfaces, platform profiles, peripheral wrappers and the Rust standard-library integration for NuttX.

The [navigation example](examples.md#navigation-and-service-composition) connects sensor, fusion and health services. The [IoT camera](examples.md#iot-camera) combines capture, detection, transport, gateway delivery and client-facing state.

## Completed validation

| Area | Tests and evaluations |
| --- | --- |
| Portable components | Host tests for application behavior, service lifecycle, ownership and failure handling |
| NuttX integration | Target build and ABI checks, plus firmware execution in the NuttX simulator and ESP32-S3 QEMU |
| Event-driven runtime | Channel, wake-up and delivery measurements, including on-board results |
| Memory use | Code-size analysis, queue storage, thread stacks and heap measurements |
| Camera application | On-device streaming and detection tests, recovery tests and model evaluations |

The [test index](../../tests/README.md) contains the test commands. The [Rust std integration reference](../../docs/nuttx-std.md), [event-path measurements](../../docs/std-channel-performance.md) and [footprint analysis](../../docs/rust-std-footprint.md) document configurations and results.

Camera results are recorded in the [delivery guide](../../docs/camera-detection-delivery.md) and [model evaluation report](../../docs/camera-detection-qualification.md), which also links to subsequent detector work.

## Target and configuration requirements

Available APIs and peripherals depend on the selected Rust toolchain, NuttX configuration and platform profile. NXRS maintains integration patches for those configurations. Adding a board or peripheral requires its bindings, drivers and tests.

Application budgets include thread stacks, allocations, queues, code size and timing. The [performance guide](performance.md) describes how to measure these. Camera deployment requirements are documented in the [security guide](../../docs/security.md); detector accuracy and supported resolutions are recorded with each model's results.

## Ongoing work

Development continues on reusable subsystems, additional hardware and applications, toolchain integration, and performance and memory evaluations. The [repository issues](https://github.com/yongkyuns/nxrs/issues) track individual tasks.
