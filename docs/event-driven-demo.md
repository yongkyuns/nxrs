# Event-driven std architecture demo

This demo is a **service-owned execution and HAL-ownership skeleton**, not a
physical-hardware demo.

The central rules are:

> A service owns the execution context it needs.

and:

> A service acquires and owns the HAL capability required to implement its
> function. The application composes services, not devices.

See [hal-platform-architecture.md](hal-platform-architecture.md).

## Run with mock capability providers

~~~sh
NXRS_PLATFORM=host-demo cargo build --locked \
  -p nxrs-event-demo --bin event-demo
target/debug/event-demo --duration-ms 2000
~~~

The platform binds IMU and GNSS to private mock implementations.
`app/event-demo` contains no mock-provider or concrete-device dependency.

There is no implicit mock fallback: the capability facades compile without a
provider, but starting IMU/GNSS services without platform bindings
returns Unsupported.

## Topology

~~~text
                       app/event-demo/src/main.rs
                         composition + lifecycle
                                  |
                    create/connect services only
                                  |
              +-------------------+-------------------+
              |                                       |
        ImuService                               GnssService
        owns HAL IMU                             owns HAL GNSS
        owns processor                           owns processor
        owns thread                              owns thread
        recv_timeout()                           recv_timeout()
              |                                       |
              +------ bounded fusion inputs ----------+
                                  |
                           FusionService
                           owns inbox
                           owns fusion state
                           owns thread / recv()
                                  |
                           FusionHandle
                         recv_timeout()
                                  |
                                main
                                  |
                           HealthService
                         synchronous service
~~~

The application does not:

- access the IMU or GNSS HAL;
- name SyntheticImu or SyntheticGnss;
- spawn IMU/GNSS/fusion threads;
- own their control channels or join handles.

Its setup is intentionally close to:

~~~rust
let (fusion, inputs) = FusionService::new();

let imu  = ImuService::new(inputs.imu);
let gnss = GnssService::new(inputs.gnss);

let fusion = fusion.start()?;
let imu    = imu.start()?;
let gnss   = gnss.start()?;
~~~

During start(), each sensor service acquires its HAL capability. Successful
startup therefore means that the selected capability facade supplied a resource
and the owner thread was created.

Shutdown remains high-level orchestration:

~~~text
imu.stop()?
gnss.stop()?
fusion.stop()?
~~~

## What service ownership means

ImuService owns:

- acquisition of the platform IMU through the HAL;
- exclusive lifetime ownership of that IMU handle;
- the periodic read schedule;
- the worker thread;
- the shutdown/control inbox;
- IMU processing state;
- submission of processed samples;
- error/drop statistics;
- thread join on explicit stop().

GnssService owns the corresponding GNSS responsibilities.

FusionService owns its bounded typed input queue, fusion state, worker thread,
publication cadence, bounded output queue, shutdown and join.

The raw sensor resource never escapes its service. Calibration, diagnostics or
other components should interact through service APIs rather than independently
opening the same physical resource.

## Services do not all require threads

Execution ownership is a service decision, not a framework rule.

HealthService is standalone but synchronous. A future configuration service or
pure calibration module may also remain synchronous.

Likewise, a service may contain several composable modules while using only one
owner thread. The intended rule is:

~~~text
service owns the resources required by its functionality
!=
one thread for every type/module
~~~

## Capability-local HAL boundary

The narrow capability contracts remain no_std:

- hal/imu/src/api.rs
- hal/gnss/src/api.rs

The public `nxrs-imu` and `nxrs-gnss` crates own their private providers.
For the current qualification build:

~~~text
host-demo [hal].imu  = "mock" -> SyntheticImu
host-demo [hal].gnss = "mock" -> SyntheticGnss
~~~

Those concrete names exist only below their capability facade.

A future physical platform can bind each capability:

~~~text
[hal]
imu = "<physical-provider>"
gnss = "<physical-provider>"
~~~

Neither `app/event-demo` nor `service/navigation` should change when those
providers are added.

The execution target and provider selection are separate qualification
dimensions. The mock IMU/GNSS providers can run on native Linux, ESP32-S3
NuttX/QEMU, or ARMv8-M NuttX/QEMU. That qualifies software portability without
claiming physical sensor behavior on those boards.

## Multi-instance and second-app qualification

The same navigation service package is also composed by
`app/dual-imu-demo`, a separate ordinary Rust binary. It intentionally does
not share application code or depend on `app/event-demo`.

Its handwritten main creates two independent pipelines:

~~~text
ImuService A -> FusionService A
ImuService B -> FusionService B
~~~

Both `ImuService::start()` calls independently acquire the selected IMU HAL
capability. With `host-imu`, both synthetic devices start their
sequence at 1. The app then pauses A and verifies B continues producing before
resuming A. No global HAL object, runtime registry, provider handle table, or
explicit instance ID is required.

Run it with:

~~~sh
NXRS_PLATFORM=host-imu cargo build --locked \
  -p nxrs-dual-imu-demo --bin dual-imu-demo
target/debug/dual-imu-demo
~~~

The qualification gate additionally builds/runs the app without a selected IMU
provider and requires an explicit Unsupported startup failure. It also verifies
that the GNSS crate's emitted provider cfg remains `none`, preventing selection from leaking across
firmware images.

The original `app/event-demo` remains the cross-platform proof: its unchanged
app/service source is qualified on native host, ESP32-S3 NuttX/QEMU,
MPS2/Cortex-M33 NuttX/QEMU, and a real Pico 2 board build/ABI.

## Active-service std execution

The navigation services intentionally use qualified Rust std facilities through
the minimal `nxrs-service-event` transport:

- `std::thread`;
- bounded `std::sync::mpsc::sync_channel`;
- one `EventInbox::wait(...)` call per active owner;
- `std::time`;
- explicit join-based shutdown.

The shared transport is not an executor or actor framework. It only standardizes
cloneable event senders, one unique bounded inbox, and the single wait operation.

A service does not need to be no_std merely to be portable. Capability contracts
and low-level algorithms can remain no_std while active services use the
qualified std environments.

## One wait point per active loop

Each active service has one logical blocking point and one private event type:

- IMU: `inbox.wait(Some(deadline))` multiplexes commands and sample deadline;
- GNSS: `inbox.wait(Some(deadline))` multiplexes commands and fix deadline;
- Fusion: `inbox.wait(None)` receives IMU, GNSS and lifecycle events from
  cloned senders into one receiver.

Each event is handled to completion before the owner waits again. The current
mock provider is polled at the deadline. A physical HAL may expose
different readiness mechanics below the service/HAL boundary without moving
sensor ownership into main().

## Sensor processing stays inside the sensor service

The HAL produces capability-level samples/fixes. The active service owns the
processing immediately above that HAL boundary:

- ImuProcessor demonstrates bias correction;
- GnssProcessor demonstrates speed/course normalization.

Replacing the selected capability providers must not move that processing into the app.

## Service commands

Sensor handles expose acknowledged domain commands:

~~~rust
imu.set_period(Duration::from_millis(10))?;
gnss.pause()?;

let imu_status = imu.status()?;
let gnss_status = gnss.status()?;

gnss.set_period(Duration::from_millis(100))?;
gnss.resume()?;
~~~

Returning success means the owner loop consumed and applied the command.
status() is handled by that same loop and therefore snapshots service-owned
state without exposing mutable internals.

## Bounded overload behavior

Inter-service data paths use bounded channels. High-rate sensor submission uses
try_send; if fusion is full, the service records a drop instead of allocating an
unbounded queue or blocking indefinitely.

That is a service-specific policy, not a universal framework rule.

## NuttX firmware matrix

All current event-demo NuttX platforms build the unchanged application and the
same mock IMU/GNSS capability providers through the Cargo firmware frontend.

### ESP32-S3

~~~sh
bash tools/install-qemu-tools.sh
cargo firmware --app event-demo --platform esp32s3-qemu-mock
~~~

The QEMU oracle boots:

~~~text
target/firmware/event-demo/esp32s3-qemu-mock/nuttx/nuttx.merged.bin
~~~

### ARMv8-M Cortex-M33 QEMU proxy

~~~sh
cargo firmware --app event-demo --platform mps2-an521-mock

python3 tests/host/test-event-demo-nuttx.py "$(command -v qemu-system-arm)" \
  --machine mps2-an521 \
  --image target/firmware/event-demo/mps2-an521-mock/nuttx/nuttx \
  --log target/firmware/event-demo/mps2-an521-mock/console.log
~~~

MPS2 AN521 exercises the ARMv8-M std/thread/channel/HAL-boundary path; it is not
RP2350 peripheral emulation.

### Raspberry Pi Pico 2

~~~sh
cargo firmware --app event-demo --platform pico2-mock
~~~

This uses the actual `raspberrypi-pico-2:nsh` board configuration and emits
`target/firmware/event-demo/pico2-mock/nuttx/nuttx.bin`, but IMU/GNSS provider
selection is still mock. It therefore qualifies the board build/ABI and portable
app/service path, not physical IMU/GNSS or Pico 2 W radio integration.

A future physical Pico 2 W qualification should add/select a physical product
platform, not modify event-demo.

## What this demo does not claim

It does not qualify:

- physical IMU/GNSS drivers;
- concrete Pico 2/Pico 2 W capability-provider bindings;
- ISR-to-thread sensor notification;
- hard real-time deadlines or priority inversion;
- RP2350 peripheral behavior in QEMU;
- Pico 2 W CYW43/Wi-Fi;
- browser sensor I/O;
- production navigation/filter accuracy.

Its purpose is to prove that application and service code remain unchanged while
execution targets and capability-provider selections vary independently.
