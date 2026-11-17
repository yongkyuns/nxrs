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

## Performance and comparisons

Start with the [performance guide](../website/guide/performance.md) to choose a measurement. These summaries answer different questions:

| Question | Reference |
| --- | --- |
| How does message delivery compare on WROOM and Pico 2 W? | [NXRS, NuttX and Zephyr](rtos-comparison.md) |
| How do bounded std inboxes work, and where does their cost come from? | [Bounded channels](std-channel-performance.md) |
| What consumes RAM and flash as the application grows? | [Memory and image size](rust-std-footprint.md) |
| How do I detect allocation, retention and stack pressure? | [Rust allocation](memory-qualification.md), [NuttX heap and stacks](nuttx-memory-qualification.md) |
| How should I test progress, recovery and portability? | [Evaluation method](rtos-characterization.md) |
| How do detection and video perform together? | [PICO performance report](camera-pico-performance.md) |
| How accurate are the evaluated person detectors? | [Pretrained detector comparison](camera-detector-alternatives.md) |
| Which commands should I run? | [Test and fixture index](../tests/README.md) |

Host checks, target builds, emulator execution and physical observations establish different things. Each result identifies its scope. The [NuttX simulator](nuttx-sim.md) and [QEMU](nuttx-qemu.md) references explain the execution fixtures.

## Historical measurements

These reports preserve the original investigation and evidence. Read the summaries above first; older results do not describe every later firmware build.

| Study | Question at the time |
| --- | --- |
| [ESP32-S3 std channel campaign](history/std-channel-campaign.md) | Which queue, wakeup and placement changes address the observed transport gap? |
| [Native-adapter RTOS study](history/native-rtos-study.md) and [resource ledger](rtos-comparison-accounting.md) | How do native threaded services and a cooperative executor compare at equal event capacity? |
| [C/Rust footprint study](history/rust-footprint-study.md) | Which startup, output and transport choices retain code and storage? |
| [Thread-Metric foundation](rtos-benchmarking.md) | What can be reproduced and compared with an external RTOS report? |

## Application-specific notes

The [examples guide](../website/guide/examples.md) separates introductory programs from application-specific code. The [event-driven application](event-driven-demo.md), [camera delivery](camera-detection-delivery.md), and [native camera clients](client-camera-architecture.md) references describe particular applications rather than requirements of the framework.

Implementation plans and historical reports remain in this directory. For current work on peripheral composition, see the [implementation plan](peripheral-composition-plan.md) and its linked issues.
