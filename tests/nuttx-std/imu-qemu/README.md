# ESP32-S3 DevKit QEMU IMU qualification

The `esp32s3-devkit-imu-qemu` qualification profile runs the ESP32-S3 DevKit
NuttX BSP with the production IMU HAL and Rust BMI270 driver. The fixture
registers an emulated I²C bus and INT1 pin through the NuttX upper halves.
The BMI270 model supplies initialization registers, FIFO data, and the 24-bit
sensor clock. It raises the synthetic interrupt after the acquisition worker
registers its GPIO poll waiter.

On Linux x86-64 with the [Nxrs QEMU prerequisites](../../../docs/nuttx-qemu.md):

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
cargo +1.100.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-qemu/Cargo.toml \
  --profile esp32s3-devkit-imu-qemu \
  --target-c-source tests/nuttx-std/imu-qemu/bmi270_lower.c \
  --out target/imu-qemu
source target/qemu-tools/environment.sh
python3 tests/nuttx-std/imu-qemu/run.py target/imu-qemu
```

The fixture checks six samples for sequence, units, nominal cadence, and
10 ms spacing between capture timestamps. It joins the worker and checks that
the poll waiter and device handles are gone.
A second acquisition injects FIFO overflow and checks that both the event and
repeated joins report it. A final acquisition verifies recovery with six more
samples. The runner checks the booted kernel and firmware identity, the pass
report, and the shell exit status.

The ESP32-S3 QEMU workflow runs this fixture. Physical GPIO signaling,
controller transfers, and interrupt latency use the
[separate hardware qualification](../imu-qualification/README.md).
