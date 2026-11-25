# NXRS examples

Run a small Rust program to learn an API, or pick an integration that matches the project you want to build.

## Choose an example by task

| I want to… | Example | Runs without a board? |
| --- | --- | --- |
| Build and run a Rust application | [Basic firmware](#basic-firmware) | Yes, as host Rust and in the firmware tutorial |
| Send messages to a worker and stop it cleanly | [Basic firmware worker](#basic-firmware) | Yes, using the same tutorial workspace |
| Switch an output and confirm each command | [Controller application](#controller-application) | Yes, with the explicit LED mock on the host and in QEMU |
| Switch a light from Home Assistant | [Simulated Matter light](../examples/matter-led/README.md) | Yes, with the same LED mock and a Matter controller |
| Try an ESP32-S3 touchscreen and BLE pairing | [Motion Tracker in the browser](motion-tracker.md) | Yes, with two NuttX cores and modeled peripherals |
| Use Rust collections, channels, and deadlines | [Standard-library recipes](#standard-library-recipes) | Yes |
| Test overload and repeated shutdown | [Stress example](#exercise-overload-and-shutdown) | Yes; host measurements describe the host |
| Try replay, recording, or UDP output | [Software capability tests](#test-a-reusable-software-capability) | Yes, subject to their file/network prerequisites |
| Open a maintenance shell and run a Rust command over SSH | [Dropbear and NSH](io.md#optional-remote-shell) | The ESP32-S3 terminal example needs a board |
| Connect several services in one application | [Event-driven demo](../../docs/event-driven-demo.md) | Yes with its documented mock or simulation setup |
| Use two devices on a shared bus | [CoreS3 second-sensor example](#add-a-second-sensor-on-cores3) | Build inspection is local; acquisition needs the configured devices |
| Follow a working camera pipeline | [IoT camera](#iot-camera) | Parts run on the host; full capture and delivery need the relevant hardware/services |

Complete the [host setup](getting-started.md) and run the commands below from the NXRS root.

## Basic firmware

The [basic-firmware package](../examples/basic-firmware/) has a normal Rust entry point, an independent Cargo workspace, and NuttX firmware metadata. It is the starting point for [Your first application](first-application.md).

```sh
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --bin hello
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --example worker
```

`hello` prints a greeting. `worker` sends typed commands to a bounded inbox and joins the worker after shutdown. It uses no hardware-specific service or driver.

The greeting prints `Hello from NXRS`. The worker finishes with `Processed total: 6`.

## Controller application

[Build a controller](controller.md) adds `on`, `off`, and `status` commands to a bounded worker. Run it on your computer or in QEMU with the mock LED, then follow the [Matter light example](../examples/matter-led/README.md) to control the same output from Home Assistant.

## Standard-library recipes

[app/std-demo](../../app/std-demo/) contains collections, ownership, channels, synchronization, and deadline examples:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

The selected channel recipe finishes with `STD_DEMO PASS cases=1`. With no `--case`, the program runs all nine recipes and reports `STD_DEMO PASS cases=9`. Each recipe checks its own result before printing success.

| `--case` | What to inspect in the source |
| --- | --- |
| `vec` | Growth, reservation, reuse, and an application-enforced capacity limit |
| `fixed` | Inline arrays and an occupied slice |
| `maps` | `HashMap` updates, admission limits, and ordered `BTreeMap` entries |
| `queues` | `VecDeque` reuse and earliest-deadline ordering with `BinaryHeap` |
| `bytes` | Fixed byte buffers, little-endian encoding, `String`, and `Cow` |
| `ownership` | Unique/shared ownership, one-time initialization, and joined threads |
| `channels` | Bounded and rendezvous channels, full/disconnected handling, and moved buffers |
| `synchronization` | Mutex/condition-variable predicates and atomic publication |
| `deadlines` | An absolute deadline and remaining-time receive waits |

The implementations are in [collections.rs](../../app/std-demo/src/collections.rs) and [concurrency.rs](../../app/std-demo/src/concurrency.rs). These recipes explain behavior; they are not a claim that every `std` operation is allocation-free or has a target-independent timing bound.

After completing the target setup in [Building for NuttX](firmware.md), build this repository application with:

```sh
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock
```

Use the tutorial's QEMU invocation with the image path changed to `target/build/std-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin`. At `nsh>`, run `std_demo --case channels`. The Cargo application name uses a hyphen; its registered NSH command uses an underscore.

Use [Standard-library APIs](standard-library.md) to choose APIs for your own code and the [recipe reference](../../docs/std-demo-and-ao-stress.md#the-curated-recipes) for details.

## Exercise overload and shutdown

`ao-stress` uses producer threads, bounded worker inboxes, and a collector. Start with the default configuration on the host:

```sh
cargo run --locked -p nxrs-ao-stress -- --scenario steady
cargo run --locked -p nxrs-ao-stress -- --scenario burst --rounds 3
```

The first run ends with `AO_STRESS PASS scenarios=1 rounds=1`; the second ends with `AO_STRESS PASS scenarios=1 rounds=3`. Before that, `AO_RESULT` records report accepted work, losses, latency, owner progress, and shutdown behavior. Exact counts and timings depend on the executing system.

The stress workload deliberately counts full-queue drops. A passing run verifies its accounting, ordering, checksums, and cleanup; it does not mean zero loss. Compare admitted work with completed work before interpreting a throughput number. Read the [stress reference](../../docs/std-demo-and-ao-stress.md#stress-topology-and-policy) before changing capacities, thread counts, or stack settings for an MCU.

## Test a reusable software capability

These package tests exercise existing implementations without selecting a hardware profile:

```sh
cargo test --locked -p nxrs-camera --features replay
cargo test --locked -p nxrs-storage --features recording
cargo run --locked -p nxrs-std-udp -- pass
```

Follow the owning [camera](../../hal/camera/README.md), [storage](../../hal/storage/README.md), and [UDP qualification](../../tests/nuttx-std/udp/README.md) guides for runnable examples and file or network inputs. Enable the feature on the dependency that owns the functionality. The package references explain queue admission, record commit, cancellation, and cleanup contracts that matter when combining these components.

## IoT application structure

A connected logger or controller commonly needs device access, processing, persistent records, and a connection manager. Those responsibilities can be separate components with explicit resource ownership and typed events.

[Networking and storage](io.md#design-example-a-connected-data-logger) uses a logger as a design example. The application chooses record formats, retention policy, and behavior while disconnected. These are use cases for the framework rather than a required NXRS application model.

<span id="experimental-iot-camera"></span>

## IoT camera

The existing [home-cam application](../../app/home-cam/) combines capture, detection, streaming, and gateway/client integration. Its [delivery reference](../../docs/camera-detection-delivery.md) describes the end-to-end protocol and ownership. Model-specific [evaluation results](../../docs/camera-detection-qualification.md) include streaming, accuracy, memory, and recovery tests.

For a Matter integration, follow the [camera integration guide](../../gateway/matter/integration.md)
for architecture diagrams and steps for Home Assistant, a custom Matter
controller, or a client using the Nxrs gateway API.

Read this material when working on a camera application. It is not required for learning the build system, service model, or hardware interfaces.

## Other repository applications

<span id="navigation-and-service-composition"></span>

The repository also contains [event-demo](../../app/event-demo/) and [dual-imu-demo](../../app/dual-imu-demo/), which exercise sensor-specific service composition and named devices. Their [application notes](../../docs/event-driven-demo.md) remain available as implementation examples rather than the default introduction to NXRS.

Read `main.rs` to see component construction and shutdown, then follow the linked service and HAL contracts. Copy a pattern only after deciding which component should own each resource in your own application. The application examples do not define a mandatory set of services or a required event bus.

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

For your next application, start with the small source package and add one useful capability at a time. Keep a host test for the behavior, a target build for the integration, and a physical test for each hardware dependency. [Designing applications](designing-applications.md) develops that process.
