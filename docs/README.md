# Documentation

Start with the [user guide](../website/guide/start-here.md) for setup, application development, and firmware builds. The [repository README](../README.md) gives a short project overview.

## Framework and development

| Reference | Contents |
| --- | --- |
| [Architecture](architecture.md) | Components and source ownership |
| [Build system](build-system.md) | Applications, profiles, toolchains, and outputs |
| [Rust std on NuttX](nuttx-std.md) | Standard-library and target integration |
| [HAL architecture](hal-platform-architecture.md) | Capability APIs and implementation boundaries |
| [NuttX device access](nuttx-device-access.md) | Native resources and Rust access |
| [Peripheral composition](peripheral-composition.md) | Devices, resources, aliases, and overlays |
| [Static-device composition](static-device-composition.md) | Device identity and ownership |
| [Event communication](concurrency-event-communication.md) | Delivery, execution, and lifecycle |
| [Editor setup](editor-setup.md) | Local Rust and native tooling |
| [Security](security.md) | Trust boundaries and deployment requirements |

## Tests and evaluations

Use the [test index](../tests/README.md) for commands and prerequisites. The [NuttX simulator](nuttx-sim.md) and [QEMU](nuttx-qemu.md) guides cover firmware execution. [Event-path measurements](std-channel-performance.md), [Rust std footprint](rust-std-footprint.md), and [RTOS comparisons](rtos-comparison.md) record performance and memory evaluations.

## Application-specific notes

The [examples guide](../website/guide/examples.md) separates introductory programs from application-specific code. The [event-driven application](event-driven-demo.md), [camera delivery](camera-detection-delivery.md), and [native camera clients](client-camera-architecture.md) references describe particular applications rather than requirements of the framework.

Implementation plans and historical reports remain in this directory. For current work on peripheral composition, see the [implementation plan](peripheral-composition-plan.md) and its linked issues.
