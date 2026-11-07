# BMI270 driver

`nxrs-bmi270` contains the synchronous BMI270 protocol over `embedded-hal`
`I2c` and `DelayNs`. It is `no_std` and owns chip initialization, the exact
configuration readback, bounded headerless FIFO decode, sensor-specific errors
and stop operations. `Sample` contains physical SI values; reception time and
delivery sequencing belong to the consuming capability.

The driver retains `bmi2` 0.1.2 and `embedded-hal` 1.0.0. Initialization loads
`bmi2::config::BMI270_CONFIG_FILE`, the Bosch BMI270 configuration blob bundled
with the pinned `bmi2` release. `Cargo.lock` retains the registry checksum for
that release. The driver retains the 100 Hz ODR, ±4 g acceleration and
±2000°/s gyro defaults; the HAL maps driver errors/samples to the public IMU
contract.
