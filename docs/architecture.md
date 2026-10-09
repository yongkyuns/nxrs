# Architecture and source ownership

The source hierarchy is app, service, hal, driver, and platform.

The normative HAL/resource ownership rules are in
[hal-platform-architecture.md](hal-platform-architecture.md). Physical NuttX
device access, including the Rust `std` boundary and private device-control
glue, is defined in [NuttX device access from Rust](nuttx-device-access.md).
The [roadmap](architecture-and-roadmap.md) describes remaining migrations.

The [concurrency and event communication design discussion](concurrency-event-communication.md)
records a proposed revision to active ownership and inter-service communication.
It is intentionally non-normative while the ownership boundaries, wait backend,
typed-message transport and bulk-data path are still being discussed.

## Applications

Each production firmware image selects exactly one app. An app owns an ordinary
handwritten Rust main(), product-level service composition, workflows, lifecycle
and policy.

The default dependency shape is:

~~~text
app -> service -> HAL capability facade
~~~

Applications and services are both portable layers. An app may access a
capability facade or API directly when the app itself is the natural
owner/consumer and introducing a service would not add a useful boundary.

What portable code must not do is select the concrete realization: physical
sensor model, mock/native/NuttX provider package, device driver, bus instance,
address, IRQ, or other platform-specific resource.

app/event-demo follows this rule. Its main creates ImuService,
GnssService and FusionService without naming any IMU/GNSS implementation.

app/dual-imu-demo is a separate ordinary firmware image that reuses the same
navigation services but creates two ImuService/FusionService pipelines. The two
service-owned IMU acquisitions are independent; app instances do not require a
global resource registry or application-defined HAL instance IDs.

app/nxrs follows the same provider-hiding rule for its native replay
profile. It acquires camera, storage and transport through `nxrs-camera`,
`nxrs-storage`, and `nxrs-transport`, then moves those resources into
CameraService, RecordingService and TelemetryService.
The architecture checker has no app-specific provider-selection exception.

There is no platform launcher, app registry, mandatory App trait, registration
macro or service-graph configuration language.

## Services

A service owns a standalone system function and the resources required to
implement it.

A service may be synchronous or may own an execution context. Active services
can intentionally own std threads, bounded channels, timing, shutdown and join
when those resources are part of their functionality.

When a capability primarily exists to implement a service, that service should
normally acquire it internally from the HAL at an explicit lifecycle boundary
such as start(). This is the preferred ownership pattern because it prevents
unnecessary resource sharing. It is not a blanket prohibition on direct
application access to portable HAL abstractions.

The navigation services are the first implementation of this rule:

~~~text
ImuService.start()
    -> HAL IMU acquisition
    -> owner thread owns IMU for its lifetime

GnssService.start()
    -> HAL GNSS acquisition
    -> owner thread owns GNSS for its lifetime
~~~

Their public constructors contain only service configuration and service
connections.

## Event-driven service communication

> **Current implementation, not settled target architecture.** The section below
> documents the existing event-demo topology and its qualification properties.
> In particular, the current IMU/GNSS-to-Fusion high-rate message paths should
> not be generalized into a rule that normal data flow crosses active-object
> queues. The proposed coarse ownership/control-plane model is discussed in
> [concurrency and event communication](concurrency-event-communication.md).

Active services use a **single-owner, single-inbox, run-to-completion** execution
model built on Rust `std` synchronization primitives.

The canonical active entity is:

~~~text
many producers
     |
     | cloned EventSender<Event>
     v
+-------------------------------+
| one bounded EventInbox<Event> |
+---------------+---------------+
                |
         one blocking wait
       recv / recv_timeout
                |
                v
         handle one event
         run to completion
                |
       emit bounded events
                |
                +----> same wait point
~~~

`service/event` is the small shared implementation of this rule. It is only a
thin ownership wrapper around `std::sync::mpsc::sync_channel`:

- `EventSender<E>` is cloneable so any number of producers can fan into one
  entity;
- `EventInbox<E>` is intentionally not cloneable, so one execution owner
  serializes state changes;
- every inbox has an explicit finite capacity;
- `wait(Option<Duration>)` is the canonical owner wait operation and maps
  directly to std `recv` / `recv_timeout` semantics;
- `send`, `try_send`, and `try_recv` remain available with their ordinary
  std semantics.

This is not an actor framework, service registry, executor, or global event bus.
Each service keeps its event enum private and exposes narrow typed endpoints or
domain methods.

For example, Fusion has one private event type:

~~~text
ImuService  -- ImuSample --+
                           |
GnssService -- GnssFix ----+--> one Fusion inbox --> Fusion state
                           |
lifecycle -----------------+
~~~

`ImuInput` and `GnssInput` are typed public endpoints, but both wrap clones
of the same private Fusion event sender. Fusion therefore never has to select
between several receivers.

### One wait point

Every active service should normally have exactly one logical blocking wait
point in its owner loop.

The owner always calls the same operation:

~~~text
inbox.wait(None)                         # no internal deadline
inbox.wait(Some(time_until_deadline))    # event or deadline
~~~

The wrapper uses std `recv` or `recv_timeout` underneath. A timeout is
converted into a private timer event and dispatched through the same
run-to-completion path. IMU and GNSS use this model for their acquisition
deadlines; commands and timer expiry do not require separate waits.

Future device readiness, callbacks, IRQ notifications, or browser completions
must ultimately wake the same owner loop rather than mutate service state from a
second execution context. Platform/HAL-specific readiness adaptation remains
below the portable service boundary.

### Run to completion

Once an event has been dequeued, the owner handles that event completely before
receiving the next event.

The handler may:

- mutate its private service state;
- call ordinary synchronous helper modules it owns;
- perform bounded local/HAL work;
- enqueue zero or more downstream events;
- update counters and deadlines.

An active-service handler should **not synchronously wait for another active
service**. A peer interaction that requires a later result should be expressed
as a request event followed by a later completion/reply event. This avoids
wait-for cycles and keeps every owner responsive to its own inbox.

Top-level application/control code may use acknowledged synchronous methods such
as `imu.pause()` or `imu.status()`; those methods enqueue a command event and
wait on a one-shot reply. The receiving service still processes that command at
its one wait point. Do not use the same blocking request/reply pattern from one
active service's event handler without an explicit deadlock/timing analysis.

### Bounded communication and overload

All active-service inboxes are bounded.

Run-to-completion handlers should normally use non-blocking `try_send` when
emitting data to another active service. The receiving service contract defines
what a full inbox means: drop-new, replace-stale, report overrun, or another
explicit bounded policy.

Blocking `send` is reserved for paths where waiting for admission is known to
be safe, such as top-level lifecycle/control code. A handler must not acquire an
unbounded dependency on downstream progress merely to publish data.

Different traffic classes may intentionally use different policies. High-rate
IMU samples can be dropped and counted when Fusion is saturated; shutdown or
configuration control generally requires a reliable path. Reliability does not
mean introducing an unbounded queue.

### Ordering and determinism

A single inbox gives one serialized mutation order for each service. Per-producer
message order follows the underlying std channel semantics, but semantically
independent producers may race.

If cross-source ordering matters, the protocol must encode the required ordering
using timestamps, sequence numbers, generations, priorities, or another explicit
domain rule. Sensor fusion in particular must not equate host-thread arrival
order with measurement-time order.

Determinism therefore comes from:

- one owner of mutable service state;
- one bounded inbox;
- one wait point;
- run-to-completion handlers;
- explicit message ordering semantics where needed;
- explicit overload behavior.

It does not come from assuming that the OS will schedule concurrent producers in
a repeatable global order.

### Synchronous code remains synchronous

Not every module is an active entity. Processing stages that share one ownership
and execution context should use ordinary Rust function calls rather than
channels. Message passing is for crossing an independently scheduled ownership
boundary, not for turning every type into an actor.

## HAL: capability-local facades

`hal/` is Nxrs's hardware/platform abstraction layer, but provider selection
is not centralized in a global HAL package.

Each capability owns:

~~~text
hal/<capability>/          public facade, e.g. nxrs-imu
hal/<capability>/api/      provider-independent contract
hal/<capability>/native/   optional provider where applicable
hal/<capability>/nuttx/    optional provider where applicable
hal/<capability>/mock/     explicit test/simulation provider
~~~

Portable app/service code depends on the capability facade or its contract, not
on provider packages. The facade is the only production layer allowed to select
a concrete provider for that capability.

For example:

~~~text
ImuService -> nxrs-imu
                -> nxrs-imu-api
                -> nxrs-imu-mock      # selected by build

app/nxrs -> nxrs-camera
                -> nxrs-camera-api
                -> nxrs-camera-native # selected by build
~~~

Provider selection is an explicit Cargo/deployment feature such as
`nxrs-imu/mock` or `nxrs-camera/native`. There is no global
`nxrs-hal`, no `hal/platform` package, and no giant `Hal` object.

The separate `api/` package remains useful because providers depend on that
contract. Putting provider dependencies into `api/` itself would create a
dependency cycle. The capability-root facade therefore re-exports the contract
and owns provider selection.

With no provider selected, provider-independent library code still compiles.
Capabilities that support explicit acquisition may report Unsupported or simply
omit provider-specific acquisition until a provider feature is selected. There
is never an implicit mock fallback.

Ordinary `std::time` is not a HAL capability. Services use it directly where
qualified; sensor timestamp domains remain explicit device/data contracts.

## Providers and drivers

A provider implements one capability. It may use repository-owned protocol
drivers, OS support, or pinned upstream NuttX drivers.

For a physical NuttX capability, the provider itself remains the Rust
implementation. Use qualified Rust `std` file/I/O/socket facilities directly
for ordinary descriptor operations. Device-specific controls such as NuttX
`ioctl` requests stay private to that provider and use only the minimum
target-header/ABI glue required. Do not add a generic POSIX/device translation
layer between the Rust HAL and NuttX. See
[NuttX device access from Rust](nuttx-device-access.md).

Concrete device identity belongs in the capability provider and deployment
configuration. BMI270, LSM6DSO, u-blox model selection, I2C/SPI/UART instances,
addresses, and IRQs must not appear in application or portable service policy.

Repository-owned low-level protocols belong in `driver/`. Existing physical
NuttX drivers can remain under `external/nuttx` when reused directly.

## platform/ directory

platform/ is target/build integration, not the HAL abstraction:

- platform/nuttx contains board/build profiles and Rust/NuttX link integration;
- it does not construct services;
- it does not run the application through a second launcher.

Changes to dependency sources live separately in `upstream/`, grouped by
dependency with their patches, revision pins and provenance. `external/`
remains the pinned upstream source; builds apply patches to archived copies.
See [upstream patchsets](upstream-patchsets.md) for the active series and
optional proposals.

The Cargo firmware frontend connects:

~~~text
application Cargo metadata
one product platform
~~~

through `cargo firmware --app <app> --platform <platform>`. The selected
product platform supplies capability-provider bindings plus execution
target/board/OS facts, while main() remains the sole source of service topology.
There is no separate checked-in deployment graph.

## Enforced production dependencies

tools/check-architecture.py inspects Cargo metadata without a platform filter,
so optional, target-conditional and build edges cannot evade layer rules.

| Source | Permitted production dependencies |
| --- | --- |
| app | services, capability facades/APIs |
| service | services, capability facades/APIs |
| HAL capability facade | its API plus optional selected providers |
| HAL API | portable APIs only |
| HAL provider | its capability API plus required drivers/support |
| HAL mock | its capability API |
| driver | APIs and lower drivers |
| platform/ build integration | lower layers, never apps |
| tests | production layers as required |

New apps may use portable HAL capability facades/APIs when justified, but cannot
depend directly on concrete providers. In particular, app/event-demo cannot
regain configured-imu/configured-gnss aliases that point directly at provider
packages.

HAL capability facades remain no_std and unsafe-free. Service libraries remain
unsafe-free but may use std for qualified execution resources. App binaries own
safe handwritten main() functions.

All Cargo packages are explicit workspace members. Target-only, destructive
qualification packages remain out of default members.

## Event-demo boundary

app/event-demo is the reference implementation for the target architecture.

Its production graph is:

~~~text
event-demo app
     |
navigation services
     |
nxrs-imu / nxrs-gnss
     |
build-selected capability providers
~~~

With the mock provider features selected, ImuService and GnssService acquire synthetic
providers internally. The app source and manifest do not know that.

Service tests also execute through the mock IMU/GNSS provider selection rather than injecting
TestImu/TestGnss constructors. This keeps production ownership and test
ownership aligned.

The same app/service graph is qualified on:

- native host execution;
- ESP32-S3 NuttX/QEMU;
- MPS2 AN521 ARMv8-M/Cortex-M33 NuttX/QEMU;
- Raspberry Pi Pico 2 board build/link/ABI.

Those deployments currently use the mock IMU/GNSS provider selection, so they establish
software portability rather than physical IMU/GNSS behavior.

See [event-driven-demo.md](event-driven-demo.md).

## Multi-app and multi-instance proof

There are now two distinct ordinary application binaries using
`nxrs-navigation-services`:

~~~text
app/event-demo       -> IMU + GNSS + fusion
app/dual-imu-demo    -> two independent IMU + fusion pipelines
~~~

Neither app depends on the other. The architecture checker continues to reject
app-to-app production dependencies.

The dual-IMU app proves that a capability may supply more than one resource
instance when its selected provider supports that operation: each
`ImuService::start()` calls `nxrs_imu::open()` and owns the returned
resource for its lifetime. Pausing one service does not affect the other.

Provider selection remains per firmware image. The IMU-only dual app is
qualified without `nxrs-gnss-mock`, while the event demo may select both
mock IMU and GNSS. Starting the dual app without an IMU provider is required to
fail Unsupported, so no configuration leaks or implicit mock fallback hide an
unsupported image.

The existing event-demo Cargo firmware builds separately prove that one
unchanged app/service graph builds/runs against several product-platform
profiles.

## Camera stack

The camera/recording/telemetry stack keeps its capability-first
camera/storage/transport split. For the native replay profile, app/nxrs now
acquires those capabilities through nxrs-camera/nxrs-storage/
nxrs-transport rather than naming provider packages directly, then transfers
ownership into the existing services.

The services remain generic over their capability contracts; this migration does
not add service factories or a runtime registry merely to hide construction.
Future NuttX/web bindings should use the same provider-hiding rule while
preserving capability-level build exclusion.

Existing native/NuttX/browser qualification remains valid within its documented
scope.

## Build policy

Use Cargo, NuttX Kconfig/menuconfig, board definitions and focused scripts.

Configuration chooses:

- application binary;
- one product platform;
- application command/resource settings.

The product platform owns provider features, execution target/board, and
target-specific configuration.

Handwritten Rust chooses service topology.

Do not add a generated app manifest, platform launcher, runtime device registry,
generic actor framework, compulsory executor, or xtask merely to represent this
architecture.
