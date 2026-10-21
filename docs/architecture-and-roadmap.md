# Nxrs: portable embedded architecture and roadmap

**Revision:** 10 — product-platform selection plus qualified multi-app/multi-instance composition.
**Updated:** September 29, 2026.
**Documentation baseline:** `686d59c46484a66d12085ab946e6a882970a869f` (merged PR #2).
**Original source review:** `e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc`.
**Status:** Staged design. Current implementation and qualification are recorded in
[architecture.md](architecture.md) and [nuttx-std.md](nuttx-std.md).

## Decision

Each `app/<name>` is a normal Cargo binary with a handwritten `src/main.rs`.
Exactly one app is selected for each production MCU image. Its `main()` normally
creates and connects reusable services. Applications and services are both
portable layers and may use capability-local HAL facades/contracts when that is
the natural ownership boundary. Neither may depend on or name the concrete
provider/hardware realization.

When a capability primarily exists to implement one service, that service should
normally acquire and own it through the HAL at an explicit lifecycle boundary
such as `start()`.

The HAL is capability-local. Each domain such as `hal/imu`, `hal/gnss`, or
`hal/camera` is one crate with a public interface, a provider-independent
`src/api.rs` contract and private provider modules. One `NXRS_PLATFORM`
selects the complete set of bindings. There is no runtime `hal/platform` package.
Provider selection remains a deployment/build decision below app and service
policy.

NuttX supplies the RTOS; Rust `std` is allowed. Prefer the same ordinary threads
and bounded channels on NuttX, native, and the selected pthread-enabled
Emscripten browser profile. Keep actual platform I/O differences below the HAL
boundary, not in a compulsory async service rewrite.

For physical NuttX resources, the dedicated Rust HAL provider should use
qualified `std` file/I/O/socket APIs directly for ordinary operations. A
device-specific NuttX control is translated behind a typed Rust method with only
the minimum private target-header/ABI glue required. Do not add a generic POSIX
adapter layer or duplicate protocol/device logic in C. See
[NuttX device access from Rust](nuttx-device-access.md).

Unselected implementations and their exclusive dependencies must not be built,
linked, or initialized in that image. See
[hal-platform-architecture.md](hal-platform-architecture.md).

**Require behavior and enforceable boundaries, not a framework.** Cargo, NuttX's
configuration/build tools, ordinary Rust types, and small existing scripts are
the starting point. Do not add a tool or abstraction merely to complete a diagram.

## 1. Requirements and remaining work

The original review found a useful portable slice, not a finished firmware
platform. The assessment below retains that source-review scope. This revision
simplifies the design and incorporates the separately executed browser thread
probe [Q01]; it does not claim new NuttX or physical-device qualification.

| # | Requirement | What the design must deliver |
|---|---|---|
| 1 | NuttX, with Rust `std` | Ordinary startup/main/thread/time/TLS/linking is qualified on the selected NuttX test targets, and ESP32-S3 QEMU now exercises full camera/storage/std-UDP integration. Final production-app packaging and physical-board execution remain. |
| 2 | Modular and composable | Explicit ownership and dependency boundaries; add packages only for meaningful reuse or dependency isolation. |
| 3 | Reusable services | Ordinary libraries with any number of related functions; neither a thread nor a universal service trait is mandatory. |
| 4 | Apps compose services | One app owns `main()` and handwritten service construction; Recorder/Monitor are workflows, not co-resident apps. |
| 5 | Build selects HAL providers/target | Deployment chooses the app binary, one provider per required capability, board/OS configuration and resource limits without duplicating service wiring. |
| 6 | Portable apps | The same app behavior runs wherever its required capabilities and execution model are supported; unsupported combinations fail explicitly. |
| 7 | Common HAL APIs | Capability-first, backend-free APIs with clear data, timing, error, and ownership contracts. |
| 8 | Portable HAL abstraction | Apps and services may access capability facades/contracts when appropriate; concrete hardware/OS/provider identity remains below each capability facade. Service ownership is preferred when one service naturally owns the resource. |
| 9 | Service-owned event loops/threads | Explicitly started threads where useful, with bounded handlers and configured resources; no compulsory thread per service. |
| 10 | Interthread message passing | Typed bounded channels, declared overload behavior, and safe payload ownership. |
| 11 | One event-driven wait point | Each active owner has one bounded inbox and one canonical wait operation; commands, peer events, readiness, timers, and completions are serialized into run-to-completion handlers. |
| 12 | Ergonomic configuration | Reuse NuttX Kconfig/menuconfig and existing board definitions; validate settings without duplicating service wiring in a configuration language. |
| 13 | Browser WASM | Use the qualified pthread-enabled Emscripten profile for direct app/service + web HAL execution; qualify actual HAL I/O separately. Neither an async service rewrite nor NuttX-in-browser is required. |

The review's main implementation gaps remain:

- The native replay path now has one app-owned timed wait point: replay exposes
  the next useful camera poll time, camera polling is separated from consumer
  retries, and Busy sinks use an explicit retry timer without re-polling the
  camera. Physical/NuttX interrupt-driven camera readiness is still unqualified.
  [R04], [R06], [R07]
- Borrowed frames remain local to the camera owner. The selected native
  recording boundary now uses preallocated owned buffers: queue capacity and a
  free payload buffer are reserved before copying, ownership transfers to the
  writer, and completion recycles the buffer. Drop no longer joins the worker;
  flush/finish are the explicit completion contracts. Other future cross-thread
  large-payload paths still need the same ownership discipline. [R05], [R09]
- The x86 NuttX simulator remains core-only, while ESP32-S3 QEMU now uses an
  ordinary Rust main/std runtime with synthetic camera data and real NuttX
  camera/storage/UDP paths. Physical camera/radio integration and production-app
  packaging remain separate work. [R11], [R13], [R14]
- IMU/GNSS demonstrate service-owned HAL acquisition: app/event-demo creates
  services only and those services acquire their capabilities internally.
  The native camera/storage/transport profile now demonstrates the other valid
  case: app/nxrs acquires portable HAL capabilities directly, then transfers
  ownership into the generic services. Neither app path names concrete provider
  packages, and the former checker exception has been removed. [R01], [R12]

Retain the existing lifetime checks, staging isolation, sequence-gap reporting,
independent byte/ABI oracles, target link checks, and failure tests. Full current
contracts belong in [portable-services.md](portable-services.md); source ownership
and current checker rules belong in [architecture.md](architecture.md).

## 2. Minimal organization and dependencies

This is the target structure, not a claim that these packages already exist:

```text
app/
  nxrs/
    Cargo.toml
    src/main.rs             This firmware's construction and entry
  <another-app>/            A different binary and firmware image
service/
  <domain>/                 Reusable functionality; private modules as needed
hal/
  imu/                      Public nxrs-imu facade
    api/                    Common contract, no backend dependencies
    nuttx/                  Physical/OS provider where implemented
    native/
    web/
    mock/
  gnss/                     Same pattern
  camera/
  storage/
  transport/
driver/                     Repository-owned device protocols when needed
platform/                   Board configuration, target startup/link integration
  nuttx/
tools/                      Existing focused build and validation scripts
tests/                      Portable, native, browser, and NuttX tests
external/                   Pinned NuttX sources
```

Keep today's small `service` crate until separate dependencies or consumers
justify splitting it. For HALs, the API/backend package split provides real
build isolation. Do not create empty backends, one crate per small type, or
mandatory `runtime/{api,threaded,web,test}` packages. Shared execution helpers
can start as modules and become a library when actual reuse justifies it.

```text
app main -> reusable services
         -> capability facade when directly useful

service -> capability facade + required data contracts
capability facade -> selected provider
provider -> domain API + required driver/OS support
```

Handwritten app/service policy may use portable HAL contracts/facades, but does
not call NuttX/browser/register/bus APIs or concrete provider packages. Concrete
resource selection stays in the build-selected feature of the relevant
capability facade. A service should own the HAL resource when the resource exists
primarily to implement that service; direct app ownership is also valid when it
is the simpler and more natural boundary. The HAL layer does not require code
generation, a universal factory trait, or a giant global Hal object.

Forbid app-to-app production dependencies and platform-support libraries that
import app crates. There is no separate product layer, platform launcher, app
registry, mandatory app library, registration macro, or universal `App`/`Service`
trait. `main()` is the composition root. An optional library target is a testing
choice, not the application model. [E01]

A service may be synchronous, own a thread, or share a loop with related code.
Keep ordinary local calls local. Only execution boundaries require channels;
a pure calculation does not need a client, request ID, and reply.

## 3. HAL composition and platform support

### Contracts and implementations

Portable app/service code uses only the capability contracts/facades it needs.
For example, navigation depends on nxrs-imu/nxrs-gnss and their
sample/fix contracts, but not BMI270, LSM6DSO, UART instances, mock packages, or
camera resources. Preserve
units, coordinate conventions, timestamps, actual negotiated settings, error
semantics, and ownership in those contracts. Keep readiness/cancellation
behavior explicit; a timeout setting does not make a blocking driver cancellable.

Each required capability selects at most one provider for an executable
deployment. Provider selections are independent across capabilities. Reject two
owners of one exclusive physical resource. Reuse an
existing NuttX driver instead of rewriting or registering it twice. Low-level
portable drivers may use established bus traits; those bus details remain below
services.

Each HAL maintains its supported platforms/profiles, required resources,
capabilities, and tested configurations alongside its code. A small table and
checked-in build selections are enough initially. Machine-readable fields can
live in Cargo's `package.metadata` if needed; a separate `hal.toml` schema and
catalog compiler are not prerequisites. Cargo does not validate custom metadata;
any checks using it must be explicitly implemented. [E02]

Support means the stated capabilities work on a tested configuration, not merely
that a platform appears in a list. Physical-device, replay, and mock modes are
explicit choices. Do not silently replace required hardware behavior with a mock.
Do not promise a web implementation for every HAL.

### Exclude unused implementations at build time

Use separate provider crates and Cargo's existing optional/target-specific
dependencies below each capability facade. Validate that an executable
deployment selects one compatible provider for every required capability without
conflicts. Shared capability APIs enable no provider by
default. Source-level `cfg` does not deactivate an unconditional dependency.
[E03], [E04]

Dependency activation must be decided through Cargo manifests/features before
compilation. `build.rs` can validate inputs, emit constants/bindings, or pass link
arguments; it cannot introduce new dependencies after Cargo resolves them. Do
not generate a replacement app manifest or a second executable as the default
solution. Keep the app's checked-in binary and handwritten main. [E05]

For a selected firmware build, check its active Rust dependency/build records
and the resolved NuttX C-driver configuration, objects, image map, and registration.
Unselected implementations and their exclusive build scripts/C objects must be
absent; linker dead-code elimination alone is insufficient. A workspace/lockfile
entry is not proof something was compiled. Shared dependencies are legitimate
when a selected component independently needs them. Broad test builds may test
other backends separately from the minimal firmware build.

## 4. Event loops, messages, and resource bounds

Keep service state and event handling in ordinary structs and methods. Active
services use the shared `service/event` transport: a private service-specific
event enum, cloneable senders, one unique bounded inbox, and one canonical wait
operation backed by `std::sync::mpsc::sync_channel`.
The intended threaded loop is conceptually:

```text
wait for an input or the next deadline       <- one logical wait point
handle the event with bounded work
submit outputs without an unbounded wait
repeat, or perform explicit shutdown
```

Use a blocking receive/timed receive on the owner thread for NuttX/native and
the selected pthread-enabled browser profile. The same ordinary std thread/channel
code can be retained; a custom thread wrapper, async adapter, universal inbox/outbox
interface, actor framework or executor is not required. The isolated browser probe
executes this pattern in actual Chrome and Safari. Its results do not establish
production HAL behavior or timing guarantees. [Q01]

The single wait rule applies to each service owner, not the browser UI thread.
Handlers remain bounded and must not wait indefinitely on peer services. Browser
callbacks may deliver HAL events, but must execute on a context that stays able to
process them. An alternative threadless profile can use cooperative execution if
there is a concrete need; it is not the default or a required second implementation.

Start threads explicitly, not as a surprise in constructors. Use std directly
for facilities the selected targets qualify. Isolate only genuinely target-specific
priority, affinity or startup settings; do not introduce a universal execution
wrapper. Browser workers do not reproduce NuttX priority guarantees. Configure
resources before production work starts. A local service need not become a task.

A camera-ready event or timer must explain when to retry a pending read. Avoid
lost-wakeup races. Coalesce readiness only while preserving knowledge that work
remains. A polling-only driver may use a declared timer; a blocking driver needs
its own bounded/contained worker where it would otherwise stall a shared loop.
An ISR must use the target's approved notification path, not an unqualified std
channel, allocator, or arbitrary handler.

### Rules that are necessary, not framework features

Every cross-thread path defines capacity, maximum payload/in-flight work,
ordering, full/disconnected behavior, and shutdown handling. A saturated data
stream must not indefinitely block shutdown or required completions. Do not
block one event loop waiting for a service that may depend on that loop. Use
correlation IDs or generations only where concurrent requests/restarts make them
necessary; they are not compulsory fields for every message.

Small samples can be copied into bounded queues. Keep borrowed camera frames for
co-located consumers; add preallocated owned buffers when data must outlive that
borrow or cross threads. Do not immediately build a general zero-copy lease pool.
If a measured path later needs shared buffers, safe ownership must prevent reuse
until every reader is finished, including disconnect/error paths.

Broadcast needs independent consumer state; a work queue with competing receivers
is not a broadcast guarantee. Choose each actual consumer's overflow policy:
preview can replace stale data, recording may need backpressure or an explicit
overrun error. With finite memory, an indefinitely stalled consumer cannot be
guaranteed lossless service. These policies need not become a generic credit,
subscription, or quality-of-service framework.

Distinguish queue admission, I/O completion, storage commitment, and remote
acknowledgement. A timeout is not proof that an operation was cancelled; retries
must respect side effects. Shutdown stops admission, finishes or cancels accepted
work according to policy, releases resources, and joins workers from an
appropriate context. Do not hide an unbounded join/flush in a real-time handler
or promise reliable cleanup from `Drop` alone. Preserve existing error contracts.

### Timekeeping without another HAL

Use `std::time::Instant` for elapsed time/deadlines, `Duration` for intervals,
and `SystemTime` only for wall time where a valid system clock is available.
Using portable std facilities is not direct hardware access. Do not add
`hal/clock`, `configured-clock`, or a platform-specific wrapper for ordinary
system timekeeping on the selected std targets.

The execution owner samples time and passes it into time-dependent processing;
keep the existing `step(now_ms)` boundary. Tests supply explicit timestamps and
ordered events without sleeping. Preserve each timestamp's units and origin:
the native replay epoch starts after workflow startup, not at boot or Unix time.
A fake time value does not virtualize blocking channel waits; test actual waits
separately. There is no Clock HAL trait: deterministic tests use an ordinary
value object, while the core-only x86 NuttX fixture keeps its unavoidable
clock/sleep shim under `tests/nuttx` rather than in production HAL code. Device acquisition times
and conversion between sensor/GNSS clocks remain explicit device/data contracts.

### Determinism

Use fixed limits and startup allocation for latency-critical paths. Track actual
queue/buffer capacity, stack use, handler latency, and failure behavior. Ordinary
`std` facilities are allowed; neither `std` nor message passing proves deadlines.
Channel synchronization and dependencies on lower-priority workers still need
analysis. Test actual physical deadlines under a stated workload.

Inject time and ordered inputs for behavior tests. When input order affects a
result, define that order or test replay of the accepted order. A new universal
trace/replay engine, automatic budget calculator, and reporting dashboard are not
prerequisites. Keep functional repeatability, bounded memory, and hardware timing
as distinct claims; host/browser/QEMU tests do not prove physical deadlines.

## 5. Configuration and build tooling

### Use the existing sources of truth

| Concern | Starting mechanism |
|---|---|
| Which app and Rust code to build | Cargo package/binary selection. |
| OS facilities and compiled NuttX drivers | NuttX Kconfig/menuconfig and checked-in configuration fragments. |
| Board pins, buses, registered devices, memory | The board's existing definition and startup path; use devicetree where actually supported. |
| Service instances and connections | Handwritten Rust in the selected app. |
| Rates, capacities, and other app settings | Typed Rust configuration; feed settings from the existing build configuration where useful. |
| HAL resource/provider selection | `cargo firmware --app ... --platform ...` selects one product platform; that platform owns provider features plus board/target configuration. Providers remain dependencies of their facade, not the app. |

Retain the goal of ergonomic Kconfig/devicetree-quality configuration, not a
requirement to recreate Zephyr's tooling. Do not maintain a second hardware graph
beside an existing board definition, or duplicate Rust service wiring in
`app.toml`, `[workflow]` string references, or deployment graph files.

Keep saved build configurations easy to select and inspect. Reject conflicting
app selection, unsupported providers, missing/exclusively claimed resources, and
invalid capacities at the appropriate build/type/initialization boundary. The
compiler checks typed service connections. Devices still report actual negotiated
settings at startup. A general graph resolver cannot replace either check.

A narrow `build.rs` may expose existing configuration as Rust constants, check
required resolved NuttX options, or integrate target linking when needed. Outputs
belong in `OUT_DIR`; inputs must trigger rebuilds correctly. It is not mandatory
for crates needing no such work. [E05]

### Cargo-first firmware command

Cargo is now the canonical developer-facing build surface:

~~~sh
cargo firmware --app event-demo --platform pico2-mock
~~~

The repository alias invokes the narrowly scoped host-side
`platform/firmware` package. It reads firmware-entry metadata from the selected
app's existing `Cargo.toml`, validates the product-platform selection, and
passes explicit arguments to one common NuttX backend.

This is not a general `xtask` framework. The helper has one responsibility:
compose an existing Cargo app with one product platform and produce the firmware
image.

The remaining shell backend is limited to operations Cargo does not model:
NuttX/Kconfig configuration, qualified std preparation, ABI inspection,
NuttX Make/final link and image generation. Per-app/per-target wrapper scripts
and checked-in deployment scripts are removed.

A host Rust binary alone is not the complete MCU image; Cargo remains the command
surface while NuttX remains responsible for its native OS/image build stages.

Keep separate app/board build outputs and retain the source/toolchain versions,
final NuttX configuration, Cargo inputs, image map, and test results needed to
reproduce a build. Do not prescribe a canonical configuration IR, generated
manifests, a new schema suite, or a build-provenance database.

## 6. NuttX and browser integration that cannot be removed

A normal app-owned main still needs correct NuttX startup and linking. Verify
that it is entered once with the intended Rust initialization, ABI, TLS, threads,
timeouts, cleanup, and return behavior. Do not call a private mangled Rust main
from C as though it were a stable ABI. Preserve existing core/bridge tests, but
add tests of the real ordinary-binary/`std` path. [R13], [R14]

### Selected browser route: ordinary threads via Emscripten

Use direct app/service execution with web HALs and the tested
`wasm32-unknown-emscripten` pthread profile. A handwritten `main()`, std thread
creation/join, bounded blocking channels, TLS, timeout and disconnect checks
passed in actual Chrome 153.0.8010.52 (Linux) and Safari 26.6.1 (macOS 15.7.9).
Run 36274987242 tested source `7511b1cec08b4c92524b08fc7f1f21bf4281513f`;
both browsers ran the same artifact, each with three positive starts and two
negative controls. This is a completed probe, not a completed M6 app migration. [Q01]

The tested profile uses isolated `nightly-2026-09-25`, Emscripten 4.0.15 and
rebuilt std with matching pthread/atomic settings. `PROXY_TO_PTHREAD=1` puts
app main on a worker, four precreated workers include main, and UI-thread
blocking is forbidden. COOP/COEP isolation is required; fail clearly if absent.
These are qualified probe settings, not MCU resource recommendations or a
workspace-wide compiler upgrade. Size the thread pool, stacks and memory for
actual app needs and test exhaustion; do not inherit probe limits blindly. [Q01], [E08], [E09]

Do not confuse that profile with `wasm32-unknown-unknown`, whose ordinary std
thread creation is unsupported. Preserve the latter's existing no-thread mock
tests, but do not use its restriction to impose an async redesign on all apps. [E07]

The remaining browser-specific work is HAL I/O and application lifecycle. Keep
JavaScript API calls and promise/callback handling inside HAL/target support.
A completion must run on a browser context that remains schedulable; do not queue
it to a worker permanently occupied by a blocking Rust loop and then wait for it.
Use a responsive browser context and a bounded handoff to the service owner.
Never make the UI thread wait for a worker that needs UI-thread proxying. [E09]

Qualify one real browser I/O completion while a Rust owner waits, then saturation,
shutdown, late completions, permissions where relevant and UI responsiveness.
The probe does not establish every std API, production device access, persistent
storage/network semantics, mobile browser support, NuttX priorities or real-time
deadlines. A passing thread test cannot substitute for those contracts. [Q01]

Keep app-owned initialization and handwritten service wiring. No platform launcher,
mandatory cooperative executor or second NuttX-in-browser implementation is needed.
A simulation HAL remains an explicit mode, not proof of physical-device support.

## 7. Implementation roadmap

Milestone IDs retained from the earlier review identify the same necessary work;
M7's optional second browser route and all mandatory custom-tooling work are
removed. The browser thread probe is qualified as recorded above; no full
implementation milestone below is marked complete by this document.

### M0 — Preserve the baseline and settled model

Keep the existing contract and independent-output tests. Record one app per image,
app-owned main, and capability-first HALs. Do not change behavior just to rename a
file or split a package. Existing implementation details remain documented as such.

### M1 — Qualify ordinary main and std on NuttX

Extend the existing target build recipe for the selected app binary. Exercise its
actual main/startup, Rust-created threads, channel/time/TLS behavior, ABI, return,
and image linking. A core-only or host-library test is not a substitute. No xtask,
configuration compiler, or broad new runtime library is a prerequisite.

### M2 — Deliver one app-owned event-driven path

**Implemented for the native replay product path.** The app's main owns one
bounded inbox/wait point. Replay advertises its next useful poll deadline through
the camera contract; normal idle time blocks until that deadline. Camera polling
and local consumer work are separate, so a Busy recording/telemetry retry does
not poll the camera again. Busy sinks use a declared bounded retry timer because
those current sink APIs do not yet expose completion readiness.

Portable deterministic callers retain `CameraProduct::step(now_ms)` as a
compatibility/cooperative helper; it is no longer the native execution clock.
The camera, recorder, and monitor remain co-located so borrowed frames do not
cross threads.

Wakeup, bounded-inbox, timeout, split-consumer retry, replay deadline, and
end-to-end native behavior are qualified. Physical/NuttX camera interrupt
readiness and browser callback readiness remain later provider integration work.
Do not extract a general runtime framework unless another real host needs the
same execution code.

### M3 — Make cross-thread payload ownership safe

**Implemented for the native recording worker boundary.** Borrowed camera frames
remain local. Native storage allocates exactly `queued_records + 1` owned payload
buffers at construction, reserves a queue credit and free buffer before copying,
moves the filled buffer to the filesystem worker, and recycles it only after
completion/discard. A saturated queue therefore returns Busy before payload copy
or allocation.

The queue-credit scheme preserves the previous capacity semantics: N records may
wait while one is actively written. Tests deliberately stall the worker to prove
bounded backpressure, verify that completed buffers are reused, preserve sticky
I/O-failure/barrier behavior, and independently decode persisted bytes.

Worker completion is now explicit: `flush()` is the nonblocking commit barrier,
`finish()` closes/drains/joins and may block, and Drop only closes submission
and detaches so an event owner cannot hide an unbounded filesystem join in Drop.
Dropping without flush/finish is not a completion guarantee.

This remains a path-specific implementation, not a generic lease-pool/pub-sub
framework. Any future independently scheduled large-payload consumer should use
the same ownership principles only when that concrete boundary is introduced.

### M3a — Move concrete resources below capability-local HAL facades

**Capability-local provider selection is implemented.** The former global
`nxrs-hal` / `hal/platform` binding has been removed.

IMU/GNSS services acquire resources from `nxrs-imu` and `nxrs-gnss`;
deployment binds IMU/GNSS to mock implementations for the current
event-demo qualification profiles. This is the preferred ownership model for
sensor services, not a rule that apps can never use HAL facades directly.

The native nxrs app acquires camera/storage/transport through
`nxrs-camera`, `nxrs-storage`, and `nxrs-transport`. The build
selects those bindings through the `std` platform. Neither app nor service manifest
names concrete provider packages.

Product-facing build selection is now `cargo firmware --app <app> --platform
<platform>`. The app's existing Cargo metadata owns entry/resource settings;
the selected platform owns board/target facts and underlying capability-provider
bindings. Continue extending this pattern to physical/web products while proving
that minimal images exclude unselected Rust/C providers and registrations.

### M4 — Prove multi-app, multi-instance configuration

**Implemented.** `app/event-demo` and `app/dual-imu-demo` are separate
ordinary Rust firmware binaries that both reuse `nxrs-navigation-services`
and contain handwritten composition roots. Neither app depends on the other.

The dual-IMU app creates two independent `ImuService` instances and two
`FusionService` instances. Each IMU service independently acquires a HAL
resource at `start()`; with the mock provider both resources begin at sequence
1. The app pauses one instance while verifying the other continues, then resumes
the paused instance. This proves multi-instance service/resource ownership
without a registry, global HAL object, generated instance table, or universal
resource resolver.

Cross-image configuration is also qualified: the dual-IMU image selects only the
mock IMU provider and is checked not to select a GNSS implementation from the
event-demo image. Running it without an IMU provider must fail explicitly with
Unsupported rather than falling back to another image's configuration or an
implicit mock.

The existing event-demo qualification provides the platform half of M4: the same
unchanged app/service source is run on native and ESP32-S3/MPS2 NuttX QEMU and is
built for the actual Pico 2 board through distinct checked-in product-platform
profiles. Product platforms therefore vary below app/service policy.

### M5 — Qualify physical adapters and deadlines

Test the camera/storage/network paths for format negotiation, DMA/buffer
placement, overload, lifecycle, stack/memory bounds, and timing under the
intended workload. These paths still need appropriate physical coverage.

### M6 — Run the selected app in a real browser

Build on the passed ordinary-main/std-thread probe, not a required cooperative
service scheduler. Add the needed web HALs behind existing common APIs and run
the same app-owned initialization and thread/channel code. First qualify a real
browser I/O completion reaching a waiting Rust owner without blocking its callback
context; then test bounded queues, shutdown, late completion and UI responsiveness.
Handle unsupported capabilities explicitly. The probe is only M6's execution
prerequisite; application and HAL integration remain. This can proceed alongside
NuttX work once the relevant contracts stabilize.

## 8. What is deliberately not required

Do not turn the removed items into a second backlog of mandatory abstractions:

| Removed prescription | Use instead |
|---|---|
| `xtask` and a custom doctor/configure/explain CLI suite | Cargo plus existing focused target scripts. |
| App/deployment graph schemas, canonical IR, generated manifests | Normal Cargo selection, NuttX/board settings, typed Rust construction. |
| Mandatory generated bindings and per-HAL schema files | Small checked-in common HAL entries and local support information; generate only what integration actually needs. |
| Separate system-clock HAL packages and provider selection | `std::time` in execution code; explicit time inputs in behavior tests. |
| Four runtime packages, generic actor/inbox/outbox machinery | Ordinary service-specific loops/channels; isolate only real target differences. |
| Mandatory async browser service rewrite or custom thread API | Qualified std threads/channels via Emscripten; browser I/O adaptation stays inside HALs. |
| Universal clients, subscriptions, IDs, credits, and lease pools | Add each mechanism only for an actual protocol or measured data path. |
| Automated support/budget/trace dashboards | Focused tests, compiler checks, and measurements with recorded inputs. |
| A second NuttX-in-browser milestone | The chosen direct web HAL route. |

Keep the contracts and tests that make the smaller implementation trustworthy.
Simplicity does not mean removing bounded queues, resource ownership, readiness,
shutdown, HAL exclusion, or target qualification. It means implementing them
without unrelated framework machinery.

## References

Repository findings below retain the original review's exact source snapshot.
They are not claims that this documentation revision reran those tests. [Q01]
identifies the separately executed browser qualification at its exact source.
External runtime guidance is not evidence for an untested target configuration.

[R01]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/Cargo.toml
[R04]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/hal/api/src/lib.rs
[R05]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/service/src/camera.rs
[R06]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/app/nxrs/src/product.rs
[R07]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/platform/native/src/main.rs
[R09]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/hal/native/src/recording.rs
[R11]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/hal/nuttx/src/lib.rs
[R12]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/tools/check-architecture.py
[R13]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/docs/nuttx-sim.md
[R14]: https://github.com/yongkyuns/nxrs/blob/e6a11f739ef83f4e3f81ec8c7ab964cf7c5032bc/docs/nuttx-qemu.md
[E01]: https://doc.rust-lang.org/cargo/reference/cargo-targets.html
[E02]: https://doc.rust-lang.org/cargo/reference/manifest.html#the-metadata-table
[E03]: https://doc.rust-lang.org/cargo/reference/features.html
[E04]: https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html#platform-specific-dependencies
[E05]: https://doc.rust-lang.org/cargo/reference/build-scripts.html
[E06]: https://github.com/matklad/cargo-xtask
[E07]: https://doc.rust-lang.org/rustc/platform-support/wasm32-unknown-unknown.html

[E08]: https://doc.rust-lang.org/rustc/platform-support/wasm32-unknown-emscripten.html
[E09]: https://emscripten.org/docs/porting/pthreads.html
[Q01]: https://github.com/yongkyuns/nxrs/actions/runs/36274987242
