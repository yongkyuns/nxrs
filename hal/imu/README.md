# IMU

`open(sink)` creates an owned acquisition session; services receive typed
`Event::Sample` messages and never read the bus. Call `request_stop()` and `join()`
to wake acquisition, disable the sensor/IRQ and reclaim its worker. Drop requests
cancellation without a hidden join; the worker retains resources through cleanup.

The portable BMI270 adapter uses the pinned `bmi2` driver over `embedded-hal` I2C.
It waits for both sensors to finish startup, then enables acquisition with equal 100 Hz ODR,
a four-frame FIFO watermark, headerless combined
gyro/accel frames and INT1 watermark/full/error notifications. FIFO format is
verified before admission. The incomplete upstream FIFO reader is not used;
Nxrs drains bounded, frame-aligned reads through the same I2C trait. Full FIFO or
malformed framing terminates acquisition rather than hiding an unknown gap.

Samples carry SI units, sequence, host reception time and nominal sample interval.
Batch delivery time is not a measured hardware sample timestamp. Queue-full
rejections are counted by `dropped()`; terminal errors remain observable through
`join()` and are retried as a bounded single pending fault event.

The NuttX provider owns the I2C File and a platform-registered GPIO descriptor.
NuttX handles interrupt notification; Rust channels are only used in task context.
No timer-polling fallback is provided. M135 v1.0 leaves the interrupt unwired;
see [CoreS3 wiring limitations](../../docs/m5stack-cores3.md).
