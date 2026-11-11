# NXRS examples

Use the small examples to learn a particular API or pattern. Larger applications show how several components fit together and are optional reading.

## Basic firmware

The [basic-firmware package](../examples/basic-firmware/) has a normal Rust entry point, an independent Cargo workspace, and NuttX firmware metadata. It is the starting point for [Your first application](first-application.md).

```sh
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --bin hello
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --example worker
```

`hello` prints a greeting. `worker` sends typed commands to a bounded inbox and joins the worker after shutdown. It uses no hardware-specific service or driver.

## Standard-library recipes

[app/std-demo](../../app/std-demo/) contains collections, ownership, channels, synchronization, and deadline examples:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

Use [Standard-library APIs](standard-library.md) for an overview and [Building for NuttX](firmware.md) to build this application for a target.

## IoT application structure

A connected logger or controller commonly needs device access, processing, persistent records, and a connection manager. Those responsibilities can be separate components with explicit resource ownership and typed events.

[Networking and storage](io.md#design-example-a-connected-data-logger) uses a logger as a design example. The application chooses record formats, retention policy, and behavior while disconnected. These are use cases for the framework rather than a required NXRS application model.

<span id="experimental-iot-camera"></span>

## IoT camera

The existing [home-cam application](../../app/home-cam/) combines capture, detection, streaming, and gateway/client integration. Its [delivery reference](../../docs/camera-detection-delivery.md) describes the end-to-end protocol and ownership. Model-specific [evaluation results](../../docs/camera-detection-qualification.md) include streaming, accuracy, memory, and recovery tests.

Read this material when working on a camera application. It is not required for learning the build system, service model, or hardware interfaces.

## Other repository applications

<span id="navigation-and-service-composition"></span>

The repository also contains [event-demo](../../app/event-demo/) and [dual-imu-demo](../../app/dual-imu-demo/), which exercise sensor-specific service composition and named devices. Their [application notes](../../docs/event-driven-demo.md) remain available as implementation examples rather than the default introduction to NXRS.

## Add a second sensor on CoreS3

The [CoreS3 profile](../../platform/cores3/profile.toml) configures a CoreS3
with the M135 GNSS module. The [CoreS3 NuttX patch](../../upstream/nuttx/patches/0104-esp32s3-m5-cores3-raw-resources.patch)
adds the board and its `nsh` defconfig. Its `board_late_initialize()` calls
NuttX's shared `board_i2c_init()`. The [resource mapping](../../platform/nuttx/resources/esp32s3-m5-cores3.toml)
enables I²C0 master mode and the I²C character driver, so that initializer
registers `/dev/i2c0`. The profile selects that bus as `sensor_bus` and
configures the M135's BMI270 at `0x68`. Both sensors use the same registered
bus endpoint. Each sensor address is handled by the Rust driver.

For a standalone NuttX smoke build, configure `esp32s3-m5-cores3:nsh` with
the patch applied. In `make menuconfig`, enable `ESP32S3_I2C0`,
`ESP32S3_I2C0_MASTER_MODE`, and `I2C_DRIVER`, and set SDA to GPIO12 and SCL
to GPIO11. The defconfig already enables late initialization. The NXRS
build supplies these resource settings from the profile.

Use the CoreS3 K128's onboard BMI270 at `0x69` as the second device.
Verify the M135 uses `0x68` to avoid an address conflict. Save this overlay
as `second-bmi270.toml` in the repository root. It selects periodic FIFO
acquisition without a GPIO interrupt binding.
[CoreS3 pin and address map](https://docs.m5stack.com/en/core/CoreS3) ·
[M135 wiring and address](../../docs/m5stack-cores3.md)

```toml
[devices.backup_imu]
compatible = "bosch,bmi270"
bus = "sensor_bus"
address = 0x69

[devices.backup_imu.wake]
kind = "periodic"

[aliases]
imu = "imu"
imu_secondary = "backup_imu"
```

The first alias keeps the existing sensor primary. The second makes the new
device available as `composition::imu::secondary()`. With periodic wake, the
HAL wakes at the configured delivery interval and drains available FIFO samples.
The two devices share the bus and have separate acquisition sessions.

```sh
cargo nxrs profile --platform cores3 --overlay second-bmi270.toml
cargo nxrs build --app dual-imu-demo --platform cores3 --overlay second-bmi270.toml --dry-run
cargo nxrs build --app dual-imu-demo --platform cores3 --overlay second-bmi270.toml --out target/cores3-dual-imu
```

The resolved profile should list both addresses on `/dev/i2c0`. On a CoreS3
development unit, enter USB download mode and replace `PORT` below with
the board's serial port. This profile uses ESP32-S3 Simple Boot.
[CoreS3 download mode](https://docs.m5stack.com/en/core/CoreS3)

```sh
esptool.py -c esp32s3 -p PORT write_flash -fs detect -fm dio -ff 40m 0x0000 target/cores3-dual-imu/nuttx/nuttx.bin
```

Reset the board and open NSH over USB serial. With the M135 fitted,
use `ls /dev` to check for `i2c0`, then run `dual_imu_demo`. It reports
`DUAL_IMU PASS` after sampling both devices and checking that one keeps
producing output while the other sensor's service is paused. See the
[demo source](../../app/dual-imu-demo/src/main.rs).

An app with an optional IMU can use `composition::imu::primary_imu()` on a
profile without an IMU binding. Keep the `imu` Cargo feature enabled and
handle `io::ErrorKind::Unsupported` from `ImuService::start_on()`.
The dual-IMU demo declares IMU as required, so the build rejects an unbound
profile. See [Required and optional devices](profiles.md#required-and-optional-devices).

## Tests and benchmarks

Stress workloads and focused measurement fixtures live alongside the application examples and under `tests/`. Use the [test index](../../tests/README.md) and [Performance and memory](performance.md) when working on those tests; they are separate from the introductory programs.
