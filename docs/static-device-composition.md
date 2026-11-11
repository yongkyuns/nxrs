# Declarative builds and static device instances

**Status: accepted direction, November 8, 2026; migration in progress.**
[Issue #246](https://github.com/yongkyuns/nxrs/issues/246) tracks implementation.
This decision extends the [peripheral composition architecture](peripheral-composition.md)
and supersedes its HAL-local emission and singleton IMU construction approach.
The [follow-up implementation sequence](peripheral-composition-plan.md#static-device-follow-up)
defines the migration and evidence gates. Existing qualification results continue
to apply only to the revisions and configurations they measured.

## Decision

HAL packages own runtime contracts and acquisition. The platform composition crate owns
concrete construction and imports driver-owned supported binding declarations. The shared build system resolves profiles and generates
validated configuration and instance declarations. Cargo features enable the
required implementations. Every configured physical device in the firmware's
active instance set has one canonical, statically allocated, opaque object with
its own configuration, persistent
instance storage and ownership/fault state. Aliases refer to those objects.

Applications keep using capability contracts and logical aliases. Fixed hardware
is bound to concrete implementations at build time. Multiple devices, including
multiple devices using the same implementation, are supported by construction.

## Terms

| Term | Meaning | Example |
| --- | --- | --- |
| Capability | A portable API with observations, requirements, errors and lifecycle semantics | IMU measurements and acquisition |
| Implementation | Runtime code that implements the capability using a chip driver or OS facility | IMU acquisition using the BMI270 driver’s shared device contract |
| Resource | An OS/controller facility needed by an implementation | I2C controller, UART, GPIO interrupt or SPI chip select |
| Device instance | One configured physical endpoint with canonical identity and state | BMI270 at address 0x68 on the sensor bus |
| Alias | A logical name that refers to a device for a product purpose | Default IMU, front IMU or backup IMU |
| Acquisition session | A temporary, exclusive owner of active use of a device | A running sample worker with stop/join operations |

An alias refers to the existing allocation. Two aliases can refer to one device,
sharing that device's ownership policy. Two identical chips
at different endpoints are two devices using one implementation.

Resource bindings describe construction inputs and may resolve directly to Rust
std handles. Apply the [std-first interface choices](peripheral-composition.md#use-existing-rust-resource-interfaces)
when extending composition. Permanent device identity preserves peripheral
ownership; ordinary files and socket connections need no generated device object.
Preserve exclusive ownership where required even when the underlying handle is a
`File`. Profiles select supported BSP resources and native pin settings once;
build support derives NuttX configuration and Rust endpoint paths together.
NuttX owns MCU pin encoding, electrical setup, initialization and registration.
Nxrs checks declared pin claims in the BSP's native numbering namespace and
verifies effective NuttX configuration. Bindings, overlays and
aliases select their use without imposing an additional runtime access layer.

The firmware's active instance set consists of configured physical devices for
capabilities both admitted by the profile and consumed in the target production
Cargo graph. Available devices in a full board profile must not add an unconsumed
HAL, driver, helper or static allocation to an application. Validate required
aliases against the active set. D0 must specify treatment and validation of
unconsumed profile records without undeclared catalog dependencies. This rule
does not introduce a new profile field or a runtime device registry.

## First implemented slice: shared sensor preparation

IMU and GNSS use one generator in shared build support. The composition crate
imports version 2 binding declarations from each capability's chip drivers. Build support selects
bindings by compatible and bus, then emits typed resource bindings, one
instance per selected device, and alias accessors. Build support instantiates the types exported by the selected drivers. This follows
[Zephyr's devicetree instantiation pattern](https://docs.zephyrproject.org/latest/build/dts/howtos.html#write-device-drivers-using-devicetree-apis).
Package discovery, receipts and Cargo rebuild tracking include the exact catalog
bytes. The reusable I²C resource adapter now lives in `driver/i2c`; IMU owns only its
stream metadata, acquisition and optional transaction timing.

One shared Cargo adapter admits each consumed physical capability. The
composition crate uses additive capability features to keep their dependencies
independent. The build derives chip features before compilation and generates
one canonical instance per physical record with named alias accessors. GNSS preserves
receiver/serial rollback and per-instance quarantine through sink retirement.

### Implemented IMU device access

`composition::imu::<device_name>()` returns the configured device directly.
`aliases.imu` selects `composition::imu::primary_imu()`; `imu_<alias>` adds another accessor in
`imu_devices`. All return `&'static impl imu::Device` for the same canonical
object. A single IMU needs no alias; multiple IMUs require an explicit default.
Accessor name collisions between different devices are rejected. `Device::open`
accepts configuration and a typed sink; `open_default` uses the driver's default.
The result is an exclusive `Session`, separate from permanent identity. Consumers acquire through these device methods. The explicit mock fixture has primary and
secondary devices; coordinated simulation has primary only. Portable services
can receive the selected reference through `ImuService::start_on(device)`.

Each NuttX instance owns resource-opening and initialization callbacks and a
`Mutex<Option<device::Device<S>>>`, plus a private `DeviceState` recording
`Available`, `InUse` or `Faulted`.
The driver initializes at acquisition and remains in that static slot while
active. Retirement destroys the sink, cleans up/drops the driver and resource
handles, then drops its `DeviceUseGuard` to make the device available again.
Cancellation setup or thread-start failure also retires the slot. Failed cleanup
keeps that device faulted until restart.
Two aliases remain Busy through terminal delivery; independent devices can run
concurrently with different settings. No runtime device registry is introduced.

Host witnesses cover same-model/mixed pairs, aliases, independent faults,
backpressure, dropped sessions and IRQ cleanup isolation. Same-output Cargo
rebuilds exercise single/multiple instances and all four supported sensor/bus
bindings. Target compile/link evidence is separate from physical qualification.

## Current implementation and migration boundary

The baseline inspected for this decision is
[`15f547594512528db7cae43a7bd1293ee54668e3`](https://github.com/yongkyuns/nxrs/commit/15f547594512528db7cae43a7bd1293ee54668e3).
Recheck the implementation branch before changing it.

| Area | Baseline | Accepted target |
| --- | --- | --- |
| HAL host work | Local `build.rs`, IMU `build/emit.rs`, GNSS inline emission | Declarative HAL metadata processed by shared build tooling |
| Resolver | Capability/model-specific projections and sibling binding-file inclusions | Package-discovered schemas and generic validated rendering |
| IMU dependencies | Both sensor drivers are unconditional NuttX dependencies | Ordinary optional dependencies selected for required implementations |
| IMU construction | One generated `MODEL`, `BINDING` and `STACK_BYTES`, then a model match | Concrete binding and instance declaration for each configured device |
| IMU ownership | One global `OWNED`, including IRQ failure paths | Ownership and fault state per canonical device |
| Public lifetime | `Imu` represents an active acquisition session | Permanent device identity plus the existing session lifecycle |

See the baseline [IMU build script](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/build.rs),
[emitter](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/build/emit.rs), [manifest](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/Cargo.toml),
[sensor modules](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/src/sensors/mod.rs),
[NuttX factory](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/src/platform/nuttx/mod.rs),
[device owner](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/src/platform/nuttx/device.rs),
[IRQ owner](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/src/platform/nuttx/irq.rs),
[session API](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/hal/imu/src/api.rs), and
[composition resolver](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/platform/build-support/src/composition.rs).
At that baseline, the IMU projection selected only the default alias despite the
device map allowing multiple records. The current IMU generator and runtime
implement simultaneous named instances; the table retains the migration baseline.
A constant model match may optimize away; these source facts alone do not prove
that both drivers remain in the linked firmware.

## Source and build ownership

| Owner | Responsibilities |
| --- | --- |
| `hal/<capability>` | Managed public contract, acquisition/lifecycle policy and OS resource integration |
| `platform/` composition crate | Portable named accessors, concrete driver/resource wiring, canonical static declarations and their Cargo dependencies |
| `driver/<device>` | Reusable chip protocol, supported settings/resource bindings, chip-specific API and direct implementation of shared device contracts; no dependency on HALs, services or profile resolution |
| `driver/peripheral` | Synchronous per-peripheral contracts; no resource construction or acquisition runtime |
| Provider-local `ffi/` | Mechanical ABI source for the consuming provider |
| Product profile and overlays | Physical devices, BSP resource/pin assignments, aliases, requirements and budgets |
| BSP resource mapping | Supported registrations, native pin symbols/numbering, fixed reservations, endpoint paths and required Kconfig |
| Shared build support/controller | Schema discovery, resolution, validation, generic Rust generation, feature requests, target/native compilation orchestration and provenance |
| Cargo | Software dependency and feature resolution |
| OS/BSP | Controller registration, physical wiring, kernel facilities and qualified transaction behavior |

Remove custom HAL-local host emitters, `build/` generation modules and local
`build.rs` orchestration as capabilities migrate. Runtime implementations and
native ABI source remain with their owners. Moving C/Objective-C compilation
does not move the source or device policy into `platform/`.

Extend the existing pre-Cargo `configure_profile` path in the build controller.
Do not add a second deployment graph, generated application manifest, launcher,
arbitrary build plugin system or service/capability solver.

### Declarative configuration contract

Binding catalogs describe supported device/resource combinations and one reviewed
implementation ID. The implemented resource vocabulary covers I²C endpoints with
optional GPIO readiness, SPI chip-select endpoints and UART endpoints. Rust defines the configuration and
constructor types. Shared generation produces literal resource values and
checked-in implementation selections; IDs are validated single type names,
not arbitrary Rust paths. Extending resource kinds belongs in shared tooling;
adding a sensor using an existing kind needs no generator model table.

Keep this finite construction convention as static instance generation is added.
Do not restore per-field Rust projection schemas, programmable TOML, templates or
per-HAL host callbacks. Cargo features enable implementations through declared
edges; they do not themselves name arbitrary Rust constructor paths.

The SDK's `workspace.metadata.nxrs.device-catalogue` locates the composition
crate. Its renamed optional NuttX path dependencies register drivers. The
dependency alias supplies both the Rust crate name and the Cargo feature.
The driver declares its capability and binding files. The generator qualifies
the binding's exported type with that alias and supplies the resource adapter
types. The [composition README](../platform/README.md) shows the registration.
The production dependency closure must contain its declared inputs. Shared support must not
embed another package's catalog using a hardcoded sibling `include_str!` path.
Profiles contain data and supported identifiers, never executable source paths.

### Preparation and generated-input handoff

Before compiling target crates, the common build path must:

1. Resolve the profile and ordered overlays with package-owned schemas; validate
   resources, devices, aliases and requested capability requirements.
2. Derive the union of required implementation features for consumed components
   and request them through valid declared Cargo edges. Ask Cargo for the actual
   production graph using the real compiler, target and feature settings.
3. Generate typed configuration and one declaration per active device,
   with package-specific provider selection and explicit aliases.
4. Compile the consumed packages' required native helpers against the configured
   target headers/SDK and supply the correct link inputs.
5. Compile through Cargo and record selected packages, generated-input identity
   and final image evidence.

The integration must preserve features, aliases, dependency defaults, workspace
inheritance and detached app-to-service-to-HAL consumers. A build script cannot
enable a dependency after Cargo resolution, and a controller cannot arbitrarily
toggle an unreachable transitive dependency feature. Use legitimate dependency
edges or forwarding owned below the portable application/service policy boundary;
reject unsupported compositions with actionable diagnostics. Do not silently
rewrite application manifests or recreate Cargo's resolver.

Removing HAL build scripts also removes their package-local `OUT_DIR`,
`rustc-cfg`, `rustc-env` and native link emission. The implementation must
supply a concrete replacement for all of them before deleting the old path.
An application build script cannot broadcast its outputs into dependency crates.
Provider selection must retain per-package scope: a single global
`platform` value cannot represent mixed hardware, mock and unbound HALs.

Write generated files atomically outside source directories. Key outputs by the
resolved configuration, target, package/schema inputs and generator revision;
retain semantic and source fingerprints. Preserve content-stable files, reject
stale or mismatched inputs and isolate concurrent builds. NuttX profile builds
use `cargo nxrs build`. Underlying Cargo adapters and editor checks consume its
validated receipt and prepared native resource archives; they do not resolve a
board profile from its name alone or compile resource fallbacks. Ordinary
Cargo remains available for portable contracts and explicit host fixtures. Apply
the same rules to detached consumers. No entry point may silently reuse the last
product's configuration.

Native compilation still follows the consumed Cargo graph and required features.
Retain target compiler/flags/header provenance, freshness, archive ownership and
unconsumed-helper exclusion. Central orchestration is not permission to compile
every helper mentioned by a profile.

## Implementation availability and static selection

Declare drivers as optional Cargo dependencies and gate each coherent sensor
implementation once at its normal Rust module boundary:

~~~rust
#[cfg(feature = "bmi270")]
mod bmi270;

#[cfg(feature = "lsm6dso")]
mod lsm6dso;
~~~

These modules have different names and can coexist. Each implements the common
contract for its own concrete type. Internal names in separate modules do not
collide. Do not implement the same trait twice for one shared concrete type.
The existing OS/provider-level `selected` module remains a separate concern.

A binding may request `cargo_features`; Rust module declarations already map
those features to source files. Do not add a redundant binding `source` field,
a generated file-selection module or generated boolean aliases for the same
choice. Avoid feature conditions scattered through methods and algorithms.

| Configured hardware | Implementations enabled | Physical objects |
| --- | --- | --- |
| One BMI270 | BMI270 | One |
| Two BMI270s at distinct endpoints | BMI270 | Two independent objects |
| BMI270 and LSM6DSO | BMI270 and LSM6DSO | Two independent objects |
| Two aliases referring to one BMI270 | BMI270 | One object shared by both aliases |

Features are additive implementation availability, not mutually exclusive
whole-HAL selection and not a feature per instance. Runtime `match MODEL`
selection is unnecessary for fixed hardware; each generated instance binds
concretely at build time. Addresses remain ordinary instance configuration
unless a separate type-level need justifies otherwise.

Runtime hardware detection is an explicit opt-in policy with a declared candidate
set. Enabling two implementations for two known devices must not enable detection.
The existing `probe` feature means instrumentation/qualification, not discovery.

For a minimal production profile, verify that unused driver packages are absent
from the target dependency graph, then inspect linked code and data. Account for
Cargo feature unification and intentional evaluation builds. Do not claim size or
performance gains without measurements; a smaller selected graph is only one
part of the evidence.

## Permanent device objects and temporary sessions

Generate one actual `static` object for every device in the active instance set. Its
private storage contains immutable identity/configuration, persistent instance
state and an ownership/fault record. Initialize hardware at boot or first
acquisition according to the documented policy; compile-time allocation does
not perform I/O.

Common HAL instance machinery must enforce construction and acquisition
invariants. Applications use catalogue accessors. The HAL construction API is exposed for
platform wiring and qualification; architecture checks reject its use in app or
service code. External consumers must honor the same canonical-instance requirement. Generated alias accessors refer
to the canonical object. Avoid copyable `const DeviceSlot` values and statics
hidden inside generic functions: Rust defines one such generic-scope static,
not a separate allocation for each monomorphization.

Shared references to a device may be copied. The acquisition lease is exclusive
and non-Clone. Reacquiring the same device returns Busy while its previous use
is active or retiring. A different device can be acquired independently.
Fault/poison state belongs to the affected device, including partial startup,
IRQ registration rollback and IRQ unregister failures.

Keep the `imu::Session` contract:
cancellation, observable terminal failure, dropped-event accounting, completion
and explicit join. A permanent device is a separate concept. Session Drop
requests cancellation without
blocking; it must not free the lease while a worker or callback can still touch
hardware or instance state.

IMU retains ownership through terminal-event delivery and sink destruction, then
destroys its driver/resources before releasing the lease. A later acquisition
cannot overlap the previous session's callbacks or final events. Session Drop
requests cancellation; it does not establish that retirement has completed.

Opaque means private representation. It does not require `dyn` dispatch, a
vtable, heap boxing or a heterogeneous runtime registry. Private typed instances
and named accessors returning an opaque implementation can preserve static
dispatch. The implemented IMU API is `Device`, `composition::imu::primary_imu()` and
`composition::imu::<name>()`; its `Instance<S, R>` fields stay private. A uniform collection
of unlike devices needs a separately reviewed dispatch/storage choice. Preserve
the generic typed callback contract without silently boxing sinks.

The static-storage guarantee must cover the declared permanent instance state.
A static descriptor plus a lease atomic alone is not proof that driver state is
static. Inventory actual state, buffers and ownership. Worker stacks, session
queues and remaining OS/backend allocations need separate memory accounting;
this decision does not replace the std thread/event model with a heap-free runtime.
IMU stores its concrete driver, applied settings, readiness state, tracing and
lease in the static slot. Initialization temporaries and transaction scratch
buffers use stack space; threads, cancellation Arcs and native descriptors retain
their existing allocation costs. Typed resource-opening and initialization callbacks run
once per acquisition; sample processing and ordinary sinks use static dispatch.

## Resource identity, sharing and enforcement

Canonicalize physical endpoint identity using the available resource metadata.
Two logical resource names for the same bus/address must not create independent
device owners. Reject duplicate device declarations for one endpoint; express
legitimate aliases as aliases referring to one device. Detect exclusive IRQ/CS and
pin claims where metadata supports that proof, including console/UART conflicts.
State the limits when BSP metadata or path aliases cannot establish identity.

| Resource | Ownership and sharing policy |
| --- | --- |
| UART | Normally one protocol-session owner per port; sharing requires an explicit multiplexer |
| I2C | Share controller transactions; keep one configuration/acquisition owner per bus/address |
| SPI | Own the device/chip select; preserve bus configuration, select and transfers for the whole transaction |
| GPIO/IRQ | Own configuration and notification lifetime; share observations only through explicit supported semantics |

Device exclusivity is not whole-bus exclusivity. Verify the OS/controller's
transaction serialization before adding a Rust mutex. Prefer qualified existing
reservation and transaction facilities. Descriptor lifetime, physical resource
reservation, device/session ownership and transaction atomicity are separate
guarantees. Rust cooperative ownership does not exclude arbitrary native code,
another process or another independently linked image.

Preserve rollback, quarantine, cancellable waits, bounded typed delivery and
retained terminal faults. ISR code only performs qualified bounded notification;
no allocation, service callbacks or std/Crossbeam channel operations in ISR
context. Shared bus extraction still requires real consumers of the same
protocol; this work does not add a generic resource manager.

## Portability and the Zephyr comparison

Product configuration knows the chip and endpoint. Application/service logic
knows the capability, its requirements and the logical alias. Switching hardware
changes configuration and the selected implementation while preserving portable
policy. The HAL must report accepted rate/range/timing and reject unsupported
requirements; a common API does not make different sensors physically identical.
Coordinate frames, calibration and timestamp meaning remain explicit contracts.

Zephyr separates software support in Kconfig from hardware instances in
devicetree. Its device model holds build-time configuration and per-instance
runtime data, and applications can use common subsystem APIs and logical
devicetree aliases. Nxrs borrows the separation and permanent physical identity.
See [devicetree versus Kconfig](https://docs.zephyrproject.org/latest/build/dts/dt-vs-kconfig.html)
and the [device driver model](https://docs.zephyrproject.org/latest/kernel/drivers/index.html).

Zephyr's shared device pointer does not itself provide a Rust exclusive lease.
Its common driver APIs normally dispatch through function pointers. Devicetree
generation supplies macros; driver code still maps properties into its private
configuration/data structures. Nxrs need not copy those mechanisms to obtain
static identity and portable capability use. See
[devicetree API usage](https://docs.zephyrproject.org/latest/build/dts/api-usage.html).

Source portability, identical firmware binaries and support across operating
systems are separate properties. This decision claims a portable source/API
boundary, with product-specific compilation. QNX's userspace driver model remains
comparative context; it does not establish NuttX ownership, ABI or timing.

## Remaining design choices and completion

Complete generated-input delivery, package-specific compiler/link integration,
optional driver activation and extension beyond IMU. These choices must satisfy the invariants
above; none authorizes a new runtime registry or a per-HAL host emitter.

Start with IMU, prove same-model and mixed-model instances, then use GNSS as a
second capability to prove the generic build path. Preserve existing sensor
defaults, simulation/qualification separation and portable service behavior.
Coordinate with [PR #299](https://github.com/yongkyuns/nxrs/pull/299), which changes
NuttX resource bindings and GNSS port ownership; recheck its final merge state
before moving those paths. Keep unrelated camera/radio migrations separately scoped.

The follow-up plan and issue require negative ownership/alias cases, failure
isolation, shared-bus witnesses, Cargo graph and binary evidence, fresh/concurrent
generation and detached-consumer tests. Existing architecture tests that require
the local IMU emitter or global model match must be replaced with checks for the
new invariants in the implementation PR, without weakening dependency, native
boundary or lifecycle coverage.

## Language and build references

- [Cargo features and optional dependencies](https://doc.rust-lang.org/cargo/reference/features.html)
- [Cargo build-script scope and outputs](https://doc.rust-lang.org/cargo/reference/build-scripts.html)
- [Rust static allocation and generic-scope rules](https://doc.rust-lang.org/reference/items/static-items.html)
