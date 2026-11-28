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

## Optional time and position aiding

An active session can transfer one `AidingHandle` through `Session::aiding()`.
The portable handle reports supported hint types and admits at most one request,
including its retained completion. Receivers without this capability return
`None`; ordinary acquisition is unchanged. Navigation exposes the same handle
through `ObservationService::take_gnss_aiding()` and `GnssHandle::take_aiding()`.
Applications and services use these APIs without selecting a chip protocol.

The first implementation accepts two inputs in `gnss::aiding`:

| Input | Required interpretation |
| --- | --- |
| `TimeAid` | Unix UTC milliseconds at `observed_at_us`, an error bound including source and receipt error, maximum age, and a clock-drift bound in ppm. |
| `PositionAid` | WGS84 latitude/longitude and **ellipsoid** altitude, a conservative 3D standard deviation in metres, maximum age, and a maximum displacement speed including vertical motion. |

Stamp observations with `gnss::monotonic_us()` in the current boot. Do not reuse
these stamps across a reboot or an unaccounted suspend. Time advances from that
observation and its uncertainty grows with drift. Position uncertainty grows
with elapsed time and the supplied speed bound; zero speed explicitly asserts
stationary operation through the allowed age. Provider confidence radii need a
documented conversion to the position contract. The existing 2D `GnssFix` is
insufficient to supply ellipsoid altitude; do not silently substitute zero.

The caller supplies validated estimates. This phase does not acquire network
time, infer a position, persist hints, or add another clock service. Source
selection, provenance, and independence belong to the later aiding coordinator.
In particular, delivery of a hint is not a new independent time or position
observation, and `Event::AidingReady` does not refresh navigation measurements.

```rust,ignore
use gnss::aiding::{Aid, AidingHandle, RequestId, SubmitError, TimeAid};

fn seed_time(
    session: &mut impl gnss::Session,
    sample: TimeAid, // Supplied by a validated source with an honest error bound.
) -> Result<(AidingHandle, RequestId), SubmitError> {
    let mut aiding = session.aiding().ok_or(SubmitError::Unsupported)?;
    let id = aiding.try_submit(Aid::Time(sample))?;
    Ok((aiding, id))
}

// Keep the handle. Reconcile it on AidingReady, other wakes, and a bounded
// caller deadline. Reading a completion frees the slot for the next hint.
if let Some(completion) = aiding.try_completion() {
    // Match completion.id and handle completion.result in the aiding owner.
}
```

`try_submit` never waits for slot capacity. `Busy` means no admission; a request
that was admitted keeps its result until `try_completion` consumes it. Invalid,
expired, or unsupported inputs are rejected explicitly. A stop closes admission
and retires unfinished work; a handle from an old session cannot submit into a
replacement session. Dropping the handle alone does not cancel an admitted
request. Session stop/join remains the lifecycle boundary.

`AidingReady` is a best-effort notification through the existing bounded sink;
the completion itself is retained outside that queue. A coordinator must include
a bounded pending-request deadline in its existing wait and reconcile before
waiting again. This also covers callbacks that filter nonmeasurement events.
Optional rejection, expiry, or delivery timeout leaves acquisition running.
An actual UART failure or a truncated outbound frame can make the shared stream
unsafe and therefore follows the existing terminal-fault/quarantine path.

The M9 driver encodes `UBX-MGA-INI-TIME_UTC` and `UBX-MGA-INI-POS_LLH` into a
fixed 32-byte command buffer. Its existing UART owner advances nonblocking writes while
continuing NAV and PPS parsing. It checks freshness again at encoding, accounts
for clock drift, motion, encoding precision, and a conservative delivery window,
including earlier queued writes, and freezes a frame after its first byte is
written. The model assumes sole ownership of an unthrottled 8N1 UART at the
configured actual baud. The one-second transaction
budget deliberately makes this **coarse initialization**, not precision time
transfer. A physical UART timing qualification is still required to validate the
assumed transmit scheduling and serialization bound.

MGA replies identify a message subtype, not a unique submission. This first M9
implementation therefore reports a complete write as `Outcome::Unconfirmed` and
does not enable or depend on MGA acknowledgements. It cannot falsely confirm a
new seed using a delayed reply from an older session. The generic result contract
also permits `Accepted`, `Rejected`, `TimedOut`, and `Cancelled` for adapters that
can establish those outcomes. None implies a TTFF or navigation-accuracy gain.

There is no additional worker, timer thread, or allocation per request. The
session uses its existing cancellation pipe for commands and adds one bounded
mailbox with a retained result. Write readiness is requested only while a frame
needs transmission; the normal wait also observes the transaction deadline.
The shared wake and mailbox allocate bounded state at session setup even if
the caller does not use aiding. These heap objects are separate from linked
flash/static-RAM size. Old handles retain completion memory, not pipe descriptors,
after the session is dropped.

Follow-up scope is tracked in [GNSS aiding #491](https://github.com/yongkyuns/nxrs/issues/491):
generic connectivity providers and cache policy, satellite-data delivery, then
receiver prediction/retention. Clock-source integration must coordinate with
[time synchronization #490](https://github.com/yongkyuns/nxrs/issues/490).
