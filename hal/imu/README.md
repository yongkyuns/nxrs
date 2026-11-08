# IMU

This HAL and its BMI270/LSM6DSO drivers use Rust `std`, as do their NuttX and
host consumers. There is no bare-metal `no_std` variant. The optional `probe`
feature selects instrumentation at one tracing-module boundary.
`lib.rs` exposes the API; each selected provider includes the shared helpers it
uses. Shared implementations stay in ordinary modules outside `platform/`.

`open(sink)` creates an owned acquisition session; services receive typed
`Event::Sample` messages and never read the bus. Call `request_stop()` and `join()`
to wake acquisition, disable the sensor/IRQ and reclaim its worker. Drop requests
cancellation without a hidden join; the worker retains resources through cleanup.

Without `probe`, tracing is zero-sized and its hooks perform no timing reads,
observer checks or callbacks. The diagnostic API remains callable, but returns
`Unsupported` before resource acquisition. Build with `--features probe` and call
`probe::open_configured(config, sink, observe)` to receive task-context wake,
I²C and FIFO-drain events. The observer must not block; ownership lasts through
cleanup. Mock, simulation and unbound providers return `Unsupported` for this
physical timing API. Sensor-reference output also requires the explicit
`probe::open_sensor_reference_configured` call. Enabling the feature alone does
not start recording; ordinary acquisition in that build uses an empty observer.

The [composition design](../../docs/peripheral-composition.md) places reusable
BMI270 protocol behavior in `driver/bmi270`. BMI270 and LSM6DSO directly implement
`peripheral::imu::Imu` from `driver/peripheral`, retaining their chip APIs. This HAL
adds stream metadata and owns acquisition lifetime; it has no normalization
wrapper objects. Drivers own supported rates and exact physical-range conversions.
The HAL asks the selected driver to resolve a request into concrete configuration
and accepted settings before reserving resources.
The product profile supplies the device, bus/address, optional IRQ and worker
budget. [bindings.toml](bindings.toml) declares supported sensor combinations and
their checked-in Rust implementation IDs. Shared build support generates typed
resource values and one implementation import; there is no model enum or Rust
field-projection schema. The generic build adapter retains provider selection,
freshness and native linking.

The shared [NuttX I²C adapter](../../driver/i2c/README.md) owns the controller descriptor
and implements `embedded_hal::i2c::I2c`. This HAL only wraps transactions for its
optional timing observer, then supplies that bus to the sensor driver.
`driver/gpio` owns the GPIO interrupt descriptor and registration/rollback, and
`nxrs-acquisition` supplies the shared NuttX poll/pipe binding. This HAL keeps
interrupt/timer selection, cadence, stream metadata and acquisition lifetime.
GPIO cleanup failures notify its quarantine policy even during setup or Drop. Buffering is
explicit in the shared device contract; this acquisition path requires a FIFO
and rejects latest-value devices before acquiring resources.
Composition overlays and coordinated simulation are tracked in the
[migration plan](../../docs/peripheral-composition-plan.md).

`resolve(config)` previews the selected provider's accepted settings without
I/O or resource acquisition. It does not verify hardware availability or reserve
the device. Use `open_configured(config, sink)` to request exact full scales, inclusive rate
limits in millihertz and a maximum nominal interval between delivery
opportunities. The HAL selects the slowest supported rate that satisfies both
timing requirements and returns the session with its applied settings. For
example, 25–100 Hz with a 20 ms delivery interval limit selects 50 Hz on BMI270.
The delivery interval is the sample period rounded up to a microsecond for
the one-sample FIFO watermark or periodic wake. Bus wait, scheduling and sink
backpressure affect measured latency separately.

API migration: `Config` and `AppliedConfig` use `accel_range_g: u16` and
`gyro_range_dps: u16` instead of HAL range enums. `resolve(config)` replaces
`capabilities()` and `Capabilities::select`; `Capabilities`, `AccelRange`
and `GyroRange` are removed from the HAL. The drivers retain their
chip-specific enums. Supported-mode enumeration is not part of this API.

The BMI270 provider accepts 25, 50, 100 or 200 Hz at any combination of
accelerometer ±2, ±4, ±8 or ±16 g and gyroscope ±125, ±250, ±500, ±1000 or
±2000 degrees/s. `open(sink)` uses 100 Hz, ±4 g and ±2000 degrees/s.
The mock offers that default mode. The unbound build returns `Unsupported`.
A zero rate, full scale or delivery interval and inverted
rate limits return `InvalidData` on configured providers. Well-formed
requirements without a matching mode return `Unsupported` before resource
acquisition. A configuration readback mismatch returns `InvalidData` and
quarantines the physical owner.

The `nxrs-bmi270` package exposes sensor-specific errors and physical
samples. See the [driver provenance and protocol notes](../../driver/bmi270/README.md).
This HAL consumes the driver's shared device contract and produces `DeviceError`
and `ImuSample`, assigning host reception time, sequence and nominal interval.

The driver uses the `embedded-hal` I2C and delay traits.
It waits for both sensors to finish startup, then enables acquisition with equal ODR,
a one-frame FIFO watermark, headerless combined
gyro/accel frames and INT1 watermark/full/error notifications. FIFO format is
verified before admission. The incomplete upstream FIFO reader is not used;
Nxrs drains bounded, frame-aligned reads through the same I2C trait. Full FIFO or
malformed framing terminates acquisition rather than hiding an unknown gap.

Samples carry SI units, sequence, host reception time and nominal sample interval.
Batch delivery time is not a measured hardware sample timestamp. Queue-full
rejections are counted by `dropped()`; terminal errors remain observable through
`join()` and are retried as a bounded single pending fault event.

The NuttX provider owns the I²C and optional GPIO resource adapters through its
acquisition session. Their native glue lives in the resource packages; IMU has
no private C/FFI code.
Its I2C adapter admits one read, one write or a repeated-start write/read pair,
with at most 64 bytes in each direction. Other operation sequences fail without
bus I/O. The host bridge witness checks message layout and cancellation using
fake target headers. The CoreS3 cross-build checks the actual target headers and
linkage. Neither measures physical bus or interrupt timing.
NuttX handles interrupt notification; Rust channels are only used in task context.
A configured IRQ uses its GPIO descriptor. With no IRQ resource, the provider
explicitly selects an ODR-derived timer: 100 Hz gives a 10 ms wake period for
fusion prediction without intentional batching. Both sources invoke the same
FIFO drain and event sink. Empty FIFO reads
emit nothing; missed ticks coalesce. A configured IRQ failure never changes modes.
The same cancellation pipe wakes either wait without another thread or queue.
OS scheduling and the unsynchronized sensor clock can delay delivery; late wakes
drain all available frames and retain the nominal interval of the selected mode.
M135 v1.0 uses this timer path because its interrupts are unwired; see
[CoreS3 acquisition](../../docs/m5stack-cores3.md).
