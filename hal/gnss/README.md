# GNSS capability

`composition::gnss::primary_gnss().open(sink)` returns a `gnss::Session`: stop, join, completion and drop accounting for
active acquisition. Receiver protocol operations remain in the UBX driver.

`nxrs-gnss` owns acquisition, resource lifetime and the hardware-independent
fix API. `driver/peripheral::gnss::Receiver` defines bounded synchronous
configuration, restoration and measurement operations; `driver/ubx` implements it. Services receive WGS84 latitude/longitude, speed in m/s and optional
movement course in radians. `timestamp_ms` is the owner’s monotonic reception
time, not UTC or measured satellite-to-host latency. Navigation owns projection
into local north/east coordinates.

The NuttX provider owns a nonblocking UART. The CoreS3 profile declares a UART
resource and an M9 receiver. The [driver binding](../../driver/ubx/bindings.toml)
declares supported receiver/resource combinations. The composition crate in `platform/`
constructs the concrete receiver and exports named `&'static impl gnss::Device`
accessors. The HAL has no production UBX dependency. Each configured receiver
has its own static storage and ownership/fault state; aliases share that object.
Services receive the selected device through `GnssService::start_on`.

The reusable `driver/ubx` crate runs M9 protocol transactions over an injected
byte stream. It uses [`kinavis-ubx`](https://github.com/KINAVIS/kinavis) 0.1.1
for framing, checksums and CFG-VALGET/VALSET. The driver validates wire domains
before the codec wraps longitude and course. It admits 92-byte NAV-PVT reports,
configuration acknowledgements and the requested configuration response.
The HAL suppresses repeated epochs and timestamps accepted fixes at reception.

Acquisition reads the existing UART1 NAV-PVT output rate, sets it to one message
per navigation epoch **in RAM only**, waits for ACK and verifies by readback.
Release/failed acquisition restores that rate with readback before serial mode,
descriptor and ownership release. Configuration and restoration each have a one-second
deadline; uncertain restoration quarantines only that receiver until reboot. Baud,
protocol enables, navigation rate, BBR and flash settings are preserved. UART1
must already accept/output UBX (the receiver’s default configuration).

A HAL-owned worker reads bounded UART chunks after descriptor readiness. IMU and
GNSS share the NuttX poll/pipe binding in `nxrs-acquisition`; UART termios controls,
receiver policy and restoration remain here. Protocol buffers remain fixed-size. Host fixtures and native builds qualify software
behavior/linking; physical receiver firmware, power, UART timing, RF, accuracy
and peak task-stack usage remain unqualified.

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
