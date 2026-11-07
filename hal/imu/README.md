# IMU

`open(sink)` creates an owned acquisition session; services receive typed
`Event::Sample` messages and never read the bus. Call `request_stop()` and `join()`
to wake acquisition, disable the sensor/IRQ and reclaim its worker. Drop requests
cancellation without a hidden join; the worker retains resources through cleanup.

The [composition target](../../docs/peripheral-composition.md) moves reusable
BMI270 behavior into `driver/bmi270`, retains IMU normalization in this crate,
and separates NuttX resource acquisition from sensor construction. Follow
[steps S0–S8](../../docs/peripheral-composition-plan.md) for the first physical
slice. Today the NuttX factory directly constructs BMI270; the profile's driver
field validates that fixed choice. The capabilities/configuration API, device
graph, overlays and coordinated simulator described in that design are pending.

The portable BMI270 adapter uses the pinned `bmi2` driver over `embedded-hal` I2C.
It waits for both sensors to finish startup, then enables acquisition with equal 100 Hz ODR,
a one-frame FIFO watermark, headerless combined
gyro/accel frames and INT1 watermark/full/error notifications. FIFO format is
verified before admission. The incomplete upstream FIFO reader is not used;
Nxrs drains bounded, frame-aligned reads through the same I2C trait. Full FIFO or
malformed framing terminates acquisition rather than hiding an unknown gap.

Samples carry SI units, sequence, host reception time and nominal sample interval.
Batch delivery time is not a measured hardware sample timestamp. Queue-full
rejections are counted by `dropped()`; terminal errors remain observable through
`join()` and are retried as a bounded single pending fault event.

The NuttX provider owns the I2C File and an optional platform-registered GPIO descriptor.
NuttX handles interrupt notification; Rust channels are only used in task context.
A configured IRQ uses its GPIO descriptor. With no IRQ resource, the provider
explicitly selects an ODR-derived timer: 100 Hz gives a 10 ms wake period for
fusion prediction without intentional batching. Both sources invoke the same
FIFO drain and event sink. Empty FIFO reads
emit nothing; missed ticks coalesce. A configured IRQ failure never changes modes.
The same cancellation pipe wakes either wait without another thread or queue.
OS scheduling and the unsynchronized sensor clock can delay delivery; late wakes
drain all available frames without fabricating samples or losing their 10 ms interval.
M135 v1.0 uses this timer path because its interrupts are unwired; see
[CoreS3 acquisition](../../docs/m5stack-cores3.md).
