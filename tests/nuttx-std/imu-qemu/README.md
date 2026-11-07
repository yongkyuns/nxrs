# ESP32-S3 QEMU BMI270 qualification

This fixture runs the production NuttX IMU provider and Rust BMI270 driver.
The qualification profile binds the driver to a test I²C bus and GPIO pin
registered through the NuttX upper halves. The model accepts the initialization
upload, stores configuration registers, supplies a headerless FIFO, and raises
a synthetic INT1 notification after the worker registers a GPIO poll waiter.

On Linux x86-64 with the [Nxrs QEMU build dependencies](../../../docs/nuttx-qemu.md):

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-qemu/Cargo.toml \
  --profile esp32s3-qemu-bmi270 \
  --target-c-source tests/nuttx-std/imu-qemu/bmi270_lower.c \
  --out target/imu-qemu
source target/qemu-tools/environment.sh
python3 tests/nuttx-std/imu-qemu/run.py target/imu-qemu
```

The Rust fixture verifies six distinct samples spanning two FIFO reads, including
units, cadence, and sequence. It joins acquisition and checks device release,
then verifies a FIFO-full fault is delivered and retained by join. A fresh
acquisition after the fault verifies recovery and produces another six samples.
The runner checks NuttX shell identity, the exact pass report, and the command's
exit status.

The emulation covers the driver's configured BMI270 register subset and the
NuttX descriptor, ioctl, and poll paths. GPIO electrical behavior, the MCU's
physical I²C controller, and hardware timing require the separate bench matrix.
