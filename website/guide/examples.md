# NXRS examples

These examples exercise different parts of NXRS. They are not a gallery of finished products. Start with the host example for the programming model; read the application-specific reports before choosing a hardware configuration.

## Navigation and service composition

The [event-demo application](../../app/event-demo/src/main.rs) connects IMU and GNSS services to a fusion owner and observes the resulting state. It is useful for understanding typed delivery, control commands, bounded waits and explicit shutdown.

The [getting-started guide](getting-started.md) runs it with the explicit `host-demo` profile and synthetic inputs. This checks application behavior without opening physical sensors. The app selects a logical IMU through `devices::primary_imu()`; the selected profile supplies its implementation.

Read [Your first application](first-application.md) alongside the source. The [event-driven reference](../../docs/event-driven-demo.md) describes the execution model and its validation. Synthetic input is not sensor accuracy or real-time hardware qualification.

## Experimental IoT camera

The camera work is an application of the same separation of concerns to a larger system: frame acquisition, detection, recording/streaming, gateway delivery and client-visible state. It provides a practical setting for resource ownership, bounded buffering, failure recovery and stale-data handling.

The [camera detection delivery reference](../../docs/camera-detection-delivery.md) follows observations from the camera application through the gateway to the iOS view. It explains authenticated publication and viewing, bounded message formats, expiration and cancellation. Detection state and video progress are tracked separately so old observations are not presented as current merely because a video view is still open.

The [VWW baseline qualification report](../../docs/camera-detection-qualification.md) records a particular model, board and camera configuration, including memory use and failure cases. It also points to subsequent model-specific work. Those results must not be generalized across resolutions, models or boards. In particular, the baseline report explicitly describes missed detections and does not establish a dependable intrusion detector.

This is a functional experimental camera workflow, not a production security-camera certification or a promise of remote alert delivery. Deployment and threat boundaries belong to the [security design and release requirements](../../docs/security.md) and the application-specific guides.

## Tests are not interchangeable examples

Host fixtures, mocked providers, emulator runs and hardware probes are useful for different questions. A workload designed to isolate channel overhead is not a complete application; an emulator pass is not a physical-device timing measurement.

For the validation paths, see [Testing and debugging](testing.md), the [repository test index](../../tests/README.md), and [Performance and memory](performance.md). More application examples will be documented as they are added, together with their configuration and evidence.
