# Navigation telemetry service

`nxrs-telemetry-service` provides a fixed navigation telemetry protocol, bounded
sensor capture and synchronous delivery operations. Its public Rust crate is
`telemetry`. Applications own observation topology, workers, network selection
and reconnect policy. Motion Tracker composes these operations in its
[communications module](../../app/motion-tracker/src/communications.rs).

The navigation telemetry protocol has two IMU channels and one GNSS channel.
Those are protocol slots, independent of a board or concrete sensor model.
Acquisition capabilities supply normalized measurements and source identities.
`Snapshot::from` accepts navigation snapshots with one or two IMUs. A one-IMU
producer keeps its natural `ObservationReader<1>`; conversion fills the second
wire slot with `Waiting`. Absent GNSS also becomes `Waiting`, without coordinates.
No source is truncated. The three export versions and existing browser/collector
formats retain their byte layout and source identifiers. More sources require a
deliberate protocol extension.

## Choose delivery operations

The default feature set is empty. Core APIs include `wire`, `export`, `Capture`,
`Snapshot`, `ClockAnchor`, `publication` and `collector::Reports`. They compile
without BLE, TLS, cellular or execution runtimes. The portable map codec remains
a dependency because the public wire APIs use `maps::Key`.

| Feature | Additional API | Resources supplied by the caller |
| --- | --- | --- |
| `ble` | `radio::Output`, `BleServices`, `BleStatus`, `BleView` | BLE listener, TLS identity, capture and service turns |
| `collector-tls` | `collector::Upload` | Connected TCP socket, certificate pin, TLS identity and service turns |
| `probe` | Capture diagnostics | Explicit qualification controls |

`ble` and `collector-tls` are independent software features. `probe` is for
diagnostics; normal consumers do not need it. Neither transport operation opens
a modem, selects a route or creates a worker. `radio::pair` is a convenience for
opening the caller-configured BLE services; `radio::Output::start` accepts an
existing listener.

## Compose a service

Before starting acquisition, construct `Capture` from `CaptureConfig`. Supply the
resolved IMU rates, expected GNSS rate, lossless-count scales when available,
the time and burst that the pool must cover, and the requested-memory ceiling.
`reserve_export: false` keeps clock/fault publication without allocating the
full-rate export pool. The application decides whether later pairing requires a
reserved pool.

The constructor returns one exclusive consumer and three exclusive callback
publishers in IMU 0, IMU 1, GNSS order. Connect configured sources directly to
their acquisition callbacks. Keep the consumer in the owner that drains export.
Encoding snapshots or sending summaries does not require a capture pool.

For summaries, construct `collector::Reports` with a report interval and the
current `Instant`. Call `service` with a nonblocking `Write` implementation,
the current time and a closure that produces a `Snapshot`. It samples only when
a new report is due and performs at most one write and one flush per call.
Intervals must be greater than zero and no longer than one day
(`collector::MAX_REPORT_INTERVAL`); construction also rejects an unrepresentable
deadline. The first report is due immediately, and missed intervals do not
accumulate a burst.

A partial or failed report spends its interval. After a write error, discard
that stream and call `reset_stream` before supplying its replacement. The next
stream starts a complete frame when the retained allowance permits it; an old
frame's suffix is never replayed. `next_report` gives the next sampling deadline,
while `in_flight` tells the caller whether pending bytes or a flush need earlier
service. A frame times out after three seconds.

`collector::Upload::open` takes a caller-connected TCP socket, a certificate pin
and a TLS identity. Address selection and dialing belong to the caller. Keep
`Reports` across connection replacement to preserve the allowance.
The BLE operation instead accepts summaries through `radio::Output::offer`;
the application decides their cadence. `offer` reports whether the snapshot
was accepted and calls its snapshot closure only after queue admission, without
replacing pending frame bytes. BLE export batching and
session deadlines remain part of the protocol operation; there is no hidden
BLE summary timer.

## Execution and resource ownership

All delivery operations run synchronously in the caller's owner. The caller
keeps each TLS identity alive until its operations are dropped, chooses wake
deadlines and joins acquisition before retiring its endpoints. Motion Tracker's
[ownership and stack budgets](../../app/motion-tracker/README.md#device-composition)
belong to that product.

The fixed 512-slot capture pool is shared across sources. Each source preserves
its own order; independent sources can interleave. A paused producer cannot
block other sources from consuming remaining capacity. Overflow retains the
first missing identity and closes admission; the owner drains published samples
and reports an incomplete export. Closing capture does not wait for a paused
callback: the owner continues bounded retirement before admitting a new export.

`CaptureBudget` validates expected traffic and limits the requested allocation;
it does not change physical pool capacity or discover hardware rates. Actual
overload still closes export. Pool allocator overhead, clock/wake state, TLS
arenas, map storage and task stacks have separate budgets.

The generic `publication::Publisher<Sample, Frame>` preserves partially written
frames and coalesces only waiting samples. It retains one frame and one waiting
sample, without creating an execution owner.

## Protocol-only callers

Callers can encode snapshots, drain capture or drive `collector::Reports`
directly. `ClockAnchor::new` rejects an unrepresentable mapping;
`utc_ms` rejects observations earlier than the anchor or arithmetic overflow.
Map wire types and viewport/cache state are owned by `nxrs-map-service`.

The public integration tests use an actual one-IMU observation reader and check
the stable export/JSON contract, partial writes, flush backpressure, failed
stream replacement and report cadence. The
[independent consumer](../../tests/host/fixtures/telemetry-consumer/README.md)
uses renamed dependencies and its own lockfile with default features disabled.
Its host check exercises a caller-owned stream and rejects compiled BLE,
cellular, TLS or execution runtimes. These checks establish public composition
and stream behavior. Native mutex/atomic progress, NuttX ABI, target stack
headroom, firmware footprint and physical throughput remain separate
qualification gates.
