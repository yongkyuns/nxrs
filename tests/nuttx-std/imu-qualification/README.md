# Physical IMU acquisition witness

This application opens the selected IMU through its public HAL, receives
normalized FIFO observations through a bounded queue, and explicitly stops and
joins acquisition. Its serial summary reports software observations and faults.
Use [the S8 plan](../../../docs/peripheral-composition-plan.md#s8--qualify-protected-execution-and-physical-deadlines)
for the full timing matrix.

This fixture's `probe` feature enables HAL tracing together with its timing
recorder and target-only scheduler, GPIO and resource collectors. Without it,
ordinary acquisition uses zero-sized tracing and diagnostic opens are unsupported.

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
   `/dev/i2c0`. M5Stack lists the M135 at `0x69`. Check the module's address
   switch and verify its `0x68` response before using these profiles. The
   `cores3-onboard-bmi270.toml` overlay selects the K128's onboard sensor at
   `0x69` for a separate periodic case. [CoreS3 pin map](https://docs.m5stack.com/en/core/CoreS3) · [M135 connection](https://docs.m5stack.com/en/module/Module_GNSS)
4. Use `overlays/lsm6dso.toml` for a wired LSM6DSO at `0x6a` on the same
   registered sensor bus. The overlay selects periodic wake and retains the
   profile's bus speed. [LSM6DSO electrical interface](https://www.st.com/resource/en/datasheet/lsm6dso.pdf)
5. For the external BMI270 interrupt case on the 4 MiB ESP32-S3-DevKitC-1,
   use `esp32s3-devkit-imu-qualification` and
   `esp32s3-devkit-imu-protected-qualification`. Verify the breakout's I/O
   voltage and common ground. The profile uses GPIO8 for SDA, GPIO9 for SCL
   and GPIO16 for the BMI270's active-high INT1. Use
   `overlays/bmi270-periodic.toml` to switch the same DevKit and BMI270 to
   periodic wake. The LSM6DSO overlay can reuse the I²C wiring for a periodic
   run. [DevKitC-1 pin map](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/index.html)

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

## Capture scheduler and I²C notes

Use `--features probe` and these additional build flags in both modes.

```sh
--kconfig-enable ARCH_PERF_EVENTS \
--kconfig-enable PERF_OVERFLOW_CORRECTION \
--kconfig-enable SCHED_INSTRUMENTATION \
--kconfig-enable SCHED_INSTRUMENTATION_FILTER \
--kconfig-enable SCHED_INSTRUMENTATION_DUMP \
--kconfig-enable I2C_SCHED_NOTES \
--kconfig-enable DRIVERS_NOTE \
--kconfig-enable DRIVERS_NOTECTL \
--kconfig-enable DRIVERS_NOTERAM \
--kconfig-enable DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE
```

For the wired DevKit run, also add `--kconfig-enable SCHED_INSTRUMENTATION_SWITCH`
and `--kconfig-enable SCHED_INSTRUMENTATION_IRQHANDLER`. GPIO16 must be the
only active GPIO interrupt on the board. The collector filters IRQ notes to
the GPIO controller vector. Omit those two flags for periodic wake.

Check the effective config for a single core, a 32 KiB note ring, and
pseudo-filesystem operations enabled. The fixture disables tracing during setup,
clears the ring, and records a 200 ms window starting at the first accepted
sample. It disables capture at the deadline and prints the trace after join.

Validate the serial capture with
`python3 tests/host/imu_scheduler_trace.py console.log`. Retain the raw
transcript and parsed result together. The report separates character-driver
mutex wait, controller mutex wait, and controller service. In wired mode it
also reports GPIO dispatch entry to worker switch, character-driver entry,
and controller-service entry where the records can be paired. Use an external
capture for electrical edge and wire time. Keep a build without the note
Kconfig flags for an overhead comparison.

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
profiles enable the sensor's output-ready signal and a delivery marker on the
DevKit's stock GPIO15 output (`/dev/gpio0`). Apply the chosen sensor overlay and
leave its wake mode fixed across the flat and protected pair. Connect INT2, SCL,
SDA, and GPIO15 to voltage-compatible logic analyzer inputs with common ground.
Keep INT1 wired to GPIO16 for the wired BMI270 test and include it in the
capture for the electrical wake measurement. Start capture before sensor
initialization so the first FIFO state and marker edge are visible.

- BMI270 INT2 carries the shared data-ready signal. Establish the relationship
  between pulses and paired FIFO frames on the actual breakout before counting
  samples from it. Decode completed reads from register `0x26` as 12-byte
  frames in this configuration. [Bosch BMI270 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf)
- LSM6DSO INT2 carries pulsed accelerometer data-ready. Decode the seven-byte
  tagged reads from `0x78`, pairing accelerometer and gyroscope records by
  their FIFO slot. Verify the relationship between reference edges and the
  resulting pairs before counting complete samples. [ST LSM6DSO datasheet](https://www.st.com/resource/en/datasheet/lsm6dso.pdf)

1. Record the exact target address and FIFO occupancy from the bus trace. For
   BMI270, read the observed length from register `0x24`. For LSM6DSO, use the
   status read at `0x3a`. Pair each source edge or validated group of edges
   with a completed FIFO sample. Exclude startup, partial frames, and boundary
   samples with unresolved backlog.
2. Capture all 32 GPIO15 transitions. The fixture starts low and alternates
   high and low once per sample at the HAL sink, before queue admission. The
   first rising edge is sequence 1. A complete capture ends low at sequence 32.
   Confirm that each transition follows the corresponding completed FIFO read.
   In a BMI270 burst, map transitions to the 12-byte frames in read order.
3. Keep the post-join `delivery_marker_schema: 1` JSON object with the normal
   fixture pass summary. Require `status: pass` and `count: 32` in the marker
   report. Its `bracket_ns` array is ordered by sample sequence.
   Incomplete or nonconsecutive marker sequences, GPIO failures, FIFO faults,
   software queue drops, and ambiguous analyzer matches invalidate the run.
   Count source edges, complete FIFO samples, and delivered samples separately.
   Keep unresolved boundary samples separate from measured loss.
4. Measure output-ready edge to GPIO15 edge on the analyzer clock. Call that
   interval `d`. The software endpoint is the monotonic read immediately
   before the GPIO write inside the HAL sink, before queue admission. The
   age at that point is bounded by `max(0, d - b - a)` through `d + a`.
   Here `a` is the analyzer's interval uncertainty, including both edges'
   quantization, channel skew, and timebase accuracy. Use the sequence's
   `bracket_ns` plus `clock_resolution_ns` for `b` after qualifying the target
   clock for elapsed-time bounds. Until then use `d + a` as the upper bound
   and report the software bracket separately. Sensor conversion and filter
   delay precede the output-ready reference. Keep the raw trace and record
   the analyzer model and sample rate.

The marker measures the bounded initial capture only. Report its overhead
separately from the matched run with the ordinary qualification profiles.

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

## SPI composition witnesses

`overlays/bmi270-spi.toml` and `overlays/lsm6dso-spi.toml` select SPI resource
bindings using the same qualification application. They use periodic FIFO wakes
and `/dev/spi2`, mode 3, 4 MHz, `SPIDEV_USER(0)`. They do not configure physical
pins or enable the controller. A board build must register that resource and
provide the CS mapping described in [driver/spi](../../../driver/spi/README.md).
The existing host freshness test compiles all four sensor/transport selections
in one Cargo output directory. Local flat/protected checks compile production
Rust and the enabled native SPI header layout and relocatably link the selected
path; they do not boot the firmware or exercise an electrical bus. GPIO and
I²C physical qualification requirements remain unchanged.
