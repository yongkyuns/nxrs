# NXRS examples

NXRS includes a navigation application for learning the service model and an IoT camera application that combines capture, detection, communication and client integration.

## Navigation and service composition

The [event-demo application](../../app/event-demo/src/main.rs) connects IMU and GNSS services to a fusion service and observes the resulting state. It demonstrates typed messages, control commands, bounded waits and explicit shutdown.

The [getting-started guide](getting-started.md) runs it with the `host-demo` profile and synthetic inputs. The app selects a logical IMU through `devices::primary_imu()`; the selected profile supplies its implementation.

Read [Your first application](first-application.md) alongside the source. The [event-driven reference](../../docs/event-driven-demo.md) describes the execution model and test results.

<span id="experimental-iot-camera"></span>

## IoT camera

The camera application combines frame acquisition, detection, recording/streaming, gateway delivery and client-visible state. It uses bounded buffering, explicit resource ownership, failure recovery and stale-data handling.

The [camera detection delivery reference](../../docs/camera-detection-delivery.md) follows observations from the camera through the gateway to the iOS view. It describes authenticated publication and viewing, bounded message formats, expiration and cancellation. Detection state and video progress are tracked separately so stale observations are cleared even while a video view remains open.

The [VWW baseline evaluation](../../docs/camera-detection-qualification.md) records streaming performance, detection accuracy, memory use and recovery tests for the tested model and board. It also links to subsequent detector work. The baseline has documented missed detections; use the model-specific results when selecting a configuration. Remote alert delivery and deployment requirements are described in the application guides and [security documentation](../../docs/security.md).

## Tests and evaluations

The applications are tested on the host, in emulation and on supported hardware. Separate benchmarks measure channel performance, event delivery and memory use.

See [Testing and debugging](testing.md), the [repository test index](../../tests/README.md), and [Performance and memory](performance.md) for the test procedures and results. Additional examples are documented as they are added.
