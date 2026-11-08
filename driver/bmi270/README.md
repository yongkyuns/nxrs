# BMI270 driver

`nxrs-bmi270` contains the synchronous BMI270 protocol over `embedded-hal`
`I2c` or `SpiDevice` and a caller-supplied `DelayNs` instance. It uses Rust `std` and owns chip
initialization, the exact configuration readback, bounded headerless FIFO
decode, sensor-specific errors and stop operations. `Sample` contains physical
SI values; reception time and delivery sequencing belong to the consuming
capability. Initialization and stop operations reuse the owned delay instance;
the driver requires no `Default` delay constructor.

[bindings.toml](bindings.toml) declares supported I²C addresses, SPI modes, bus
frequencies and readiness modes for profile validation. A product profile chooses the actual
bus, address or chip select, and interrupt endpoint; NuttX configures the physical pins.

The driver retains `bmi2` 0.1.2 and `embedded-hal` 1.0.0. Initialization loads
`bmi2::config::BMI270_CONFIG_FILE`, the Bosch BMI270 configuration blob bundled
with the pinned `bmi2` release. `Cargo.lock` retains the registry checksum for
that release. `Bmi270::open` retains the 100 Hz ODR, ±4 g acceleration and
±2000°/s gyro defaults. `open_configured` accepts the chip-specific `Config`
with rates of 25, 50, 100 or 200 Hz, accelerometer ranges of ±2/4/8/16 g, and
gyroscope ranges of ±125/250/500/1000/2000°/s. `Rate::SUPPORTED` enumerates the
typed rates and `Rate::milli_hz()` reports their exact values.
`AccelRange::from_g()` and `GyroRange::from_dps()` resolve exact physical ranges
to chip-specific settings, returning `None` for unsupported values.
Initialization verifies the
accepted accelerometer and gyroscope ODR and range fields by reading their
registers back. All supported gyro ODRs are at least 25 Hz. FIFO conversion
scales each raw axis using the chosen physical range. FIFO layout, watermark,
interrupt mapping and bounded sensor startup behavior are unchanged.
`SAMPLE_INTERVAL_US` remains the public 100 Hz default interval for existing
consumers; configured consumers can derive their interval from
`Config::rate`. The driver directly implements `peripheral::imu::Imu` alongside
its chip API. That contract resolves supported settings and returns synchronous
SI measurements with explicit FIFO buffering and overflow errors. HAL IMU adds
stream metadata and manages acquisition and resource lifetime.

`Bmi270Spi::open_spi` selects the same device implementation with chip-local SPI
framing. Reads include the required dummy byte; the first read after power-on
and soft reset selects SPI. Writes clear the read bit and leave the required
2 µs access gap. Register setup now uses one transport-independent sequence;
`bmi2` supplies its unchanged configuration image and register encodings, not
its 0.1.2 SPI transport (whose read/write framing is unsuitable here). The
initialization sequence was adapted from that dependency and the datasheet;
[its MIT notice](LICENSE-bmi2) is retained. Advanced
power save remains disabled during FIFO acquisition on both transports.
The sequence follows the [Bosch BMI270 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf).
Host tests check reset/reselection, the full uploaded image, FIFO units and
errors. Physical SPI timing and throughput remain unqualified.
