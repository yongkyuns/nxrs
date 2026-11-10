# NXRS examples

Use the small examples to learn a particular API or pattern. Larger applications show how several components fit together and are optional reading.

## Basic firmware

The [basic-firmware package](../examples/basic-firmware/) has a normal Rust entry point, an independent Cargo workspace, and NuttX firmware metadata. It is the starting point for [Your first application](first-application.md).

```sh
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --bin hello
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --example worker
```

`hello` prints a greeting. `worker` sends typed commands to a bounded inbox and joins the worker after shutdown. It uses no hardware-specific service or driver.

## Standard-library recipes

[app/std-demo](../../app/std-demo/) contains collections, ownership, channels, synchronization, and deadline examples:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

Use [Standard-library APIs](standard-library.md) for an overview and [Building for NuttX](firmware.md) to build this application for a target.

## IoT application structure

A connected logger or controller commonly needs device access, processing, persistent records, and a connection manager. Those responsibilities can be separate components with explicit resource ownership and typed events.

[Networking and storage](io.md#design-example-a-connected-data-logger) uses a logger as a design example. The application chooses record formats, retention policy, and behavior while disconnected. These are use cases for the framework rather than a required NXRS application model.

<span id="experimental-iot-camera"></span>

## IoT camera

The existing [home-cam application](../../app/home-cam/) combines capture, detection, streaming, and gateway/client integration. Its [delivery reference](../../docs/camera-detection-delivery.md) describes the end-to-end protocol and ownership. Model-specific [evaluation results](../../docs/camera-detection-qualification.md) include streaming, accuracy, memory, and recovery tests.

Read this material when working on a camera application. It is not required for learning the build system, service model, or hardware interfaces.

## Other repository applications

<span id="navigation-and-service-composition"></span>

The repository also contains [event-demo](../../app/event-demo/) and [dual-imu-demo](../../app/dual-imu-demo/), which exercise sensor-specific service composition and named devices. Their [application notes](../../docs/event-driven-demo.md) remain available as implementation examples rather than the default introduction to NXRS.

## Tests and benchmarks

Stress workloads and focused measurement fixtures live alongside the application examples and under `tests/`. Use the [test index](../../tests/README.md) and [Performance and memory](performance.md) when working on those tests; they are separate from the introductory programs.
