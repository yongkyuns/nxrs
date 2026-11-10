# IMU

This HAL manages acquisition over `peripheral::imu::Imu`. It has no production
sensor-driver or I²C/SPI adapter dependency. Concrete selection and resource
construction live in the [platform device catalogue](../../platform/devices/imu/README.md).

Applications select a named device and pass it to a service, or use the HAL API:

```rust,ignore
use imu::{Device, Session};
let device = imu_devices::primary_imu();
let mut session = device.open_default(sink)?;
session.request_stop()?;
session.join()?;
```

`Device` describes permanent identity; `Session` describes exclusive active use.
`ImuService::start_on(device)` accepts the selected reference. Services do not
look up profiles or choose concrete devices. Unbound acquisition returns
`Unsupported`; mocks and coordinated simulation require explicit profiles.

Drivers resolve `Config` into supported settings and SI measurements. The HAL
adds sequence numbers, host reception timestamps and nominal sample intervals.
`device.resolve(config)` previews accepted settings without I/O or reservation.
`open(config, sink)` resolves before opening resources and returns the session
with `AppliedConfig`. Invalid requirements fail before I/O; unsupported settings
return `Unsupported`. The current acquisition loop admits FIFO devices only.

Hardware initializes on acquisition. A canonical static `construction::Instance`
contains the device state and a mutex-protected acquisition slot, including the
concrete driver while active. Platform wiring supplies resource-opening and
sensor-initialization functions; the HAL controls their order. A missing bus can
release the device normally, while uncertain sensor initialization leaves it
faulted. One private `Device::open` operation validates, reserves and initializes
the acquisition owner before worker startup, without a separate factory module.
The construction API is reserved for platform catalogues and qualification;
architecture checks reject its use in apps and services.

A device stays Busy through final delivery, sink destruction and resource cleanup.
Failed cleanup leaves that device faulted until restart, without affecting other
devices. Drop requests cancellation; explicit `join()` establishes retirement.
The shared acquisition worker implements `Session` directly. Static device storage
is separate from thread stacks, cancellation state and OS allocations. Opening
uses typed function pointers; sample processing and ordinary sinks remain
statically dispatched, without boxing or a runtime device registry.

The HAL owns readiness and cancellation through NuttX GPIO and shared acquisition
helpers. A configured IRQ failure never falls back to a timer. An explicitly
unwired device uses a sampling-rate-derived periodic wake, which is only a hint:
the driver still checks its FIFO. Late wakes coalesce and empty FIFOs emit nothing.
NuttX handles interrupt entry; Rust message delivery runs in task context.
Resource packages own native ABI glue; this HAL contains no C/FFI implementation.

`probe` enables optional tracing. Ordinary builds use zero-sized tracing and
unwrapped resources, with no observer checks or timing reads. The catalogue's
`imu_devices::probe::open_configured` selects a physical device and activates an
observer. Shared timing events and bus wrappers live in `acquisition::probe`;
the catalogue reexports `Timing` for diagnostic callers. Sensor-reference setup remains
in catalogue diagnostics, with HAL cleanup retained on setup failure. Mock,
simulation, unbound and non-probe acquisition reject physical diagnostics.

Samples use SI units. Batch reception time is not a hardware sample timestamp.
Queue-full losses accumulate in `dropped()`; terminal errors remain observable
through `join()`, independently of delivery. Bus wait, scheduling and sink
backpressure affect observed latency separately from requested sampling rate.

Validation includes normalized-provider lifetime tests, actual driver/resource
witnesses and catalogue profile-switch tests. Host tests and target compilation
do not establish physical bus or interrupt timing. See the
[composition design](../../docs/peripheral-composition.md) and
[static-device contract](../../docs/static-device-composition.md).
