# Navigation telemetry service

`nxrs-telemetry-service` owns bounded sensor capture, its versioned wire formats,
latest-value publication, paired BLE delivery and collector TLS recovery. Its
public Rust crate is `telemetry`. Motion Tracker supplies product configuration
and connects sensor callbacks and observation readers to this service.

The navigation telemetry protocol has two IMU channels and one GNSS channel.
Those are protocol slots, independent of a board or concrete sensor model.
Acquisition capabilities supply normalized measurements and source identities.
An unused slot has no live sample; snapshots represent it as `Waiting` without
coordinates. The three export versions and existing browser/collector formats
retain their byte layout and source identifiers.

## Compose a service

Before starting acquisition, construct `Capture` from `CaptureConfig`. Supply the
resolved IMU rates, expected GNSS rate, lossless-count scales when available,
the time and burst that the pool must cover, and the requested-memory ceiling.
`reserve_export: false` keeps clock/fault publication without allocating the
full-rate export pool. The application decides whether later pairing requires a
reserved pool.

The constructor returns one exclusive consumer and three exclusive callback
publishers in IMU 0, IMU 1, GNSS order. Connect each publisher directly to its
acquisition callback. Keep the consumer with one `Telemetry` value containing
its independent `ObservationReader<2>` and summary interval. Pass an
`Option<Telemetry>` to `Communications::start` together with `Config` and a bounded
notification callback.

`Config` supplies both BLE service UUID triples, initial pairing intent, cellular
APN/collector settings, an optional pinned map relay, and diagnostic/task names.
The application decides whether a network may carry maps; supplying a relay
explicitly admits that route. Firmware products keep their device selection,
CLI options, credentials, metered-data policy and screen workflows outside the
service.

Both report intervals must be greater than zero and no longer than one day
(`MAX_REPORT_INTERVAL`). Admission rejects invalid or unrepresentable intervals
before transferring the telemetry endpoint or opening a transport.

`pair`, `view` and `map` are narrow control/state methods. Notifications only
announce changes; retained state remains available if the application's event
inbox is full. `stop` wakes and joins the worker. After joining,
`take_telemetry` returns its exclusive endpoints for a later start. Failed
thread creation restores the caller's original value. Initialization failures
and unwinding close capture admission before returning ownership.
After joining, pairing commands are harmless, map exchange returns `None`, and
the retained link view reports `Unavailable`.

## Execution and resource ownership

The same communications worker steps BLE, collector TLS and map transfers under
one TLS identity lease. It retains one logical wait point and the existing
23 KiB stack reservation. Blocking TCP connection attempts remain on the
existing collector and map-dial workers, with 4 KiB and 8 KiB stacks. No worker
is created merely because the code is packaged as a service.

The fixed 512-slot capture pool is shared across sources. Each source preserves
its own order; independent sources can interleave. A paused producer cannot
block other sources from consuming remaining capacity. Overflow retains the
first missing identity and closes admission; the owner drains published samples
and reports an incomplete export. Worker shutdown does not wait for a paused
callback: pending retirement is resumed by the next joined owner. Full product
shutdown must still join acquisition before retiring its endpoints.

`CaptureBudget` validates expected traffic and limits the requested allocation;
it does not change physical pool capacity or discover hardware rates. Actual
overload still closes export. Pool allocator overhead, clock/wake state, TLS
arenas, map storage and task stacks have separate budgets.

The generic `publication::Publisher<Sample, Frame>` preserves partially written
frames and coalesces only waiting samples. It runs synchronously in the existing
owner. Collector intervals remain spent after partial or failed writes, so
reconnection does not reset the application's data allowance.

## Protocol-only callers

`wire`, `export`, `Snapshot`, `ClockAnchor` and `publication` are public. A caller
can encode snapshots or drain capture directly without starting the
communications owner. `ClockAnchor::new` rejects an unrepresentable mapping;
`utc_ms` rejects observations earlier than the anchor or arithmetic overflow.
Map wire types and viewport/cache state are owned by `nxrs-map-service`.

The public integration test exercises an IMU-only consumer with its own traffic
budget and checks the stable export/JSON contract. Unit tests retain partial
writes, overflow, ownership recovery, callback pause and retirement controls.
Host tests do not establish native mutex/atomic progress, NuttX ABI, target stack
headroom or physical throughput; those remain product qualification gates.
