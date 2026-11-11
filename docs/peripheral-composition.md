# Peripheral composition architecture

A named profile and its ordered overlays resolve to one validated composition.
Hardware profiles bind IMU and GNSS aliases to concrete sensors and NuttX
resources. The simulation profile binds those aliases to one shared scenario.
Shared build preparation verifies package declarations and generates typed IMU
and GNSS instances. Small HAL Cargo adapters invoke that
shared path and preserve provider/native build integration. See the
[implementation and qualification plan](peripheral-composition-plan.md).

The accepted [declarative build and static-device follow-up](static-device-composition.md)
replaces local host emission with shared generic preparation, makes driver
features additive, and gives each active physical device one canonical static
opaque object. That work is tracked in [issue #246](https://github.com/yongkyuns/nxrs/issues/246).
IMU and GNSS support simultaneous named instances.
Sections below distinguish current v1 mechanics from the remaining target.

## Ownership

Applications consume capabilities. Profiles assemble their implementations from
operating-system resources and reusable peripheral drivers where needed. Prefer
Rust std operations on NuttX POSIX endpoints before introducing custom access.

~~~text
application / service
        |
        v
capability HAL                 resource ownership and managed lifecycle
        |
        +-- std::fs / std::io / std::net --> NuttX VFS / sockets
        |
        +-- chip / protocol driver
        |       +-- std::io stream ------> NuttX UART endpoint
        |       +-- embedded-hal adapter -> NuttX bus / GPIO controls
        |
        +-- thin native binding ----------> device controls / radio SDK

NuttX BSP and kernel own controller setup, pins, DMA and interrupt entry.
~~~

| Owner | Responsibility |
| --- | --- |
| NuttX BSP and configuration | MCU pins, controller initialization, kernel drivers and resource registration |
| Resource adapter | Implement the required Rust resource interface using an owned OS resource |
| Peripheral driver | Supported chip settings and resource bindings, initialization, register/FIFO protocol and direct implementation of a shared device contract where applicable |
| Shared peripheral contract | Synchronous device configuration, physical measurements, buffering semantics and errors; no runtime or resource construction |
| Capability HAL | Stream metadata, readiness, acquisition, delivery, cancellation and recovery |
| Platform device catalogue | Named portable accessors and concrete driver/resource construction; no acquisition policy |
| Profile resolver | Resolve BSP resources into NuttX configuration and Rust endpoint bindings, validate devices and aliases, then select construction code |

Reusable chip drivers belong in ordinary Cargo packages under `driver/`. They
expose sensor-specific APIs and have no dependency on HALs, services, profiles or
the build controller. Retain reviewed upstream libraries and local corrections.
An extracted BMI270 driver continues to use the pinned `bmi2` implementation.

`driver/peripheral::imu` defines a small shared device contract. BMI270 and
LSM6DSO implement it directly, alongside their chip-specific APIs. Drivers resolve
unit-based requirements using their supported settings, decode measurements into
SI units and report device errors. The HAL reexports the configuration types,
adds reception timestamps and sequence numbers, and maps device failures into its
own lifecycle error vocabulary. Constructors and optional chip qualification
controls stay in the platform catalogue’s NuttX factory. Shared acquisition accepts a
constructed `peripheral::imu::Imu`, without a chip-construction trait or wrapper.

Buffering is explicit: FIFO retains ordered hardware measurements; latest-value
reads can lose intermediate samples without detection. The current IMU acquisition
path admits FIFO implementations only. Neither mode promises lossless delivery
under overload. Wake signals are hints, and the driver must inspect actual data.
Chip filters, FIFO tags and other specialized controls remain chip-specific.

The HAL supplies acquisition execution and lifetime policy. Its `imu::Session`
and `gnss::Session` contracts describe active use, distinct from the synchronous
device operations in the chip drivers. Reuse `nxrs-acquisition` and typed Rust
functions or closures for execution. That helper shares worker code, not a
scheduler. Execution resources are an explicit implementation choice; other
driver protocols may need their own runtime. Introduce a private shared protocol
only when multiple implementations actually need the same operations.

## Source organization

~~~text
driver/peripheral/src/imu.rs           shared synchronous IMU device contract
driver/bmi270/                         chip API and direct IMU contract implementation
driver/lsm6dso/                        reusable tagged-FIFO IMU driver
driver/{bmi270,lsm6dso}/bindings.toml   supported resource combinations and implementation IDs
driver/ubx/                            reusable M9 protocol driver
hal/imu/
  src/api.rs                          application-facing IMU contract
  src/session.rs                      Session contract implemented on the shared worker
  src/platform/simulation.rs          adapter to a coordinated simulation session
  src/platform/mod.rs                 selected acquisition exports
  src/platform/nuttx/mod.rs            normalized acquisition and construction API
  src/platform/nuttx/device.rs         ownership, stream metadata and acquisition
platform/devices/imu/                 named IMU accessors and concrete construction
driver/i2c/, driver/spi/               NuttX-only embedded-hal resource adapters
driver/gpio/                           NuttX GPIO interrupt owner and embedded-hal input
hal/acquisition/                       shared workers, cancellation and native readiness
  src/probe/                          optional acquisition and I²C/SPI timing
platform/devices/gnss/                named GNSS accessors and concrete receiver construction
hal/gnss/                              UART acquisition, stream metadata and simulation adapter
shared/simulation/                     seeded scenario and logical clock
platform/profiles/                     platform defaults and shared WROOM settings
tests/profiles/                        mock, simulation and qualification inputs
tools/build-support/                shared resolution and generic typed generation
tools/build/                        build CLI and Cargo orchestration
~~~

OS adapters stay under the actual OS. Boards using the same sensor share the
sensor implementation. A board-specific factory is warranted when it implements
additional behavior beyond supplying bindings. A macOS source can implement the
capability directly through a native API or a supported external transport.
Targeting macOS alone does not establish that an IMU source exists.

`driver/i2c` owns reusable NuttX bus access, its ABI glue and qualification.
`driver/gpio` owns rising-edge GPIO registration, rollback and descriptor access.
A task-context cleanup-failure hook retains the owner's quarantine policy, including
on failed setup and Drop. `nxrs-acquisition` owns shared worker/cancellation helpers
and one NuttX poll/pipe binding for IMU and GNSS. Catalogues supply resolved resources; HALs own acquisition policy; chip drivers consume embedded-hal traits without
depending on the concrete bus adapter. No macOS or
Linux I²C backend is required. Host witnesses inject test-only bus operations.
The root `platform/` directory owns build/configuration and device catalogues;
acquisition policy stays in the HAL. Apps may use the portable catalogue; services
receive device references and never depend on it.

## Use existing Rust resource interfaces

Select the interface for each operation in this order:

1. Use qualified Rust std APIs wherever they express the NuttX operation. Open
   UART endpoints with `std::fs::OpenOptions` and transfer bytes through
   `std::io::Read`/`Write`; use `std::net` for IP data. A profile resource can
   resolve to an owned `File` without a new resource trait or driver package.
2. Add thin private bindings for controls absent from std: termios configuration
   and restoration, cancellable readiness, device ioctls and native radio calls.
   Opening a POSIX device node does not make all of its controls std operations.
   Preserve Rust ownership, deadlines, rollback and joined cleanup around them.
3. Use existing embedded-hal contracts for operations such as I²C transactions,
   SPI chip-select sequencing and GPIO access. Resource adapters implement these
   semantics over NuttX. Qualify repeated starts, bus exclusion, completion,
   transfer limits and errors; a generic byte stream cannot express them all.
4. Add a reusable Nxrs driver when chip or protocol behavior belongs in Rust.
   A GNSS protocol can consume `Read`/`Write`; an IMU can consume `I2c` or
   `SpiDevice`. No driver needs a no_std requirement solely for this architecture.

These choices apply per operation: one provider can combine std data transfer
with native configuration. Use `embedded-io` only where an actual dependency
requires its interface. A worker or executor requires an execution need.
Paths and profile names stay in construction; drivers receive usable resources.
See [NuttX device access](nuttx-device-access.md) for native-boundary requirements.

Borrow declarative bindings, ordered overlays and logical aliases from
[Zephyr's devicetree approach](https://docs.zephyrproject.org/latest/build/dts/howtos.html).
NuttX BSP/Kconfig remains authoritative for pins, controller initialization and
device registration. Composition records dependencies and ownership constraints;
they do not require a runtime chain of alias, device and resource wrappers.
An integrated peripheral may use a direct native endpoint; a composite device may
need several typed resources. Those extensions remain future schema work: v1
currently models IMU/GNSS with a bus and optional interrupt wiring.

## Profile composition v1

Retain the existing TOML profiles and NuttX BSP selection. Add a versioned
composition section to describe Nxrs-managed peripherals above NuttX resources.
The profile is the single product input for resource selection and wiring.
`platform/nuttx/resources/<board>.toml` maps supported BSP bindings to registered
paths, required Kconfig and native pin-setting symbols. After inheritance and overlays,
the resolver derives both NuttX configuration and typed Rust endpoint bindings.
The mapping and every input are hashed for Cargo freshness and build receipts.
NuttX retains its existing controller initialization and endpoint registration.

~~~text
profile + overlays -> BSP resource resolution
                       |-> Kconfig -> NuttX bring-up -> registered endpoints
                       |-> Rust bindings -> open endpoints -> peripheral drivers
~~~

Select `binding` and supply its declared native pin symbols in the resource's
`kconfig` table. Do not repeat `kind`, `/dev` paths or the binding's fixed Kconfig
requirements in the product. The mapping derives UART baud
configuration from the same `baud` consumed by Rust. Explicit Kconfig remains
available for OS settings, buffers and timing budgets. Conflicting explicit or
diagnostic settings fail. The build checks the effective configuration after
`olddefconfig`, including disabled settings omitted by Kconfig.

The initial mappings cover CoreS3 I²C0/1 and UART1, and DevKit I²C0/1, its
configurable GPIO IRQ and SPI2. CoreS3 UART1 keeps UART0 enabled and uses the USB
console so `/dev/ttyS1` cannot change identity. CoreS3 does not register SPI and
rejects that selection. Adding a binding requires checking BSP registration;
an entry cannot create missing bring-up support. Pins claimed by declared
resources must not conflict. Other board peripherals, electrical suitability
and hardware operation still require board qualification. Acquisition verifies
endpoint availability and chip identity.

### Reuse NuttX pin configuration

NuttX owns MCU pin encoding and electrical setup. Nxrs uses existing Kconfig
symbols for configurable pins and leaves fixed wiring in native board headers
and initialization. It does not maintain a GPIO-controller catalogue, pinctrl
states, mux encodings or an inventory of every valid pin combination.

~~~toml
[resources.sensor_bus]
binding = "i2c0"
kconfig = { ESP32S3_I2C0_SDAPIN = 8, ESP32S3_I2C0_SCLPIN = 9 }
frequency = 400000

[resources.imu_irq]
binding = "gpio_irq"
kconfig = { ESP32S3_DEVKIT_GPIO_IRQ_PIN = 16 }
~~~

These are native ESP32-S3 GPIO numbers: `16` means GPIO16, not pin 16 on an
unspecified port. Each mapping identifies its native `pin-namespace` and lists
only the `pin-settings` needed by its registered resources. Symbols for another
controller, missing settings and noninteger values fail resolution. Claims in
that namespace, including declared fixed BSP reservations, must not conflict.
Reuse of the same bus setting is permitted. Port/mux selections defined in a
NuttX board header stay there; do not flatten port-local numbers into this space.

An overlay replaces a resource's `kconfig` table as a whole. Native settings are
recorded in the resolved report and hardware semantic digest; changes invalidate
build inputs even when the Rust endpoint path stays the same. NuttX evaluates
its own symbol ranges and dependencies, and the build rejects effective settings
that differ from the requested ones. This does not prove package availability,
board electrical suitability or conflicts with undeclared hardware.

The existing NuttX I²C setup fixes pull-ups/open-drain. DevKit IRQ registration
fixes rising-edge/pull-down behavior required by the current GPIO adapter.
Additional electrical modes require native support and adapter qualification.
NuttX supports [configuration includes and fragments](https://nuttx.apache.org/docs/latest/quickstart/configuring.html#reference-configuration)
for board variations; extending Nxrs does not require a separate pinctrl runtime
or DTS parser. The normal build applies these profile selections to the selected
NuttX board configuration and retains effective-configuration verification.

The following fragment replaces the IMU portion of a complete CoreS3 profile.
Its existing board, target, GNSS and Kconfig configuration still applies.

~~~toml
[composition]
version = 1
mode = "hardware"

[resources.sensor_bus]
binding = "i2c0"
kconfig = { ESP32S3_I2C0_SDAPIN = 12, ESP32S3_I2C0_SCLPIN = 11 }
frequency = 400000

[devices.imu]
compatible = "bosch,bmi270"
bus = "sensor_bus"
address = 0x68

[devices.imu.wake]
kind = "periodic"

[imu]
stack-bytes = 16384
~~~

The `[hal] imu = "nuttx"` binding selects NuttX integration for this capability.
The device's `compatible` selects the sensor. The capability API owns ODR and
full-scale requests. The device is named `imu`, so firmware can access it through
`imu_devices::imu()`. This single-IMU profile needs no alias;
`imu_devices::primary_imu().open_default(sink)` acquires that same device.

Composition version 1 is the migration boundary for physical sensor profiles.
Move legacy IMU driver, address and wake settings into the device record,
and move bus speeds, BSP selection and pins into resources. Replace raw resource
`kind`/`device` declarations with a supported `binding` and its routing; remove
their duplicated Kconfig assignments. Move legacy GNSS UART selection and baud
settings into a UART resource. A sole IMU or GNSS device is
the default automatically; with multiple devices of a capability, select its
default through `[aliases]`. The `[imu]` and `[gnss]` tables can supply `stack-bytes`.
The resolver rejects unversioned sensor bindings and legacy physical fields
before SDK setup.

Existing profiles must rename `[roles]` to `[aliases]` and overlay
`remove-roles` to `remove-aliases`. Old keys fail with migration guidance.
The resolved graph reports aliases, including inferred single-device defaults.
Replace experimental `pins`, `pinctrl` and `gpio` fields with the binding's native
pin settings under the resource's `kconfig` table; retired fields fail with guidance.

For wired hardware, replace the whole wake table with `kind = "interrupt"` and
`resource = "imu_irq"`. On DevKit, use the `gpio_irq` resource declaration above;
the binding selects rising-edge registration and
`/dev/gpio2`. V1 admits the
existing interface shape with host witnesses. Additional polarities and masking modes
require provider support. A periodic wake derives its interval from the accepted
sensor ODR. An interrupt acquisition failure remains an error. The M135 stock
module uses periodic wakes because its interrupts are unwired.

Device names identify configured instances: `[devices.imu]` generates
`imu_devices::imu()`. Names use lowercase snake case; `self`, `super` and
`crate` are reserved. Each physical IMU has one canonical static object with its
own driver storage and ownership/fault record. `aliases.imu` selects
`imu_devices::primary_imu()` for default acquisition. Additional
`imu_<name>` aliases generate `imu_devices::<name>()` accessors:

```toml
[aliases]
imu = "imu"
imu_navigation = "imu" # alias of primary
imu_backup = "spare"     # separately declared device/endpoint
```

Names and alias accessors share one namespace. A name may refer to only one
device; redundant aliases for the same device are emitted once. `primary` is
the default accessor, so use `aliases.imu` to select it. Changing an alias in an
overlay preserves direct device names and never creates another device owner.

Applications import `imu::Device` and call `device.open(config, sink)` or
`device.open_default(sink)`. References do not acquire hardware. Different devices
may acquire independently; aliases share Busy/fault state until the prior worker,
terminal delivery, sink and resource cleanup retire. GNSS follows the same
ownership pattern through `gnss_devices::primary_gnss()` and
`gnss_<name>` aliases. The OS/resource adapter retains transaction serialization
for a shared bus. See the
[static-device contract](static-device-composition.md#permanent-device-objects-and-temporary-sessions)
for lifecycle, opacity and storage requirements.

## Bindings and overlays

A binding describes a supported compatible/bus combination, allowed addresses,
bus frequencies and readiness modes. IMU declarations live in each chip driver's
`bindings.toml`; supported sampling rates and ranges remain in the Rust driver.
Product profiles choose the actual bus, address and interrupt endpoint. NuttX
owns physical pin assignment and device registration.

Each package declares version 2 binding files in `package.metadata.nxrs.bindings`.
The IMU platform catalogue imports its drivers' declarations with `{ dependency = "nxrs-bmi270", feature = "bmi270" }`
and the corresponding LSM6DSO entry. Imports must name an existing normal Cargo
dependency with an explicit path; target tables and dependency aliases are
supported. Registry-only and workspace-inherited imports are not implemented.
The loader follows declared inputs, records their manifests and binding files,
and rejects cycles and ambiguous matches. It does not select Cargo dependencies.
GNSS imports its UBX driver declaration through `platform/devices/gnss`.
An import may name an additive Cargo feature declared by the catalogue; product
builds activate it before target compilation when the catalogue is consumed.

Build-only resolution selects a checked-in constructor. Profiles cannot supply
Rust paths or executable code. Unknown models and unsupported buses fail.
One implementation ID names a checked-in Rust type; profiles do not name code.
Resolution loads those declarations for explicit `[hal]` capabilities, including
`"none"` entries used to validate unselected devices. A physical device whose
capability is absent from `[hal]` has no admitted catalog and fails validation.
Build support has no compile-time inclusion of sibling HAL catalogs.
Profile inspection and build dry runs report the normalized graph, profile
sources, catalog paths and exact package input hashes under `resolved-composition`.

Use repeatable `--overlay` arguments with `cargo nxrs profile` and
`cargo nxrs build`, or declare an ordered `overlays` list in a named profile.
Resolution merges ancestor profiles, the child profile, the declared overlays
from ancestor to child, and then command-line overlays in argument order.
Overlay files contain changes only and cannot recursively include overlays or
change the target, BSP, execution OS or composition mode. Those changes require
a named profile. Declared paths resolve against the profile file. Command-line
paths resolve against the selected workspace root.

In the current v1 path, the build frontend writes `resolved-profile.json` into the owned build output
and passes its path to Cargo through `NXRS_RESOLVED_PROFILE`. HAL build scripts
verify the workspace, target, profile name and input fingerprints before using
it. Direct Cargo consumers select a named profile and its declared overlays
through `NXRS_PLATFORM` and `NXRS_SDK_ROOT`. Both sensor Cargo adapters call the
same shared preparation function. Their explicitly declared binding files select
checked-in implementations. Shared build support renders fixed typed I²C/SPI/UART
resource contracts, an implementation import and the validated stack budget.

The `resolved-composition` report includes ordered source hashes, field
provenance, a semantic digest of the normalized graph and target, and a separate
input fingerprint for the exact profile, overlay, manifest and catalog
bytes plus the resolver revision. `binding-inputs` preserves each declared
package's input paths and hashes, including both IMU and GNSS inputs.
Use this report to inspect the device and resource binding before a build.

Apply the following merge rules in order.

1. Merge named resource/device maps by ID and property. Scalars and arrays replace
   the preceding value. Explicit `remove-devices` and `remove-resources` lists
   remove existing IDs. Reject unknown removals and same-overlay remove/update.
2. A change to `compatible` replaces the entire device record. A change to resource
   `binding` replaces the entire resource record. Require all new mandatory fields.
   The wake table is always replaced as a unit. This prevents stale chip-specific
   settings and IRQ references from surviving a replacement.
3. Alias assignments replace explicitly. Remove an alias through `remove-aliases`.
   Removing its device alone leaves an invalid reference until repaired.
4. Validate the fully merged graph. Record override provenance, input content
   hashes and ordering. Intentional later overrides are deterministic. Malformed
   inputs and conflicting final resource claims fail with source locations.

An LSM6DSO overlay can replace the sensor while retaining the device name `imu`.
The NuttX binding supports periodic FIFO drains.
The driver reports its nominal rate as 104 Hz at the default setting.
The CoreS3 replacement overlay qualifies compilation only. A board with
the declared bus and replacement sensor is needed for physical measurements.

~~~toml
[devices.imu]
compatible = "st,lsm6dso"
bus = "sensor_bus"
address = 0x6a

[devices.imu.wake]
kind = "periodic"
~~~

This example becomes buildable only after LSM6DSO has its driver, IMU
implementation, binding and tests. Overlays describe the attached hardware.
They do not add support for an unimplemented device.

A GNSS alias references a receiver on a UART resource. The UART supplies its
NuttX device path and baud rate. The receiver compatible selects the reusable
UBX driver. The HAL owns event delivery and configuration rollback.

~~~toml
[resources.receiver_uart]
binding = "uart1"
kconfig = { ESP32S3_UART1_TXPIN = 17, ESP32S3_UART1_RXPIN = 18 }
baud = 38400

[devices.navigation]
compatible = "u-blox,neo-m9n"
bus = "receiver_uart"

[aliases]
gnss = "navigation"
~~~

Validate missing references, disabled/removed required devices, incompatible
alias assignments, duplicate physical endpoint/address claims, exclusive IRQ/CS
claims and invalid resource requirements. Shared I2C buses are valid. Conflicting
speed declarations for the same physical endpoint fail in v1. Any provider
dependencies used for initialization must be acyclic. Physical pin conflict
proof depends on the NuttX BSP's available metadata and target checks. A pathname
alone cannot establish electrical correctness.

Both IMU drivers also accept SPI through the same normalized device contract.
An SPI resource selects a supported BSP `binding`, native pin settings and `frequency`;
its device declares `chip-select` and `spi-mode`, with no I²C `address`. Driver
bindings admit modes 0/3 and 1/4/8 MHz. Duplicate path/chip-select claims fail,
including aliases of the same path. A selection index means NuttX
`SPIDEV_USER(index)`, whose physical mapping belongs to the BSP.

For a same-chip transport change, remove the old device, introduce the SPI
binding under a new ID and point `aliases.imu` at it. This uses the existing
overlay rules without implicitly discarding stale address fields. See the
[BMI270](../tests/nuttx-std/imu-qualification/overlays/bmi270-spi.toml) and
[LSM6DSO](../tests/nuttx-std/imu-qualification/overlays/lsm6dso-spi.toml) examples.
These DevKit overlays derive SPI2 registration and CS/SCK/MOSI/MISO pins
10/4/5/6. They use the BSP's fixed single CS (`SPIDEV_USER(0)`). CoreS3 rejects
these overlays because its bring-up does not register SPI. The acquisition loop
and consumer API are unchanged; physical SPI qualification remains pending.
The [SPI resource limitations](../driver/spi/README.md) still apply.

## Construction and Cargo

The following describes the implemented v1 handoff. The accepted
[shared preparation and additive driver design](static-device-composition.md#preparation-and-generated-input-handoff)
supersedes local OUT_DIR emission and unconditional driver dependencies as the
target for new work. Keep the v1 compatibility and qualification evidence until
its replacements are proven.

Resolve once to a typed device/resource graph in `nxrs-build-support`. Both the
CLI and shared Cargo adapter use that resolver. Emit inspectable resolved JSON and
a semantic configuration digest over the normalized graph, mode and target.
Keep an additional input fingerprint covering ordered source content hashes,
binding schemas and resolver/emitter revision in the build/cache identity.
Equivalent graphs share a semantic digest even if input spelling differs.
Input/order changes still invalidate source-derived outputs through the input
fingerprint. A common resource renderer generates canonical IMU instances and
accessors in the platform catalogue's OUT_DIR, and the default GNSS binding in
its HAL OUT_DIR.
It validates identifiers and escapes literal values.
There are no per-HAL emitters, Rust projection schemas or model-enum matches.

Cargo still resolves dependencies before running HAL build scripts. V1 keeps
portable driver crates as ordinary declared dependencies in their construction
owner (the platform catalogue for IMU, its HAL for GNSS), following existing target scoping where valid. Select the actual HAL
source and constructor at compile time. Unselected portable dependency artifacts
may still compile. Measure build cost separately from linked firmware size.
Build scripts cannot enable optional dependencies. Any later dependency-pruning
optimization must use the existing Cargo graph machinery and qualify workspace,
transitive and detached consumers before changing this rule.

Retain `NXRS_PLATFORM` for direct Cargo use and `NXRS_SDK_ROOT` for detached
consumers. Such consumers use a named profile with declared overlays. Ad hoc CLI
overlays are normalized into a per-build resolved-profile file under the owned
output directory, passed to HAL build scripts through the
`NXRS_RESOLVED_PROFILE` input. If combined with a named profile, identities must
agree. Validate the file version, both fingerprints and target by re-resolving
the recorded inputs. Retain all input paths for Cargo rebuild tracking. Write
atomically and never rewrite source profiles.
Equivalent input selections through either entry point must produce the same
typed graph and digest. Invalid/stale selections fail before native compilation.

Each HAL currently selects one default factory through the resolved provider. Contract-only
builds retain explicit unavailable factories. Test fixtures require an explicit
profile or test injection.
Selected exports remain named and factory-local. Generated wiring needs audit
coverage and cannot introduce OS branches into shared algorithms.

## Simulation and other operating systems

Simulation uses a separate profile with `composition.mode = "simulation"`.
Hardware profiles reject simulated sources. Simulation profiles reject physical
device claims. Mixed fault-injection images use separately classified test-only
qualification profiles with `mode = "qualification"`, admitted only for an
application marked `package.metadata.nxrs.qualification-only = true`. An overlay
cannot change the mode. Independent scripted fixtures keep their named qualification entry points.

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
deterministic. The event-demo supplies monotonic elapsed time to its pause,
resume and stop schedule. A focused test supplies logical times and compares
the ordered control decisions across different wake sequences. A test-only
harness also advances the seeded source, injects normalized device messages
through the IMU/GNSS service handlers, exercises pause/resume, and compares
FusionService transcripts and loss counters under reversed producer wake
orders. Its bounded sequencer admits messages in canonical EventKey order and
acknowledges each service result. Production service inboxes follow arrival
order, so this evidence covers canonical admission only. The normal simulation
workers remain wall-paced. Report deterministic source playback separately
from deterministic application behavior until independent worker scheduling
and end-to-end deadlines are qualified.
Existing independent mock-session tests remain explicit fixture tests.

The `simulation` host profile declares a seed and the IMU and GNSS endpoint IDs.
Run the existing navigation demo with `cargo nxrs build --app event-demo --profile simulation`
and then `target/build/event-demo/simulation/cargo/release/event-demo`.
One wall-paced logical clock starts after both endpoints register. They observe
the same seeded east–west trajectory. Each endpoint is exclusive and uses a
bounded drop-oldest queue with observable sequence gaps. A test harness can own
a `Simulation` directly and advance it in integer microseconds.
The independent scripted mocks remain available through the qualification
profiles. Determinism covers generated observations and their event keys.
The event-demo's services still use host scheduling.

A native macOS or Linux source may satisfy an alias directly through an OS API.
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
drivers implement a common API. Nxrs IMU drivers similarly implement a shared
synchronous device contract while retaining chip-specific APIs. HALs own managed
acquisition and resource lifetime. These are deliberate implementation
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
