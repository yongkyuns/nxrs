# Peripheral composition architecture

Status — target design for staged implementation. The profiles, bindings and
overlay syntax below are proposed interfaces. This documentation change does
not implement them. Follow the [implementation plan](peripheral-composition-plan.md)
in dependency order. Existing profiles and qualification evidence remain the
baseline until each migration step passes its acceptance checks.

## Ownership

Applications consume capabilities. Profiles assemble their implementations from
reusable peripheral drivers and operating-system resources.

~~~text
application / service
        |
        v
capability HAL                 common observations, configuration and lifecycle
        |
        v
peripheral driver              sensor-specific protocol and API
        |
        v
embedded-hal traits            I2c, SpiDevice, pins, DelayNs
        |
        v
NuttX resource adapter         descriptors, transactions and IRQ notification
        |
        v
NuttX MCU drivers              controller, DMA, interrupt entry and scheduling
~~~

| Owner | Responsibility |
| --- | --- |
| NuttX BSP and configuration | MCU pins, controller initialization, kernel drivers and resource registration |
| Resource adapter | Implement the required Rust resource interface using an owned OS resource |
| Peripheral driver | Chip identification, initialization, register configuration, FIFO decoding and chip-specific errors |
| Capability HAL | Translate peripheral behavior into its public API and own acquisition, delivery, cancellation and recovery |
| Profile resolver | Validate device instances, resource references and capability roles, then select concrete construction code |

Reusable chip drivers belong in ordinary Cargo packages under `driver/`. They
expose sensor-specific APIs and have no dependency on HALs, services, profiles or
the build controller. Retain reviewed upstream libraries and local corrections.
An extracted BMI270 driver continues to use the pinned `bmi2` implementation.

Each capability retains one public API and error vocabulary. For IMU, translation
from BMI270 or LSM6DSO belongs in private, reusable modules in `hal/imu`. Register
settings and FIFO layouts stay in the driver. Physical-unit conversion may be
implemented by the driver. The HAL defines the units and timing promised to its
consumers. Common requirements must report the accepted configuration or reject
unsupported requests. Hardware-specific filters must not acquire misleading
generic meanings.

The HAL supplies acquisition execution and lifetime policy. Drivers perform
synchronous bounded operations when called. Reuse `nxrs-acquisition` and typed
Rust functions or closures for execution. Introduce a private shared protocol
only when multiple implementations actually need the same operations.

## Source organization

~~~text
driver/bmi270/                         reusable sensor-specific Rust crate
driver/lsm6dso/                        added when this device is supported
hal/imu/
  bindings/                           supported compatible/bus schemas
  src/api.rs                          application-facing IMU contract
  src/sensors/bmi270.rs                IMU implementation using the chip driver
  src/sensors/lsm6dso.rs               added with driver and qualification
  src/session.rs                      capability-local shared execution policy
  src/simulation.rs                   adapter to a coordinated simulation session
  src/platform/mod.rs                 selected acquisition exports
  src/platform/nuttx/mod.rs            factory using resolved bindings
  src/platform/nuttx/i2c.rs            embedded-hal implementation
  src/platform/nuttx/irq.rs            cancellable notification ownership
  src/platform/nuttx/ffi/              mechanical native ABI translation
  src/platform/macos/                 added for an implemented macOS source
platform/nuttx/platforms/              existing product and qualification profiles
platform/profiles/                     existing host profiles and future sim profile
platform/build-support/                shared host-only resolution and emission
platform/build/                        build CLI and Cargo orchestration
~~~

OS adapters stay under the actual OS. Boards using the same sensor share the
sensor implementation. A board-specific factory is warranted when it implements
additional behavior beyond supplying bindings. A macOS source can implement the
capability directly through a native API or a supported external transport.
Targeting macOS alone does not establish that an IMU source exists.

Initially keep the NuttX I2C adapter private to the IMU HAL. When another
capability needs the same protocol, extract it into an owning resource HAL such
as `hal/i2c`, including its ABI glue and qualification. This is a reuse decision
with real consumers. The root `platform/` directory remains build/configuration
code.

## Use existing Rust resource interfaces

Use `embedded-hal` 1.0 blocking traits where they match the operation. SPI devices
with chip select use `SpiDevice`, whose adapter owns bus exclusion and chip-select
sequencing. I2C transactions preserve repeated starts, completion and error
semantics. Reusing a trait requires qualifying its contract, including supported
operation sequences and transfer sizes. The present IMU adapter's bounded
transfer subset requires review before becoming a general bus adapter.

Keep ordinary file, socket, thread and synchronization operations in Rust std.
GNSS may retain `std::io` when that expresses its stream contract. Use
`embedded-io` when a portable no_std driver needs that ecosystem's interface.
The async GPIO `Wait` trait is available for an async consumer. Current
threaded acquisition uses cancellable descriptor readiness. Introducing a new
executor is a separate architectural decision.

The profile configures resources before handing them to the driver. The driver
receives usable bus/delay interfaces. Paths and profile names stay in the factory.

## Profile composition v1

Retain the existing TOML profiles and NuttX BSP selection. Add a versioned
composition section to describe Nxrs-managed peripherals above NuttX resources.
NuttX Kconfig and board initialization remain authoritative for MCU wiring.
Existing profile Kconfig settings can configure those facilities. Device graph
validation checks declared requirements. Acquisition verifies actual endpoints
and chip identity.

The following fragment replaces the IMU portion of a complete CoreS3 profile.
Its existing board, target, GNSS and Kconfig configuration still applies.

~~~toml
[composition]
version = 1
mode = "hardware"

[resources.sensor_bus]
kind = "i2c"
device = "/dev/i2c0"
frequency = 400000

[devices.motion]
compatible = "bosch,bmi270"
bus = "sensor_bus"
address = 0x68

[devices.motion.wake]
kind = "periodic"

[roles]
imu = "motion"

[imu]
stack-bytes = 16384
~~~

The existing `[hal] imu = "nuttx"` selects the OS integration during migration.
It must agree with the target and resolved device. The physical model is selected
only by `compatible`. Legacy `imu.driver`, `device`, `address`, `frequency` and
`irq-device` cannot coexist with a v1 IMU role. `imu.stack-bytes` remains an
execution budget. Existing sensor operating settings remain fixed during the
structural migration. Configurable ODR/ranges are a subsequent API step.

For wired hardware, replace the whole wake table with `kind = "interrupt"` and
`resource = "imu_irq"`. Declare that resource with `kind = "gpio-interrupt"`,
its registered NuttX `device` endpoint and `trigger = "rising"`. V1 admits the
existing interface shape with host witnesses. Additional polarities and masking modes
require provider support. A periodic wake derives its interval from the accepted
sensor ODR. An interrupt acquisition failure remains an error. The M135 stock
module uses periodic wakes because its interrupts are unwired.

Device IDs identify instances. `roles.imu` names the default instance used by the
existing `imu::open(sink)` API. Two physical nodes may share a bus at distinct
addresses. The factory leases each physical instance and serializes transactions
through the OS bus implementation. Reopening the same exclusive instance returns
Busy. Multiple named application-facing acquisitions require a separate typed
capability API step. The resolved role determines device selection.

## Bindings and overlays

A capability-owned binding describes one supported compatible/bus combination,
its allowed properties, ranges, wake modes and required resources. Build-only
resolution maps the binding to a reviewed HAL constructor and ordinary Cargo
dependency. Driver crates remain independent of that catalog. Constructors are
allowlisted implementations. Profiles cannot supply Rust paths or executable
code. Unknown models, duplicate binding matches and unsupported buses fail.

Use explicit ordered overlays. The proposed CLI adds repeatable `--overlay`
arguments to build and profile inspection. A named profile may also declare an
ordered `overlays` list. Resolution order is ancestor profiles, child profile,
declared overlays from ancestor to child, then command-line overlays in order.
Overlay files contain changes only and cannot recursively include overlays or
change the target, BSP, execution OS or composition mode. Those changes require
a named profile. File-relative paths resolve against their declaring file.

Define merging before implementing the CLI.

1. Merge named resource/device maps by ID and property. Scalars and arrays replace
   the preceding value. Explicit `remove-devices` and `remove-resources` lists
   remove existing IDs. Reject unknown removals and same-overlay remove/update.
2. A change to `compatible` replaces the entire device record. A change to resource
   `kind` replaces the entire resource record. Require all new mandatory fields.
   The wake table is always replaced as a unit. This prevents stale chip-specific
   settings and IRQ references from surviving a replacement.
3. Role assignments replace explicitly. Remove a role through `remove-roles`.
   Removing its device alone leaves an invalid reference until repaired.
4. Validate the fully merged graph. Record override provenance, input content
   hashes and ordering. Intentional later overrides are deterministic. Malformed
   inputs and conflicting final resource claims fail with source locations.

An overlay for a supported replacement sensor can therefore contain this
complete device replacement, while retaining `roles.imu = "motion"`.

~~~toml
[devices.motion]
compatible = "st,lsm6dso"
bus = "sensor_bus"
address = 0x6a

[devices.motion.wake]
kind = "periodic"
~~~

This example becomes buildable only after LSM6DSO has its driver, IMU
implementation, binding and tests. Overlays describe the attached hardware.
They do not add support for an unimplemented device.

Validate missing references, disabled/removed required devices, incompatible
role assignments, duplicate physical endpoint/address claims, exclusive IRQ/CS
claims and invalid resource requirements. Shared I2C buses are valid. Conflicting
speed declarations for the same physical endpoint fail in v1. Any provider
dependencies used for initialization must be acyclic. Physical pin conflict
proof depends on the NuttX BSP's available metadata and target checks. A pathname
alone cannot establish electrical correctness.

## Construction and Cargo

Resolve once to a typed device/resource graph in `nxrs-build-support`. Both the
CLI and HAL build scripts use that resolver. Emit inspectable resolved JSON and
a semantic configuration digest over the normalized graph, mode and target.
Keep an additional input fingerprint covering ordered source content hashes,
binding schemas and resolver/emitter revision in the build/cache identity.
Equivalent graphs share a semantic digest even if input spelling differs.
Input/order changes still invalidate source-derived outputs through the input
fingerprint. Generate only typed constants and selections of checked-in
factories in the HAL's OUT_DIR.

Cargo still resolves dependencies before running HAL build scripts. V1 keeps
portable driver crates as ordinary declared dependencies in the applicable HAL
manifests, following existing target scoping where valid. Select the actual HAL
source and constructor at compile time. Unselected portable dependency artifacts
may still compile. Measure build cost separately from linked firmware size.
Build scripts cannot enable optional dependencies. Any later dependency-pruning
optimization must use the existing Cargo graph machinery and qualify workspace,
transitive and detached consumers before changing this rule.

Retain `NXRS_PLATFORM` for direct Cargo use and `NXRS_SDK_ROOT` for detached
consumers. Such consumers use a named profile with declared overlays. Ad hoc CLI
overlays are normalized into a per-build resolved-profile file under the owned
output directory, passed to HAL build scripts through a planned
`NXRS_RESOLVED_PROFILE` input. If combined with a named profile, identities must
agree. Validate the file version, both fingerprints and target by re-resolving
the recorded inputs. Retain all input paths for Cargo rebuild tracking. Write
atomically and never rewrite source profiles.
Equivalent input selections through either entry point must produce the same
typed graph and digest. Invalid/stale selections fail before native compilation.

For migrated capabilities, the selected physical, simulation or unavailable
factory replaces independent `hal_mock`/`hal_enabled` admission. Existing cfgs
remain in unmigrated capabilities until their normal entry points and fixtures
are migrated. Contract-only builds retain explicit unavailable factories.
Selected exports remain named and factory-local. Generated wiring needs audit
coverage and cannot introduce OS branches into shared algorithms.

## Simulation and other operating systems

Simulation uses a separate profile with `composition.mode = "simulation"`.
Hardware profiles reject simulated sources. Simulation profiles reject physical
device claims. Mixed fault-injection images use separately classified test-only
qualification profiles with `mode = "qualification"`, admitted only for an
application marked `package.metadata.nxrs.qualification-only = true`. An overlay
cannot change the mode. Legacy fixtures keep their current entry points until
their explicit migration step.

The coordinated engine owns logical time, seeded scenario state and stable event
ordering. HAL endpoints project the same world into IMU, GNSS and other supported
observations. Put OS-independent engine behavior in `shared/simulation` and keep
capability mappings in their HALs. Product wiring supplies scenario configuration.
Factories obtain one owned simulation session through a platform-local runtime
factory, with explicit lease/refcount and reset-after-join rules. Tests can pass
an explicit session. The runtime factory must never return unrelated per-HAL
clocks or retain mutable scenario state across completed runs.

Readiness barriers must register all expected endpoints before time advances.
Delivery uses stable event keys and bounded acknowledgement, cancellation and
loss policies. Seeded samples alone do not make application execution
deterministic. Service deadlines affecting a deterministic trial need an explicit
logical-clock/wake input or a scheduler harness. Until that is qualified, report
deterministic source playback separately from deterministic application behavior.
Existing independent mock-session tests remain explicit fixture tests.

A native macOS or Linux source may satisfy a role directly through an OS API.
It supplies the same HAL contract and validates its own binding schema. Bus-based
drivers are used where the source actually offers those operations. Platform
selection must fail required-capability validation when no supported source is
configured.

## Interrupts, protection and qualification

In protected builds, NuttX owns interrupt entry and MCU bus/DMA operations. An ISR
retains bounded readiness and wakes task-context acquisition. User Rust performs
sensor transactions through qualified OS interfaces. GPIO-controller
acknowledgement, sensor-status acknowledgement and interrupt rearming have
separate owners. Qualify edge coalescing, level handling and cancellation races.

Native ABI witnesses must cover userspace pointers, descriptor lifetime, ioctl
message layouts and any DMA buffer constraints. No ISR may call an application
sink, allocate or wait for queue space. Shutdown stops sensor production,
quiesces notifications and joins acquisition before releasing resources. Failed
initialization and failed cleanup retain the existing rollback/quarantine policy.

Measure interrupt-to-acquisition delay, bus wait, FIFO drain time, sample age,
loss, CPU time and memory on the same physical workload in flat and protected
builds. Define budgets from ODR, FIFO headroom and application deadlines before
measurement. Camera/network load belongs in a separate loaded comparison.
Hardware sensor timestamps and host reception timestamps must remain distinct.
Host tests and QEMU can validate behavior and ABI paths, but cannot qualify radio,
bus timing, physical IRQ wiring or worst-case device latency.

## Sources and compatibility boundaries

Zephyr supplies the device-description, binding and overlay concepts. Its sensor
drivers implement a common API. Nxrs keeps sensor-specific driver APIs and owns
their capability translation in HALs. These are deliberate implementation
choices. [Zephyr bindings](https://docs.zephyrproject.org/latest/build/dts/bindings.html),
[overlays](https://docs.zephyrproject.org/latest/build/dts/intro-input-output.html),
[Kconfig relationship](https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html),
[sensor API](https://docs.zephyrproject.org/latest/hardware/peripherals/sensor/index.html).

Rust's embedded ecosystem supplies portable I/O contracts. Resource configuration
and capability lifecycle stay in Nxrs.
[embedded-hal](https://docs.rs/embedded-hal/1.0.0/embedded_hal/),
[SPI ownership](https://docs.rs/embedded-hal/1.0.0/embedded_hal/spi/index.html),
[GPIO waits](https://docs.rs/embedded-hal-async/1.0.0/embedded_hal_async/digital/trait.Wait.html).

Protected execution and user-level interrupt handling require OS-specific
mechanisms and measurement. QNX demonstrates a userspace-driver model, but its
interrupt service threads do not establish NuttX latency or ABI compatibility.
[NuttX protected builds](https://nuttx.apache.org/docs/latest/guides/protected_build.html),
[QNX interrupt handling](https://qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.getting_started/topic/s1_inter_Handling.html).
