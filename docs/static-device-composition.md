# Declarative builds and static device instances

**Status: accepted direction, November 8, 2026; migration in progress.**
[Issue #246](https://github.com/yongkyuns/nxrs/issues/246) tracks implementation.
This decision extends the [peripheral composition architecture](peripheral-composition.md)
and supersedes its HAL-local emission and singleton IMU construction approach.
The [follow-up implementation sequence](peripheral-composition-plan.md#static-device-follow-up)
defines the migration and evidence gates. Existing qualification results continue
to apply only to the revisions and configurations they measured.

## Decision

HAL packages own runtime contracts, implementations and declarative descriptions
of supported bindings. The shared build system resolves profiles and generates
validated configuration and instance declarations. Cargo features enable the
required implementations. Every configured physical device in the firmware's
active instance set has one canonical, statically allocated, opaque object with
its own configuration, persistent
instance storage and ownership/fault state. Roles refer to those objects.

Applications keep using capability contracts and logical roles. Fixed hardware
is bound to concrete implementations at build time. Multiple devices, including
multiple devices using the same implementation, are supported by construction.

## Terms

| Term | Meaning | Example |
| --- | --- | --- |
| Capability | A portable API with observations, requirements, errors and lifecycle semantics | IMU measurements and acquisition |
| Implementation | Runtime code that implements the capability using a chip driver or OS facility | BMI270 adapter using the reusable BMI270 driver |
| Resource | An OS/controller facility needed by an implementation | I2C controller, UART, GPIO interrupt or SPI chip select |
| Device instance | One configured physical endpoint with canonical identity and state | BMI270 at address 0x68 on the sensor bus |
| Role | A logical name that refers to a device for a product purpose | Default IMU, front IMU or backup IMU |
| Acquisition session | A temporary, exclusive owner of active use of a device | A running sample worker with stop/join operations |

A role is an alias, not an allocation. Two roles can refer to one device, but
cannot use aliases to bypass that device's ownership policy. Two identical chips
at different endpoints are two devices using one implementation.

The firmware's active instance set consists of configured physical devices for
capabilities both admitted by the profile and consumed in the target production
Cargo graph. Available devices in a full board profile must not add an unconsumed
HAL, driver, helper or static allocation to an application. Validate required
roles against the active set. D0 must specify treatment and validation of
unconsumed profile records without undeclared catalog dependencies. This rule
does not introduce a new profile field or a runtime device registry.

## First implemented slice: shared sensor preparation

IMU/GNSS output generation lives in shared build support. Each manifest declares
one version 2 binding catalog with compatible devices, resource constraints and
checked-in Rust implementation IDs. The shared renderer uses fixed typed resource
contracts and selects the implementation directly. Separate field-projection
schemas, generated model enums and runtime model matches have been removed.
Package discovery, receipts and Cargo rebuild tracking include the exact catalog
bytes. The reusable I²C resource adapter now lives in `driver/i2c`; IMU owns only its
sensor translation, acquisition and optional transaction timing.

The two HAL build scripts remain generic adapters for this slice. They preserve
package-specific provider flags, OUT_DIR delivery, simulation preparation and
native compilation/linking. Full pre-Cargo handoff, optional driver selection and
canonical static runtime instances remain later work. This stage changes host
preparation while preserving current single-device runtime behavior.

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
The device map already describes multiple records, but the IMU projection selects
only the default role. That is not runtime support for simultaneous IMUs.
A constant model match may optimize away; these source facts alone do not prove
that both drivers remain in the linked firmware.

## Source and build ownership

| Owner | Responsibilities |
| --- | --- |
| `hal/<capability>` | Public contract, normalization, acquisition/lifecycle policy, private runtime implementations, declarative binding/configuration schema and Cargo feature declarations |
| `driver/<device>` | Reusable chip protocol and chip-specific API over existing Rust I/O traits; no dependency on HALs, services or profile resolution |
| Provider-local `ffi/` | Mechanical ABI source for the consuming provider |
| Product profile and overlays | Physical devices, resource assignments, roles, requirements and budgets |
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
optional GPIO readiness and UART endpoints. Rust defines the configuration and
constructor types. Shared generation produces literal resource values and
`use implementations::<ID> as Implementation`; IDs are validated single type names,
not arbitrary Rust paths. Extending resource kinds belongs in shared tooling;
adding a sensor using an existing kind needs no generator model table.

Keep this finite construction convention as static instance generation is added.
Do not restore per-field Rust projection schemas, programmable TOML, templates or
per-HAL host callbacks. Cargo features enable implementations through declared
edges; they do not themselves name arbitrary Rust constructor paths.

Discover catalogs through explicit package-relative metadata. The production
dependency closure must contain its declared inputs. Shared support must not
embed another package's catalog using a hardcoded sibling `include_str!` path.
Profiles contain data and supported identifiers, never executable source paths.

### Preparation and generated-input handoff

Before compiling target crates, the common build path must:

1. Resolve the profile and ordered overlays with package-owned schemas; validate
   resources, devices, roles and requested capability requirements.
2. Derive the union of required implementation features for consumed components
   and request them through valid declared Cargo edges. Ask Cargo for the actual
   production graph using the real compiler, target and feature settings.
3. Generate typed configuration and one declaration per active device,
   with package-specific provider selection and explicit role aliases.
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
`hal_provider` value cannot represent mixed hardware, mock and unbound HALs.

Write generated files atomically outside source directories. Key outputs by the
resolved configuration, target, package/schema inputs and generator revision;
retain semantic and source fingerprints. Preserve content-stable files, reject
stale or mismatched inputs and isolate concurrent builds. Specify plain
`cargo build/check/test/doc`, no-profile contracts, explicit test fixtures and
detached-consumer behavior. No entry point may silently reuse the last product's
configuration. The implementation contract must define whether profile-aware
plain Cargo consumes an explicitly prepared artifact or another shared entry
mechanism; unprepared profile use must have a clear diagnostic.

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
| Two roles referring to one BMI270 | BMI270 | One object shared by both aliases |

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
invariants. Applications cannot manufacture a new ownership record for an
existing endpoint through a public constructor. Generated role accessors refer
to the canonical object. Avoid copyable `const DeviceSlot` values and statics
hidden inside generic functions: Rust defines one such generic-scope static,
not a separate allocation for each monomorphization.

Shared references to a device may be copied. The acquisition lease is exclusive
and non-Clone. Reacquiring the same device returns Busy while its previous use
is active or retiring. A different device can be acquired independently.
Fault/poison state belongs to the affected device, including partial startup,
IRQ registration rollback and IRQ unregister failures.

Keep the existing `Imu` session contract: cancellation, observable terminal
failure, dropped-event accounting, completion and explicit join. A permanent
device is a separate concept. Session Drop requests cancellation without
blocking; it must not free the lease while a worker or callback can still touch
hardware or instance state.

The exact reacquisition boundary must be specified before implementation.
Today's device cleanup can release its lease before the
[acquisition worker](https://github.com/yongkyuns/nxrs/blob/15f547594512528db7cae43a7bd1293ee54668e3/driver/acquisition/src/lib.rs)
finishes retrying terminal-event delivery. Retaining ownership until the entire
worker exits would strengthen that behavior. In either design, prove hardware
and callback quiescence and define ordering between the previous session's final
events and a new session. Do not equate release with dropping the user handle
or merely invoking join.

Opaque means private representation. It does not require `dyn` dispatch, a
vtable, heap boxing or a heterogeneous runtime registry. Private typed instances
and named accessors returning an opaque implementation can preserve static
dispatch. `Device`, `Instance<Backend>`, `primary()` and `.open(...)` are
illustrative names, not an approved public API signature. A uniform collection
of unlike devices needs a separately reviewed dispatch/storage choice. Preserve
the generic typed callback contract without silently boxing sinks.

The static-storage guarantee must cover the declared permanent instance state.
A static descriptor plus a lease atomic alone is not proof that driver state is
static. Inventory actual state, buffers and ownership. Worker stacks, session
queues and remaining OS/backend allocations need separate memory accounting;
this decision does not replace the std thread/event model with a heap-free runtime.

## Resource identity, sharing and enforcement

Canonicalize physical endpoint identity using the available resource metadata.
Two logical resource names for the same bus/address must not create independent
device owners. Reject duplicate device declarations for one endpoint; express
legitimate aliases as roles referring to one device. Detect exclusive IRQ/CS and
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
knows the capability, its requirements and the logical role. Switching hardware
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

Extend the typed resource convention to permanent device instances and settle
generated-input delivery, package-specific compiler/link integration, public
device/session API, static storage inventory, initialization/recovery policy and
reacquisition/terminal-event ordering. These choices must satisfy the invariants
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
