# GNSS capability

`composition::gnss::primary_gnss().open(sink)` returns a `gnss::Session`: stop, join, completion and drop accounting for
active acquisition. Receiver protocol operations remain in the UBX driver.

`nxrs-gnss` owns acquisition, resource lifetime and the hardware-independent
fix API. `driver/peripheral::gnss::Receiver` defines bounded synchronous
configuration, restoration and measurement operations. `driver/ubx` implements
it. Services receive WGS84 coordinates, speed in m/s, and optional course in
radians. `timestamp_us` uses the same monotonic clock as IMU samples.
`is_capture_time` identifies PPS-aligned fixes. Other fixes use reception time.

`GnssFix::quality` reports satellites used, estimated horizontal and vertical
accuracy in metres, speed accuracy in m/s, and optional position DOP (PDOP).
The optional satellite summary contains the receiver's visible-or-tracked count,
the count with synchronized code or carrier lock, and mean C/N₀ for tracked
satellites with a signal reading, rounded to whole dB-Hz. `age_ms` gives the
report's age relative to the fix epoch, up to one configured NAV-SAT interval.
`Event::Satellites` delivers the latest report independently of fixes. It
includes constellation, satellite ID, and C/N₀ for up to 24 strongest
measured signals. The report's aggregate counts cover the full receiver list.

The NuttX provider owns a nonblocking UART. The CoreS3 profile declares a UART
resource and an M9 receiver. The [driver binding](../../driver/ubx/bindings.toml)
declares supported receiver/resource combinations. The composition crate in `platform/`
constructs the concrete receiver and exports named `&'static impl gnss::Device`
accessors. The HAL has no production UBX dependency. Each configured receiver
has its own static storage and ownership/fault state; aliases share that object.
Services receive the selected device through `GnssService::start_on`.

The reusable `driver/ubx` crate runs M9 protocol transactions over an injected
byte stream. It uses `kinavis-ubx` for NAV-PVT decoding and configuration codecs.
It checks the wire checksum and summarizes the variable-length NAV-SAT report
one satellite at a time, using fixed storage. The HAL suppresses repeated fix
epochs. [kinavis-ubx](https://github.com/KINAVIS/kinavis)

Acquisition enables UART1 NAV-PVT every navigation epoch **in RAM only**. It
spaces NAV-SAT reports at least three seconds apart according to the UART
budget. A constrained link may supply only NAV-PVT quality fields. Acquisition
checks acknowledgements and rate readback, then restores the saved rates at
release or after failed setup. Configuration
and restoration each have a one-second deadline. Uncertain restoration
quarantines the receiver until reboot. UART1 needs UBX input and output enabled.

A HAL-owned worker reads bounded UART chunks after descriptor readiness. IMU and
GNSS share the NuttX poll/pipe binding in `nxrs-acquisition`. The GNSS provider
owns UART termios, receiver lifecycle and restoration.

Reference: [M9 SPG4.04 interface description, UBX-21022436 R02](https://content.u-blox.com/sites/default/files/u-blox-M9-SPG-4.04_InterfaceDescription_UBX-21022436.pdf).

## Event-driven session

`Device::open(sink)` starts HAL-owned UART acquisition and delivers each normalized fix
through a bounded task-context sink. Services do not poll the receiver. The
worker waits on UART readiness and a cancellation pipe, parses fragmented input,
and never waits for measurement queue capacity. Full rejections are counted by
`dropped()`; terminal faults are retained in `join()` and retried with one pending
event. `request_stop()` wakes the wait; explicitly `join()` before treating the
resource as released. Drop requests cancellation without a hidden join.

Product builds derive the catalogue's additive `ubx` Cargo feature from the
profile. Direct Cargo qualification must enable it explicitly. Unbound builds
keep the named primary accessor and return `Unsupported` on acquisition.
