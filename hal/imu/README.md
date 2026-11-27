# IMU

This HAL manages acquisition over `peripheral::imu::Imu`. It has no production
sensor-driver or I²C/SPI adapter dependency. Concrete selection and resource
construction live in the [platform composition crate](../../platform/README.md).

Applications select a named device and pass it to a service, or use the HAL API:

```rust,ignore
use imu::{Device, Session};
let device = composition::imu::primary_imu();
let mut session = device.open_default(sink)?;
session.request_stop()?;
session.join()?;
```

`Device` describes permanent identity; `Session` describes exclusive active use.
`ImuService::start_on(device)` accepts the selected reference. Services do not
look up profiles or choose concrete devices. Unbound acquisition returns
`Unsupported`; mocks and coordinated simulation require explicit profiles.

Drivers resolve `Config` into supported settings and SI measurements. The HAL
supplies a shared monotonic clock and adds sequence numbers and nominal intervals.
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
devices. Drop requests cancellation; successful `join()` establishes retirement.
A failed join can report either a retired worker's terminal fault or a failed
cancellation wake while it still runs. Retain the session and retry if
`is_finished()` is false; a join error alone does not establish retirement.
The shared acquisition worker implements `Session` directly. Static device storage
is separate from thread stacks, cancellation state and OS allocations. Opening
uses typed function pointers; sample processing and ordinary sinks remain
statically dispatched, without boxing or a runtime device registry.

After `join`, `Session::failure()` exposes independent acquisition and cleanup
causes retained in the existing worker result, even if the terminal event was
rejected or cancellation ended its delivery. `Event::Failed` reports the original
acquisition cause when one exists. The legacy `join()` error still gives cleanup
failure precedence so callers do not overlook uncertain retirement/quarantine.
Read both fields before discarding the session when diagnosing a double failure.
Mock/simulation failures use the acquisition field. A provider implementing only
the legacy contract may return no detail. Startup preparation still returns one
error, and cleanup still selects one of sensor-stop/IRQ-detach errors; this small
terminal record does not claim a complete trace of every cleanup step.

The HAL owns readiness and cancellation through NuttX GPIO and shared acquisition
helpers. A configured IRQ failure never falls back to a timer. An explicitly
unwired device uses a sampling-rate-derived periodic wake, which is only a hint:
the driver still checks its FIFO. Late wakes coalesce and empty FIFOs emit nothing.
NuttX handles interrupt entry; Rust message delivery runs in task context.
Resource packages own native ABI glue; this HAL contains no C/FFI implementation.

A driver's `WouldBlock` means a coherent FIFO observation was unavailable and
no measurement was consumed or offered. The HAL defers that drain to the next
delivery interval and preserves cancellation, including an already acknowledged
IRQ watermark that stays high. Repeated ambiguity is bounded by the driver's
declared FIFO capacity divided by the applied rate. For BMI270, 170 complete
frames at 100 Hz give a nominal 1.7 s bound; after that interval the next wake
ends acquisition with `Timeout`. This is an upper bound for an incoherent
observation, not a freshness threshold or a lossless-retention guarantee. A
partly occupied FIFO may overflow earlier, and bus, overflow and continuity
errors remain terminal. Drivers without a declared capacity cannot defer.

If an empty FIFO snapshot is followed by an asserted IRQ, the HAL allows one
cancellable recheck after a delivery interval. This admits data that arrived
between the FIFO and GPIO reads. A second empty observation with the line still
asserted ends acquisition with `Io`. Neither path silently flushes a FIFO or
restarts a sequence; a new session is a distinct acquisition lifetime.

`probe` enables optional tracing. Ordinary builds use zero-sized tracing and
unwrapped resources, with no observer checks or timing reads. The catalogue's
`composition::probe::open_configured` selects a physical device and activates an
observer. Shared timing events and bus wrappers live in `acquisition::probe`;
the catalogue reexports `Timing` for diagnostic callers. Sensor-reference setup remains
in catalogue diagnostics, with HAL cleanup retained on setup failure. Mock,
simulation, unbound and non-probe acquisition reject physical diagnostics.

Samples use SI units and `timestamp_us` on the device's monotonic clock.
`received_us` records delivery on the same clock so a consumer can distinguish
capture age from recent publication. Mock and coordinated simulation use their
provider clock for both timestamps.
BMI270 uses its internal counter to estimate each FIFO frame's capture time.
Accuracy depends on host clock resolution, bus timing and sensor clock drift.
Other drivers can report reception time by leaving `is_capture_time` false.
Fusion uses consecutive capture times when available and the nominal interval
for reception-timed providers.
Queue-full losses accumulate in `dropped()`; terminal errors remain observable
through `join()`, independently of delivery. Bus wait, scheduling and sink
backpressure affect observed latency separately from requested sampling rate.

Validation includes normalized-provider lifetime tests, actual driver/resource
witnesses and catalogue profile-switch tests. Host tests and target compilation
do not establish physical bus or interrupt timing. See the
[composition design](../../docs/peripheral-composition.md) and
[static-device contract](../../docs/static-device-composition.md).
