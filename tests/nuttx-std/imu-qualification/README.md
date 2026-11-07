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

Add `--features probe` to both builds for the task-context timing summary.
The probe records periodic deadline lateness, time from acknowledged wake to
the first I²C call, each synchronous I²C call, and time from wake to the end of
FIFO drain and sample delivery. The supplied ESP32-S3 qualification builds
use a 1 ms monotonic clock. Record the effective resolution alongside the
results. GPIO acknowledgement precedes the wake
timestamp, so the probe needs a separate IRQ trace or external measurement to
cover the earlier portion. The I²C interval includes kernel wait and service.

The probe prints one `timing_schema: 1` JSON object after the worker joins.
`bounds_us` contains inclusive upper bounds for each histogram. The final
bucket counts observations above the last bound. Each histogram includes
`max_us`. An empty periodic histogram is expected for an interrupt profile.
An incomplete drain, missing I²C observation or dropped probe event invalidates
the timing run. Keep a matched baseline without the probe to assess overhead.

## Capture scheduler notes on the wired DevKit

Use the two DevKit profiles above, `--features probe`, and these additional
build flags in both modes.

```sh
--kconfig-enable ARCH_PERF_EVENTS \
--kconfig-enable PERF_OVERFLOW_CORRECTION \
--kconfig-enable SCHED_INSTRUMENTATION \
--kconfig-enable SCHED_INSTRUMENTATION_FILTER \
--kconfig-enable SCHED_INSTRUMENTATION_SWITCH \
--kconfig-enable SCHED_INSTRUMENTATION_IRQHANDLER \
--kconfig-enable DRIVERS_NOTE \
--kconfig-enable DRIVERS_NOTECTL \
--kconfig-enable DRIVERS_NOTERAM \
--kconfig-enable DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE
```

Check the effective config for a single core, an 8 KiB note ring, and initial
filter mode zero. The fixture clears the ring and records a 200 ms window
starting at the first accepted sample. It filters IRQ notes to the GPIO
controller vector. GPIO16 must be the only active GPIO interrupt on the board.
The fixture disables capture at the deadline and prints the trace after join.

Validate the serial capture with
`python3 tests/host/imu_scheduler_trace.py console.log`. Retain the raw
transcript and parsed result together. The result measures GPIO dispatch entry
to the NuttX post-IRQ worker switch timestamp for attributable wakes. Use an
external capture to measure electrical edge and wire time. Keep a build without
the note Kconfig flags for an overhead comparison.

## Capture worker and allocator usage

Use `--features probe` with the following flags for both modes. Add
`--kconfig-enable STACK_COLORATION` on profiles that do not already select it.

```sh
--kconfig-enable ARCH_PERF_EVENTS \
--kconfig-enable PERF_OVERFLOW_CORRECTION \
--kconfig-enable SCHED_CRITMONITOR \
--kconfig-enable FS_PROCFS \
--kconfig-disable FS_PROCFS_EXCLUDE_PROCESS
```

Verify a single core and `CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD=0` in the
resolved config. The fixture mounts procfs and captures the IMU worker's
thread CPU clock and stack high-water from the first observation to the final
snapshot before stop. The CPU clock is time charged to that thread by NuttX.
It can include task-context kernel work and interrupts charged during a
scheduled interval. The heap values cover the allocator arena visible through
`mallinfo`, including kernel allocations if it is shared. Retain the
`resource_schema: 1` summary along with the effective config and binary hash.

## Capture sensor-side timing and loss

Use `esp32s3-devkit-imu-sensor-reference` or
`esp32s3-devkit-imu-protected-sensor-reference` with `--features probe`. These
profiles pass `--sensor-reference` to the fixture. Apply the chosen sensor
overlay and leave its wake mode fixed across the flat and protected pair.
Connect INT2, SCL, and SDA to voltage-compatible logic analyzer inputs with
common ground. Keep INT1 wired to GPIO16 for the wired BMI270 test and include
it in the capture for the electrical wake measurement.

- BMI270 INT2 carries the shared data-ready signal. Establish the relationship
  between pulses and paired FIFO frames on the actual breakout before counting
  samples from it. Decode completed reads from register `0x26` as 12-byte
  frames in this configuration. [Bosch BMI270 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf)
- LSM6DSO INT2 carries pulsed accelerometer data-ready. Decode the seven-byte
  tagged reads from `0x78`, pairing accelerometer and gyroscope records by
  their FIFO slot. Verify the relationship between reference edges and the
  resulting pairs before counting complete samples. [ST LSM6DSO datasheet](https://www.st.com/resource/en/datasheet/lsm6dso.pdf)

Record FIFO occupancy at the capture boundaries and the exact target address.
Pair output-ready edges with acknowledged FIFO reads on the analyzer clock.
Exclude startup, partial frames, and boundary samples with unresolved backlog.
An overflow or an ambiguous edge-to-frame mapping leaves sample age
unqualified. Report observed source edges, complete FIFO frames, and software
queue losses separately. Joining analyzer and NuttX timestamps requires a
shared marker or a verified clock mapping.

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
