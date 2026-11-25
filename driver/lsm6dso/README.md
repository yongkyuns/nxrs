# LSM6DSO driver

`nxrs-lsm6dso` is an original, synchronous Rust `std` register/FIFO driver over
`embedded-hal` 1.0 `I2c` or `SpiDevice` and caller-supplied `DelayNs`. The protocol choices are
based on ST datasheet DS12140 Rev 3 (2024), and FIFO behavior also on ST
application note AN5192 Rev 6. ST's documents are copyright STMicroelectronics;
all rights are reserved. This crate does not copy ST source code. The driver
source is distributed under the workspace MIT license.

[bindings.toml](bindings.toml) declares supported I²C addresses, SPI modes, bus
frequencies and readiness modes for profile validation. A product profile chooses the actual
bus and address or chip select; NuttX configures the physical pins. The current Nxrs binding
admits periodic acquisition only; the chip's interrupt capability is separate.

Supported paired ODRs are the exact silicon rates 12.5, 26, 52, 104, and 208
Hz. `Rate::SUPPORTED` enumerates the typed rates. `AccelRange::from_g()` and
`GyroRange::from_dps()` resolve exact physical ranges to chip-specific settings,
returning `None` for unsupported values. Defaults are
104 Hz, ±4 g, and ±2000 dps. FIFO watermark is four tagged
words (two accel/gyro pairs) in continuous mode; INT1 reports watermark, full,
and overrun. Compression, timestamps, temperature, sensor hub, and downsampling
are disabled. Samples use the nominal sensitivities from DS12140 Rev 3 and are
normalized to m/s² and rad/s. A pending odd FIFO record can pair with the next
drain. Device setup polls reset completion for at most 20 ms; a
failed setup makes bounded best-effort rollback to FIFO bypass and powered-down
sensors.

Sources: [DS12140 Rev 3](https://www.st.com/resource/en/datasheet/lsm6dso.pdf),
[AN5192 Rev 6](https://www.st.com/resource/en/application_note/an5192-lsm6dso-alwayson-3d-accelerometer-and-3d-gyroscope-stmicroelectronics.pdf).
Host bus-fake tests do not qualify a physical device or bus timing.

The driver directly implements `peripheral::imu::Imu` alongside its chip API.
The shared contract returns SI measurements and explicit FIFO/overflow semantics;
chip-specific samples retain their slot metadata. HAL IMU owns stream timing,
readiness, acquisition and resource lifetime.

`Lsm6dsoSpi::open_spi` uses the same initialization, FIFO and normalized-device
implementation. Its SPI reads have no extra dummy byte; each register operation
is one `SpiDevice` transaction. Host witnesses check this framing, normalized
FIFO samples, overflow and shutdown errors. Physical SPI remains unqualified.
