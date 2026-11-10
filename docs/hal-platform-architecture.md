# HAL and platform architecture

Each capability is one Rust crate. Applications use `wifi`, `ble`, `led`,
`imu`, `gnss`, `camera`, `storage`, `config` and `transport`; Cargo package names
retain the `nxrs-` prefix.

## Capability boundaries and execution

`hal/` owns capability/subsystem APIs and their implementations, rather than
vendor register HALs. Consumers use portable contracts; private implementations
may use their native OS or SDK. Native code does not all belong in `driver/`.

| Responsibility | Owner |
| --- | --- |
| Ordinary files, sockets, threads, synchronization and time | Rust `std`, used directly |
| Reusable chip protocols and hardware-resource adapters | `driver/<device-or-resource>` |
| Managed capability behavior and private OS/SDK adapters | Owning `hal/<capability>` |
| Shared acquisition worker, cancellation and readiness mechanisms | `hal/acquisition`, an implementation helper with no profile binding |
| Device selection, resource bindings and execution budgets | Product profiles and build wiring |

Use a capability-specific contract with explicit supported configuration,
buffer ownership, completion, cancellation and failure semantics. Normalization
does not imply equal hardware capabilities or a universal device API.
`peripheral::imu::Imu` supplies synchronous device operations; `imu::Session`
and `gnss::Session` describe active acquisition lifetimes. `gnss::Gnss` remains an alias for existing GNSS consumers.

A component does not automatically require a thread or queue. A driver may own
execution when hardware or blocking operations require it; current IMU chip
contracts create none. `nxrs-acquisition` shares implementation code and starts
one worker per explicit call, not a shared executor. Shared scheduling requires
bounded device work, fairness and cancellation/deadline qualification. Application
and service execution are unchanged by this foundation cleanup.

The implemented [peripheral composition design](peripheral-composition.md)
defines reusable sensor-specific drivers, embedded-hal resource adapters and
validated device/alias bindings in profiles. The
[implementation plan](peripheral-composition-plan.md) records the migration
and its qualification evidence. Composition version 1 and ordered overlays
resolve the supported hardware bindings for physical IMU and GNSS aliases. Named
test and qualification profiles can select explicit mock bindings.

## Accepted follow-up: declarative builds and static devices

The [static-device decision](static-device-composition.md) is the accepted target
for new peripheral composition work. Its implementation remains open in
[issue #246](https://github.com/yongkyuns/nxrs/issues/246). The descriptions below
of HAL-local build scripts and default sensor factories document the current
baseline; they are not requirements to preserve those mechanisms.

The target keeps managed capability APIs, acquisition and native ABI source in
their owning HALs. Shared synchronous device normalization belongs to driver
implementations of the contracts in `driver/peripheral`. HAL build requirements become declarative.
Shared tooling owns generic generation, package-specific provider selection,
native compilation/link integration and input freshness. Cargo owns ordinary
optional driver dependencies. Gate uniquely named implementation modules once
with additive features; the existing OS/provider `selected` boundary does not
require sensor implementations to be mutually exclusive.

An active physical device has one permanent static allocation with private
configuration, instance storage and ownership/fault state. Aliases refer to that
object, and acquisition creates a separate exclusive session. Common HAL
machinery must enforce uniqueness and quiescent release. Different devices can
run independently, including two of the same model; aliases cannot create new
ownership guards. IMU implements this through `imu::Device` references returned by
`imu_devices::primary_imu()` and profile-defined `imu_devices::<name>()`; acquisition uses
`Device::open` or `Device::open_default`. Its worker retains storage and ownership through final
delivery, sink destruction and resource cleanup. GNSS retains its default owner.

Opaque access does not require dynamic dispatch or heap boxing. IMU's named
accessors preserve concrete driver and sink types. Generated-input transport and
optional-driver activation remain in the [follow-up plan](peripheral-composition-plan.md#static-device-follow-up).
Static instance storage and remaining thread/queue/backend allocation costs must
be reported separately. Existing lifecycle, native-boundary and portability
requirements continue to apply throughout migration.

Resource adapters belong in `driver/<bus>`. `driver/i2c` implements
`embedded_hal::i2c::I2c` for NuttX only and owns its native glue; IMU supplies
resolved configuration and keeps acquisition and stream metadata policy.
`driver/gpio` similarly owns NuttX interrupt registration and embedded-hal pin
reads. Its task-context cleanup-failure hook lets the HAL retain quarantine even
when setup or Drop fails. Shared `nxrs-acquisition` support owns the NuttX poll/pipe
binding used by IMU and GNSS; it supplies mechanisms, not sampling policy.
`driver/spi` provides bounded, single-CS `SpiDevice` transactions through the
NuttX SPI character ABI. Chip drivers depend on bus traits, not these concrete adapters. There are no
host bus backends, unavailable stubs or additional logical profile aliases.
IMU drivers own version 2 `bindings.toml` declarations for supported resource
combinations and checked-in implementation IDs. Platform device catalogues import them through
declared normal Cargo path dependencies; IMU runtime has no concrete sensor dependency. Product profiles select actual wiring;
shared tooling emits typed resource bindings and a selected factory implementation.
For composed NuttX peripherals, BSP mappings derive required Kconfig and endpoint
paths from the same profile resource selection. Resource `kconfig` tables name
native pin symbols, with conflicts checked in the BSP's declared native namespace.
NuttX owns port/mux encodings, electrical setup and numeric constraints.
Explicit Kconfig cannot contradict
those selections, and the effective NuttX configuration is checked after resolution.

## Required implementation boundaries

The capability API and implementation are Rust. Rust owns validation, resource
ownership, queues, synchronization, state transitions, retries, cancellation,
recovery, deadlines, and error policy. Each capability owns its errors; shared
delivery contracts do not imply a shared device-error enum. Generic channels and
owned delivery results live in `shared/messaging`, below HAL and services. A
producer accepts a typed callback; it never depends on a service inbox type.

Use ordinary qualified `std::fs`, `std::io`, `std::net`, `std::thread`, and
`std::sync` operations directly wherever available. One standard-library
implementation can serve several platforms. Do not wrap normal POSIX file,
socket, or thread operations in a second C adapter.

Private C/Objective-C is restricted to mechanical target-header ABI translation
and native SDK calls that Rust/std cannot directly express. Constructing a
device-specific ioctl argument or registering a native callback can require
such glue. Workflow sequencing, event queues, retry loops, connection recovery,
and storage rollback belong in Rust. A fixed-width scalar C API is not by itself
evidence of a thin binding.

Rust modules call one another through typed Rust functions or private traits.
Do not route Rust-to-Rust calls through C exports, opaque context pointers,
numeric status protocols or raw command/event layouts. Native error values are
translated at the SDK boundary. C-facing adapters used only by SDK witnesses
live under `tests/host/radio_shims`, enabled only in those test builds; they do
not determine the production provider interface. Actual native callbacks keep
the ABI and lifetime rules required by their SDK.

~~~text
capability-owned Rust API and shared behavior
                    |
          selected Rust platform provider
                    |
       std facilities / minimal private ABI or SDK binding
                    |
             platform implementation
~~~

Platform/provider flags are allowed at build selection and module/factory
wiring boundaries. Shared APIs, controllers, workers, and I/O algorithms must
not branch on `target_os`, `hal_provider`, or platform environment
variables. Rust model features select model code, never an OS.
Each HAL's `src/platform/mod.rs` selects source modules as `selected` and
re-exports only selected items. Prefer fixed named factory exports; do not
require a wildcard or let backend exports implicitly define the crate's public
API. Keep constructors and unavailable stubs in those selected modules, not
inline in the selector. Prefer ordinary named modules and conditional
`as selected` exports; source-path redirection is not required for selection.
Group each implementation's declaration and alias in one `cfg_if` branch rather
than repeating its selection condition. A branch may select a shared crate
module directly; do not add OS wrappers that only re-export it.
Identical Rust implementations remain ordinary shared modules outside
`platform/`; selecting acquisition does not duplicate them per OS. Native source
injection for host unit tests belongs in separate test wiring. Test private
behavior through its owning modules where possible, replacing native operations
at that boundary rather than recompiling the API and acquisition sources.
Cross-crate resource witnesses may retain explicit source injection under tests.
All HAL selectors
now name their exports explicitly; wildcard exports obscure implementation boundaries.

Platform factories supply resource budgets such as worker stack size. Providers
report capabilities; shared behavior does not infer them from the OS name.
Ordinary `cfg(test)` and language/ABI requirements inside a platform provider
remain appropriate.

Native callback and DMA ownership still require explicit lifetime proofs. A
refactor must preserve cancellation, quiescence barriers, bounded overload,
sticky failures, and release ordering. Retain tests of those guarantees when
moving behavior across an FFI boundary.

Legacy core-only fixture implementations belong under `tests/`, not in
production HALs. Radio behavior now lives in the owning Rust providers, including
NetworkManager/BlueZ schemas, station setup, discovery, bounded queues and
retirement. `hal/ffi-boundaries.toml` inventories only remaining ABI glue; no
radio migration exceptions remain. The architecture audit checks that inventory
and rejects platform selection in shared Rust modules.
Inventory checks support review; they cannot prove that C code is mechanical.

## Strict portability above the HAL

Applications own their ordinary `main()` and workflows. App and service source,
manifests and build scripts contain no OS/board cfgs, native imports, device
paths or platform inspection. Apps may select named devices through portable
catalogues under `platform/devices/<capability>`; services receive HAL references.
All other app/service dependencies on platform code remain forbidden. Catalogue
construction depends on HAL runtime and concrete drivers, never the reverse.
`imu::construction` is reserved for catalogue wiring and qualification, with
architecture checks rejecting app/service use. Unbound HAL factories keep
the same API and return `Unsupported`; applications never select a provider to
compile. The architecture audit rejects these violations without app exceptions.

Platform code owns target configuration, driver registration and generic build/
entry wiring. It receives an application descriptor as input and contains no
application identities, runtime crates or command-specific branches. Profiles
supply resource configuration; capability HALs consume the applicable data.
Console byte-stream ownership belongs to the console HAL; calling-thread
scheduling belongs to the execution HAL. Standard Rust still owns files,
sockets, thread creation, waiting and joins. Device-specific qualification stays
under `tests/`.

## One platform selection

The developer selects the application and a complete product profile. For
example, `pico2-mock` is an explicit NuttX qualification profile with mock IMU
and GNSS providers.

~~~sh
cargo nxrs build --app event-demo --profile pico2-mock
~~~

The platform owns the OS, board, HAL bindings, native drivers and resource
configuration. Rust `std` is available on production targets; it is not a
platform or a selectable HAL implementation. Application/service manifests do not choose HAL providers.
There are no per-capability Cargo provider features.

For example, `platform/nuttx/platforms/pico2w-wifi.toml` binds Wi-Fi to the
NuttX implementation and configures the CYW43439 driver. The ESP32-S3 profile
uses the same Rust implementation and different native driver configuration.
Do not duplicate Rust adapters merely because the boards differ.

For desktop/test builds, select one profile for the entire Cargo invocation.
`mock` is an explicit portable fixture profile, `host-demo` selects mock demo
sensors, and `simulation` selects the coordinated seeded IMU/GNSS source.

~~~sh
NXRS_PLATFORM=linux cargo test --locked -p nxrs-native-integration
NXRS_PLATFORM=mock cargo test --locked -p nxrs-simulator
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo
~~~

Explicit platform builds use the shared Rust profile resolver.
A build without `NXRS_PLATFORM`
compiles portable contracts/common code, with no implicit hardware or mock.
Explicit software functionality, such as camera file replay, may be requested
through Cargo without hardware admission; it does not select a physical backend.
The additive `recording` and `udp` Cargo features expose `storage::recording`
and `transport::udp` without selecting a hardware provider. They have no OS
whitelist or default feature. The old profile-dependent storage/transport root
factories and software type aliases have been removed. Synchronous NuttX record
files use the fixed `storage::create_record_file(path, max_records, max_frame_bytes)`
factory, which returns `Unsupported` when that acquisition is unbound. Explicit
`MockStorage` and `MockTransport` fixture constructors remain separate from real
file/socket operations.

`NXRS_PLATFORM=macos` selects desktop Wi-Fi, BLE scanning and GATT as well as the
explicit shared file/socket operations. Both reuse their capability's controller
and worker. CoreWLAN calls are synchronous; CoreBluetooth callbacks stay on a
serial SDK queue and the scan worker drains bounded reports. No additional
service or Rust runtime is introduced.
The platform is macOS-only. No-profile contract builds select no radio backend.
`NXRS_PLATFORM=linux` selects NetworkManager-backed Wi-Fi scanning, link
observation and temporary station activation, plus BlueZ BLE scanning. Both
Linux bridges use GIO/D-Bus on their existing Rust workers; they refuse ambiguous
adapters and do not change host radio power. The scanner uses fresh BlueZ report
signals, local UUID filtering and daemon-scoped opaque peer identities. Cached
device objects alone are not discoveries. Discovery is bounded/best-effort.
Discovery start/stop retains ownership through uncertain replies; callbacks retire before release. Linux GATT uses the
same public client API with a private native GIO executor, described below.
Private-bus tests cover IPC and lifecycle, not Linux RF hardware.
The shared `nxrs-linux-gio` package is a Linux target dependency of the two
radio HALs. Its build script compiles one GLib helper archive only for a native
radio binding; contract/mock builds need no GLib SDK. Other target graphs omit
this package. Selected C/Objective-C helpers compile against their actual target
SDK through the shared build support; embedded builds use their own NuttX helpers.

## Source organization

~~~text
hal/wifi/
  Cargo.toml
  src/
    lib.rs                 public interface and selected exports
    api.rs                 portable traits, commands, events and errors
    controller.rs          shared operation/lifecycle logic
    worker.rs              private std execution for blocking adapters
    shared/                reusable Rust station ownership and contract
    platform/
      mod.rs               capability admission and acquisition wiring
      nuttx/               NuttX Rust implementation
        ffi/               minimal target-header C helpers
      linux/               Linux Rust implementation using nxrs-linux-gio
      macos/               macOS Rust implementation
        ffi/               minimal Objective-C SDK binding
~~~

HALs own managed capability contracts and acquisition implementations.
HALs and peripheral drivers may use Rust `std`; portability requires clean OS
boundaries, not a `no_std` crate marker. The IMU HAL and BMI270/LSM6DSO drivers
target the same std environment as their NuttX and host consumers. Core-only
checks remain only for packages that intentionally support that environment.
Reusable chip protocols belong in `driver/<device>` and expose sensor-specific
APIs over existing bus traits and own their supported settings. The shared
`driver/peripheral::imu` contract defines synchronous configuration resolution,
physical measurements, buffering semantics and device errors. BMI270 and LSM6DSO
implement it directly alongside their chip APIs. HAL IMU reuses its configuration
types and adds sequence/reception metadata, readiness,
acquisition and lifecycle errors. Its acquisition loop accepts a constructed
`peripheral::imu::Imu`; chip constructors and qualification controls stay at the
platform catalogue boundary. Resolve requests before acquiring resources;
do not duplicate supported-mode tables or normalization wrappers in the HAL.
Drivers have no dependency on the capability HAL. Reusable OS resource adapters
live in `driver/<bus>`, with native glue beside the actual OS implementation.
The I²C adapter supports NuttX; host tests inject fake bus operations rather than
selecting another production backend. Product profiles supply concrete resource bindings.
The root `platform/` tree supplies product profiles, device catalogues and concrete
construction; HAL runtime code stays in the HAL. A profile's `[hal]` values name actual OS implementations:
`linux`, `macos`, `windows`, or `nuttx`. The matching OS must equal Rust's
`target_os`; a macOS profile cannot select Linux code. `mock` and `none` are
explicit fixture/unbound states. There is no `std` or `native` platform/profile.

Shared code lives outside `src/platform/`: sockets in `transport/src/udp.rs`,
replay in `camera/src/replay/`, public recording in `storage::recording`,
public shared UDP in `transport::udp`, radio
resource protocols in `src/shared/`, and detector models in `detector/src/model/`.
An OS factory may directly reuse these modules; do not copy them into each OS
folder or invent a shared platform. Private, platform-independent ESP-DL SDK
compute bindings live beside their numerical adapter in
`detector/src/compute/esp_dl/ffi/`; NuttX's compute-context lease remains in
`detector/src/platform/nuttx/ffi/`.
The TLS capability similarly keeps its shared PSA/X.509 SDK adapter and header
glue under `tls/src/sdk/`. Its OS factories own entropy acquisition; SDK callbacks,
key lifetime and enrollment requests stay in the shared Rust adapter.

Use one Rust std implementation wherever the operating systems offer the same
operation: IP sockets belong to transport, and file/thread/synchronization/time
handling needs no per-platform copy. Radio control is a separate capability:
std TCP/UDP sockets do not provide Wi-Fi association, BLE discovery or GATT.
Provider code should contain those native protocols, with generic behavior in
shared Rust controllers/workers. The Linux providers intentionally share the
same GIO/D-Bus resource/call protocol through the normal `nxrs-linux-gio` Cargo
package under `driver/linux/gio/`. That package owns its one native ABI bridge;
each HAL retains its schemas and device policy. IMU and GNSS similarly depend
on `nxrs-acquisition` for shared session/mock/readiness machinery. Neither
consumer source-includes Rust from a sibling package; capability errors stay local.

Private FFI is owned by the provider that consumes it and lives beside that
provider's Rust implementation. There is no parallel capability-level FFI tree.
These are implementation details, not a second cross-platform HAL: CoreBluetooth,
GLib and NuttX bindings may expose different operations. Share contracts and
behavior in Rust when their consumers intentionally implement the same protocol;
do not require matching C entry points or status enums across providers. The C
headers under `tests/host/native_radio_abi` let SDK witnesses exercise the Rust
backends; they are not production SDK interfaces.

The IMU catalogue returns `&'static impl imu::Device`. Its generated statics use
`imu::construction::Instance<S, R>` with separate resource-open and sensor-init
callbacks. The HAL owns all state, readiness, delivery and retirement around them;
concrete drivers and resource paths appear only in catalogue construction.

`api.rs` is an ordinary module, not another crate. Provider modules import
the contract from their own crate, so no contract/provider dependency cycle
exists. Providers are not public namespaces. Product code uses the capability
contract and `open()`; private provider contracts are typed Rust too.
Some older capabilities still export selected types for qualification fixtures.

The camera crate now defines its fixed public factory explicitly in `lib.rs`.
`open() -> Result<impl Camera, DeviceError>` hides both live and unavailable
implementation types. Its selector contains only acquisition wiring and named
exports; replay is not a desktop backend or an unavailable stub.
The additive, opt-in `replay` Cargo feature exposes `camera::replay::open` and
`camera::replay::ReplayCamera` on std-capable targets, independently of a camera
binding. Contract-only builds and hardware consumers need not compile replay.
The native CLI forwards this functionality in Cargo; core-only library fixtures
use `default-features = false` for the CLI package. Existing root replay names
remain explicit compatibility aliases under that feature, not platform exports.
See [camera organization and verification](../hal/camera/README.md).

API/controller modules forbid unsafe Rust. The capability root denies unsafe
code; only reviewed native adapter modules permit it. Unselected provider
modules are excluded by conditional compilation. Host-injected unit tests
may compile native source against fakes; that is not hardware qualification.

At the current baseline, each HAL owns its local `build.rs` and uses the host-only
`nxrs-build-support` package to validate the selected profile against Rust's target. The detector
also uses its own `nxrs-detector-build-support` package for the shared SDK adapter.
The deployment CLI reuses these packages, while HAL build scripts never depend
on the CLI. Only build-dependency edges may enter these host support packages;
runtime HAL edges to platform code remain forbidden. Apps may use only the portable
device catalogue, and services receive devices from their caller.
External profile builds supply `NXRS_SDK_ROOT`, independently of the consumer
workspace. In-checkout builds can discover the SDK root.
The resolver emits the exact validated binding as `hal_provider` for each bound
capability: `nuttx`, `linux`, `macos`, `windows`, `mock` or `simulation`. Hardware
bindings must match Rust's `target_os` before emission; `hardware` is not a
second category. An unbound capability emits no selector. Named simulation and mock
profiles select their corresponding factories. Unbound capabilities compile
their contract and unavailable factory. Keep this selection in HAL wiring so
shared algorithms can use explicit resources and capability information.
Product builds and detached Cargo consumers use the same resolver.

Ordinary driver dependencies remain Cargo declarations; a HAL build script
cannot enable an optional dependency after Cargo has resolved the graph.

Keep conditional compilation at a small set of ownership boundaries:

| Control | Owner | Purpose |
| --- | --- | --- |
| `hal_provider` | HAL module/acquisition wiring | Select the resolved provider or compile the unavailable factory |
| `target_os` | OS module/factory wiring | Compile only the actual OS implementation |
| `replay`, `recording`, `udp` | Owning HAL and its consumers' Cargo manifests | Include explicit std software operations; do not select or require a hardware backend |
| `person-model`, `pico-model` | Detector HAL, derived by product builds | Include the profile-selected model and its optional compute dependencies; explicit only for HAL qualification |
| `probe` | Owning crate and qualification fixtures | Enable optional measurements and test witnesses |

Group selected exports and acquisition under one admission module in the HAL's
`src/platform/mod.rs`; do not repeat the same condition on each fallback type,
trait implementation and function. Shared std modules remain outside that
directory. Keep test-only provider injection explicit at module boundaries.

Services consume `detector::Detector` directly and have no model feature
forwarding or model compatibility facade. A product profile's `[detector]` table
selects `model = "pico"` or `"vww"`, `stack-bytes` and `sample-ms`. The firmware
frontend asks Cargo for the selected production graph with the build's actual
compiler, target, configuration, features and default-feature settings. It derives
the required internal model feature only when Cargo selects the detector HAL.
The application declares the detector as an enabled direct normal dependency;
renamed dependencies and consumer-workspace inheritance work normally. Cargo
cannot activate an arbitrary transitive dependency's feature from an external
application, so a transitive-only detector produces an explicit composition error
before compilation. Services do not forward model features. Products without the
selected detector dependency do not acquire model dependencies just because a
platform offers detection. App/service manifests and CLI switches do not select
the model; the product profile owns that choice.
An empty table disables the optional detection composition; the video baseline
also unbinds its detector HAL. Missing or conflicting model dependencies in a
configured product fail before model compilation. There is no generated model
cfg. Cargo resolves dependencies before build scripts run, so the detector's two
internal dependency features remain necessary. Explicit model features are
supported for HAL evaluation, including unbound builds and paired comparisons;
desktop OS profiles alone are qualification bindings without a product model.
Model evaluation examples live beside the HAL's qualification code.

Use `probe` as the single opt-in name for diagnostic instrumentation and SDK
witnesses. Select coherent tracing implementations at a module boundary instead
of repeating cfg conditions on their fields and methods. The IMU selects either
instrumented tracing or a zero-sized no-op implementation; its ordinary
acquisition code has no diagnostic cfg. Enabled builds still require an explicit
observer before collecting timings.
Ordinary tests use `cfg(test)` and named test targets; software-feature
requirements such as replay belong in Cargo's `required-features`, not in OS
or hardware-admission guards. Do not add a separate feature for each probe,
test command, CLI or integration executable.

Benchmark workloads use validated, typed experiment cases selected once by their
build runner. Compile those choices into constants or concrete I/O types; do not
emit generated boolean cfg aliases or scatter Cargo feature combinations through
workload algorithms. Keep primary measurements free of probes and record the
selected case with source hashes. Historical attribution variants remain in their
original Git revisions instead of expanding the current build API.

Use descriptive local names without a redundant `nxrs_` prefix for private Rust
functions, native symbols and cfg names. Capability prefixes on C symbols avoid
collisions in the final binary. Public package names, established protocol
identifiers and environment variables retain their external namespacing.

## Platform variants and validation

Profiles live in `platform/nuttx/platforms` (firmware) and
`platform/profiles` (desktop and legacy fixtures). Names are unique across both
locations. A product variant may extend an actual OS or board profile and override
selected `[hal]` bindings. `simulation` runs coordinated IMU and GNSS.
`host-demo` and `host-imu` remain independent scripted fixtures. Core-only compilation
checks are legacy contract tests, not a bare-metal product platform.

An optional NuttX profile `cpus = 1` or `2` derives the SMP enablement, CPU count
and checked requirements; it does not introduce CPU identity into shared code.
An optional detector `compute-core` pins its numerical lease. The factory
validates actual CPU capabilities before acquisition; shared model code has no
core or platform knowledge.

Bindings merge by capability; `"none"` explicitly removes an inherited
binding. Other fields/tables replace the parent's value. In particular,
Kconfig lists are not silently concatenated. Git records platform history;
named variants represent actual board/product/test differences.

The resolver rejects unknown/ambiguous platforms, inheritance cycles,
unknown implementations and target/OS mismatches. The firmware preflight also
checks an application's declared `required-capabilities`. A missing required
binding fails before dependency setup. An optional unbound capability must
not silently turn into a mock.

Cargo tracks the platform environment, inherited profile files and resolver
as build inputs. Switching platform in an existing target directory therefore
reruns selection. Normal third-party dependencies still use Cargo manifests;
conditional Rust modules do not replace Cargo dependency management.

At the current baseline, each consumed HAL owns its native C helper compilation
through its package-local build script. A profile binding alone cannot cause an unconsumed HAL's sources
to compile. For NuttX, the controller prepares the headers and captures Make's
actual compiler/ABI flags before Cargo runs; the owning HAL builds a private
archive with that context. Additive functionality features can gate their own
helpers. Additional qualification C sources remain explicit integration inputs.
Helpers compile with the configured target headers, never as host objects.
Dependency changes remain traceable patches under `upstream/`.

## Ownership and shared behavior

The consuming application or service owns the capability. A HAL does not
automatically become a service or get a thread/queue. For example,
`wifi::Controller` owns its provider and handles operation identity, deadlines
and lifecycle in the consumer's existing event loop. A connectivity service
can own Wi-Fi/BLE and add network selection/reconnect policy when needed.

`app/home-cam` composes the camera service with a caller-polled MJPEG
server. `service/connectivity` adds Wi-Fi reconnect policy separately; neither
component adds a service thread. Hardware, storage and time are injected, and
platform configuration owns native driver resources. See the
[local streaming qualification](../tests/nuttx-std/stream/README.md) for its
resource budgets, reproduction steps and evidence limits.

Wi-Fi and BLE scanning each use one private std worker thread when the
adapter's OS calls may block. NuttX adapters supply normalized operations and
native callback/interface leases, not a second controller or service.
Native stack workers are separate. Stop admission
does not preempt a blocking ioctl; finish/join belongs in explicit teardown,
not normal event dispatch.

Wi-Fi cleanup is fallible and retains its driver on failure. Both radios retain
the first terminal fault independently of Stopped and event-queue capacity.
An explicit owner can retry failed teardown; after Drop the worker retries with
capped backoff and retains native ownership until cleanup succeeds. Persistent
native failure still prevents resource reclamation; Drop does not force it.
Unexpected BLE worker unwind conservatively retains the callback lease rather
than admitting another owner while callback quiescence is unknown.

`wifi::open_for_quiescence()` separately acquires exclusive maintenance control
after normal sessions have disconnected, stopped and joined. Acquisition is
read-only; explicit `quiesce()` requests native interface shutdown and verifies
down state. Retain this owner through flash work and `resume()` before reopening
a session. An uncertain transition is sticky and quarantines admission until
reset; Drop does no blocking radio cleanup. Only the ESP32-S3 STA, single-core
NuttX provider admits this resource, using the patched poll-thread join barrier.
It does not establish shutdown of other peripherals or physical bus timing.

The hardware-backed `ble::open` contract is scanning only. Scan events have no generic GATT
value buffer. A full bounded peer registry emits `DiscoveryLimited` once per
scan without failing the provider or silently recycling session-local IDs.
`ble::gatt::Client` is a separate, allocation-free shared state machine for
connect, UUID discovery, bounded read/acknowledged write and disconnect.
The caller chooses the inline value capacity and drives it from the same event
loop using `poll(now_ms)` and `next_deadline_ms()`. Drain until `Update::None`
before waiting; an ignored stale event is still progress, not an empty queue.
It adds no worker or queue.
Peer IDs carry their scan session; discovered attributes carry their connection
identity, so handles from a previous connection cannot be reused accidentally.

Timeout/cancel reports one outcome and starts recovery. Admission stays closed
until the driver confirms that old callbacks are drained **and** the link is
gone. A late completion or disconnect notification alone cannot release that
barrier. Recovery has its own deadline; missing it produces a sticky failure,
not an indefinite application wait or permission to recycle native storage.
Ordinary remote ATT errors leave an established link usable.
When an OS omits the raw ATT code, `Failure::Remote(Error)` preserves the remote
rejection without inventing a number or treating it as a broken local transport.

NuttX selects `ble::gatt::open::<N>(wake, recovery_timeout_ms)`. The native
adapter uses the existing Bluetooth LPWORK queue, not unbounded central ioctls
or another thread. Native callbacks retain results; a later work item publishes
completion after those callbacks return. Timeout/drop retains native storage
until cancellation, link teardown and callback removal are confirmed. A late
connection after LE-create cancellation is disconnected before recovery ends.

The NuttX backend requires flat builds, one LPWORK consumer and no user-work
queue redirection; unsupported configurations fail compilation. Only one native
GATT client can be active at a time. Values are
bounded to at most 64 bytes. Reads use blob continuation until a short/empty
response proves completion; oversized values fail instead of returning a prefix.
Writes larger than the negotiated ATT payload are explicitly unsupported.
UUID discovery stays inside the selected service and rejects duplicate matches.

macOS selects the same `ble::gatt::open` API and private typed Rust executor
contract. Commands and replies carry Rust enums and bounded values; owned Rust
wake functions stay alive until native retirement completes.
CoreBluetooth runs on a private serial SDK queue, not another Rust service or
runtime. Consumer submit/poll never wait for that queue; cancellation/drop use
priority control signals. Successful disconnect/recovery retires callbacks
before publishing its barrier. A full event queue cannot prevent dropped-owner
cleanup, and a contended poll re-arms the owner's wakeup. Desktop peer identities
are copied at admission; characteristic handles are bounded local indices,
never silently reused within a link. SDK tests cover these paths with fake
central/peripheral objects; the device results and remaining gaps are listed below.

Linux uses a private GIO executor for blocking D-Bus/BlueZ work; submit/poll
remain nonblocking and wake the same owner event loop. UUID lookup is scoped
to the chosen device and service, with bounded local characteristic handles.
Read values must be complete and fit the requested capacity; writes use BlueZ's
acknowledged `request` mode. Pending replies and signal callbacks remain alive
through cancellation and teardown, including a connection arriving after an
earlier disconnect. A full event queue cannot prevent dropped-owner cleanup.

Unlike discovery sessions, [BlueZ device connections](https://bluez.readthedocs.io/en/latest/device-api/)
are system-wide. The adapter refuses an observed existing connection or a
rejected competing connect, but this is not an OS-wide lock. The application
must exclusively control its target peripheral while using this client;
concurrent use by another program is not supported. It does not pair, change
trust/power or remove cached devices. BlueZ's named errors sometimes omit the
original ATT number; those are normalized remote rejections, not guessed codes.
This is a desktop-only executor/dependency, not another NuttX worker or runtime.

Stop scanning before connecting. Keep its provider alive through connect
admission: peer IDs expire when that scan provider closes, but an accepted
connect owns its copied target independently. The owner supplies a nonblocking,
coalescing-safe wake function (for example, `thread.unpark()`) and drains GATT
updates in its existing event loop. The wake function has shared Rust ownership
and is released only after native close is quiet; it must not block during
destruction.

The backend is sanitizer-tested and cross-compiled against both boards' real
NuttX headers. ESP32-S3 and Pico 2 W clients have each passed the bounded
[BLE-only device test](../tests/nuttx-std/ble/README.md#portable-gatt-client)
against the other board's native peripheral. Both directions have also passed
cancellation, complete bounded long reads, overflow rejection and subsequent
re-acquisition without reboot. Separate
[portable-client coexistence checks](../tests/nuttx-std/radio/README.md#what-a-pass-means)
cover UDP/TCP on both boards, with an unresolved discovery miss.
Older native fixture passes do not qualify this adapter. macOS BLE scanning
uses the same controller/worker with opaque OS peer identities, not fabricated
MAC addresses. Its SDK bridge is sanitizer-tested with fake central/peripheral
objects, including UUID filtering, bounded reports and stale-callback isolation
across restart. A real Mac scan also discovered peers and stopped/joined cleanly;
that qualifies scanning only. macOS GATT has separate device checks below;
Linux has separate private-bus scan/GATT tests, not RF qualification.

Portable event contracts use bounded payloads and explicit request/session
identities. Rejected commands are returned to their owner. Scan results and
completion must preserve FIFO ordering; terminal state remains observable if
delivery is temporarily full. Discovery is bounded/best-effort, not a claim
that every radio report is delivered.

Ordinary files, sockets, threads and time use qualified Rust std facilities.
UDP uses one shared std implementation across the native and NuttX platforms;
only the existing constructors and empty-packet contracts differ. Device-control
adapters retain target-header bindings rather than guessed numeric constants.
Storage shares its RCAMREC1 record encoding while keeping each provider's
validation, writing and commitment behavior.
NuttX-specific controls stay behind small device-specific FFI boundaries; no
generic POSIX wrapper, global HAL object or platform-wide service factory is
introduced. The older core-only camera/storage adapters and their descriptor owner live
under `tests/nuttx/support`; production NuttX storage uses Rust std files. See
[NuttX device access](nuttx-device-access.md).

The radio refactor preserves the SDK/private-bus and NuttX ABI witnesses. Rust
owns stable callback storage until native delegate detachment or subscription
destruction completes. SDK execution contexts remain explicit: LPWORK on NuttX,
serial CoreBluetooth dispatch on macOS, and GIO contexts on Linux. CoreBluetooth
references remain opaque and are pinned by native containers; Rust decides
which resources to acquire, retire and release. Linux GATT uses a Rust thread
and a process-lifetime GIO wake context, avoiding a C thread/eventfd layer.

## Current radio qualification

The hardware results below describe the previously qualified images. This
behavior-preserving source refactor requires refreshed physical radio checks
before claiming hardware qualification for newly built images.


Qualification is capability- and image-specific; host tests are not radio
proof. The linked fixtures retain the measured workload and limitations.

- ESP32-S3 and Pico 2 W have completed Wi-Fi scans, station association/DHCP
  and exact UDP round trips, including negative controls followed by reconnect.
  The bounded ARP wait is not a packet-level root-cause proof or a reliability
  guarantee. The combined-radio results below remain bounded functional checks.
- BLE scanning and the bounded portable GATT client have device results on
  both boards. The combined-radio images passed the versioned V1/V2 sequence
  in both directions: eight cases and 600 exact write/read pairs, including
  cancellation/value/error checks, without reboot or role-change reflashing.
  The current images then passed all four portable-client Wi-Fi coexistence
  cases (UDP/TCP on both boards) on the same boot. An earlier warm case missed
  discovery before connection or traffic; the new pass does not resolve its
  cause. Both original full flashes were restored and verified. Pairing and
  notifications are outside the implemented client contract.
- Pico USB recovery: the opt-in 1200-baud hook was verified at idle and during
  a foreground command. It requires working USB/timer interrupts; it is not
  a hardware watchdog and should be disabled in production unless intended.
- macOS Wi-Fi's native bridge passed three read-only hardware scans after the
  test application received Location permission. Scans returned 10, 12 and 12
  networks, each including the current access point. Every context was released
  with the host association unchanged. The earlier CLI attempt was refused by
  permissions; the successful checks used a foreground test application, not
  the CLI scan-owner fixture. Real association remains unqualified.
  Joins remain disabled by default: [CoreWLAN stores joined-network passwords,
  with no macOS join-once equivalent](https://developer.apple.com/forums/thread/805392).
  `wifi::open_with_storage(..., CredentialStorage::SystemManaged)` explicitly
  permits this storage; saved credentials may survive failed joins, disconnect
  and stop. It requires exclusive control of the interface, not a race-free OS
  lease. Linux/NuttX retain volatile credentials under either policy. SDK-backed
  fake tests are not macOS association/keychain hardware qualification.
- macOS BLE scanning passed a real eight-second scan and quiescent worker join.
  Mac-to-ESP GATT cancellation, boundary/error values and a normal 100-pair
  exchange passed local checks; these are not long-term reliability results.
  A separate five-second scan missed the peer. The versioned Mac→Pico sequence
  passed V1 compatibility and V2 cancellation, boundary/error values and normal
  transfer: 300 exact pairs, confirmed teardown and original-flash restoration.
  V1 preserves its original three-handle service; V2 owns the extended schema.
  Earlier expanded-V1 discovery failures and unsuccessful device-only forget
  attempts remain recorded. The V2 pass is not proof of old-cache recovery;
  no global host reset is performed.
- Linux Wi-Fi scan/link observation and open/WPA2 station activation are
  tested with a private D-Bus service, not RF hardware. Activation uses only
  [volatile, client-bound NetworkManager profiles](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html),
  disables autoconnect and refuses an observed existing association. There is
  no fallback to a persistent profile on older daemons. Shutdown deactivates
  only the owned activation and verifies it is inactive; unfinished replies or
  unproven cleanup retain the context and interface lease instead of reporting
  `Stopped`. An ambiguous IPC failure fails closed and can require restarting
  the application; bus closure is not treated as proof of rollback.
  Linux BLE scan/GATT have private-bus tests, including the portable client's
  legacy/cancel/value/full scenarios; no Linux radio hardware is qualified.
  Native file/socket adapters
  and host-injected radio tests do not establish other desktop radio support.

See the [Wi-Fi fixture](../tests/nuttx-std/wifi/README.md),
[BLE fixture](../tests/nuttx-std/ble/README.md), and
[dependency patchsets](upstream-patchsets.md) for exact evidence and limitations.
Radio fixtures are functional tests, not refreshed size/speed benchmarks.

## Regression checks

~~~sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_deployment.py
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py
python3 -m unittest discover -s tests/host -p test_camera_organization.py
python3 tools/check-hal-isolation.py --out target/hal-isolation-new
cargo test --locked -p nxrs-camera --features replay
cargo test --locked -p nxrs-wifi -p nxrs-ble --lib --tests
~~~

The isolation check records clean Cargo artifacts and actual emitted provider
cfgs, compiles portable core/WASM variants, runs independently checked native
examples, and rejects unsupported platform/target combinations.

## Detector ownership and portability

Detection delivery is an application contract above the detector HAL.
`service/detection-wire` owns the bounded, versioned camera/gateway protocol;
the camera app translates typed service observations into it. The stream
service handles opaque optional metadata, while the gateway validates and
authenticates it independently of video encoding. SDK/model details stay below
the detector API. Subscribers use latest-value delivery and independent source
and inference expiry; slow readers cannot block capture. iOS expires status
using monotonic receipt deadlines. Live boxes require a qualified mapping from
source frame identity to the displayed video frame. The annotated ingest path
carries identity in each H.264 access unit; the native decoder joins its RTP
timestamp to the actual decoded image buffer before the renderer consumes it.
Retained buffers and pending identities are bounded. Missing, aliased or expired
identities suppress boxes, and local timers clear overlays when input stops.
Localized presence policy uses accepted boxes; rejected raw model peaks cannot
indefinitely retain `Person`. Presence-only providers retain score hysteresis.
See
[camera detection delivery](camera-detection-delivery.md) for the schema,
resource bounds, authentication and synchronization limits.

`hal/detector` owns the `Detector` contract, capability-owned errors, borrowed
`Image` input, reported `Capabilities`, normalized bounded person boxes and model
implementations. `detector::open(Config)` acquires the profile-selected detector;
applications do not import an SDK or select a backend. OS factories reuse the shared Rust model implementation; the NuttX factory
adds its compute-context lease around the same preprocessing, numerical
kernels and postprocessing. Model compilation is
optional. Gate each optional model at its module boundary with its Cargo
feature; do not generate a second model cfg or scatter model guards through
its implementation. Compile requested SDK/model code independently of profile
admission. Acquisition remains platform-factory wiring and returns `Unsupported`
when unbound. Requesting unavailable localization also returns `Unsupported`.

`service/detection` owns its bounded worker, frame-copy admission, timeouts,
failure/expiry state and temporal alert policy. It consumes the detector's common
API. The camera HAL and capture loop do not know which detector executes the
work. Platform identities and SDK layouts never enter those shared algorithms.

PICO uses only the pinned ESP-DL numerical operators. Rust validates/decompresses
JPEGs, decodes model confidence/boxes, bounds candidate/output storage and applies
NMS. A fixed, hash-verified graph is configured at build time; no model loader,
FreeRTOS task launcher, camera SDK, flash/partition reader or native alert logic
is linked. Private SDK allocator/timing hooks and object ownership stay beside
the consuming provider's FFI. The owning Rust worker joins before its numerical
engine is dropped; the SDK launches no asynchronous work or callbacks.


A detector's native compute context is a Rust-owned scoped resource. Activate it
only around synchronous native numerical execution, serialize owners when the
native context requires it, and restore prior state before releasing the lease.
Do not leave a globally enabled accelerator context as a side effect of a frame
call. Unsupported CPU/execution configurations must fail qualification rather
than inheriting an unproven context-lifetime assumption.

Detector performance qualification compares the exact pinned model's vendor
benchmark with measured pipeline stages. State differences in input encoding,
tensor-copy scope, CPU time and wall time before comparing latencies. A requested
sampling interval is a product budget; report achieved observations/s and decoded
video FPS independently. Memory/cache/clock settings belong to product wiring,
with explicit resource costs and physical qualification. Duty cycling protects
capture but does not by itself demonstrate efficient model execution.
