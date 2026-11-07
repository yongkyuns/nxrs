# Physical IMU acquisition witness

This application opens the selected IMU through its public HAL, receives
normalized FIFO observations through a bounded queue, and explicitly stops and
joins acquisition. Its serial summary reports software observations and faults.
Use [the S8 plan](../../../docs/peripheral-composition-plan.md#s8--qualify-protected-execution-and-physical-deadlines)
for the full timing matrix.

## Prepare the board

1. Identify the exact board, flash capacity, sensor, I²C address and wiring.
   Record the board's normal behavior, verify a full-flash backup and establish
   how to restore and check it before loading a qualification image.
2. Select a pair of profiles for the same board. Keep its sensor wiring,
   clock settings and trial workload fixed across modes. Use the build receipt
   to check the expected device, address, bus speed and wake source in each
   image.
3. For a CoreS3 K128 with M135, use `cores3-imu-qualification` and
   `cores3-protected-qualification`. Both use the module's BMI270 at `0x68` on
   `/dev/i2c0`. The `cores3-onboard-bmi270.toml` overlay selects the K128's
   onboard sensor at `0x69` for a separate periodic case. [CoreS3 pin map](https://docs.m5stack.com/en/core/CoreS3) · [M135 connection](https://docs.m5stack.com/en/module/Module_GNSS)
4. Use `overlays/lsm6dso.toml` for a wired LSM6DSO at `0x6a` on the same
   registered sensor bus. The overlay selects periodic wake and retains the
   profile's bus speed. [LSM6DSO electrical interface](https://www.st.com/resource/en/datasheet/lsm6dso.pdf)
5. For the external BMI270 interrupt case on the 4 MiB ESP32-S3-DevKitC-1,
   use `esp32s3-devkit-imu-qualification` and
   `esp32s3-devkit-imu-protected-qualification`. Verify the breakout's I/O
   voltage and common ground. The profile uses GPIO8 for SDA, GPIO9 for SCL
   and GPIO16 for the BMI270's active-high INT1. The LSM6DSO overlay can reuse
   the same I²C wiring for a periodic run. [DevKitC-1 pin map](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/index.html)

## Build and capture

Build each profile with the same Cargo manifest and the same sensor overlay, if
one is used.

```sh
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-qualification/Cargo.toml \
  --profile cores3-imu-qualification --out target/s8-imu-flat
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-qualification/Cargo.toml \
  --profile cores3-protected-qualification --out target/s8-imu-protected
```

Follow the [protected image procedure](../imu-protected/README.md#rust-runtime-witness)
for the kernel and userspace pair. Capture the console from boot, retain the
JSON summary and check the applied sampling rate. The witness observes a
60-second window and fails on a three-second stall, a device fault, a delivery
gap or a queue drop. Set an outer console deadline to detect a stalled boot
or shutdown. Restore and verify the baseline after each authorized firmware trial.

## Evidence to collect for S8

- The HAL sequence and queue-drop counter identify delivery gaps after samples
  have been decoded. Record FIFO faults separately. Exact sensor-side loss needs
  an independent source count, and sample age needs a sensor timestamp with a
  clock mapping or an equivalent external observation.
- Record the electrical IRQ and the start of I²C activity with a common clock
  for the wired case. A scheduler trace or calibrated worker marker is needed
  for IRQ-to-worker latency. Measure bus lock wait separately from wire time
  and state the instrumentation overhead. For periodic wake, use the scheduled
  timer deadline as the reference.
- Record worker CPU time, system load, stack high-water and heap observations
  separately. Read the worker's stack before it exits. Use target scheduling
  accounting or a verified CPU clock. The pinned `getrusage` returns zeroed
  counters. Keep sampling overhead and workload settings the same in both modes.
- Repeat at idle and under the declared workload. Keep the raw logs, firmware
  hashes and restoration result with each run. Define the required latency and
  loss bounds before examining the results.
