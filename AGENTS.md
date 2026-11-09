# HAL architecture rules

Read `docs/hal-platform-architecture.md` and `docs/nuttx-device-access.md` before
changing a HAL, its platform wiring, or a native bridge. These are implementation
requirements, not aspirational guidance.

For the accepted declarative-build/static-device follow-up, also read
`docs/static-device-composition.md` and the follow-up section of
`docs/peripheral-composition-plan.md`. They define the target for issue #246:
shared host preparation, additive driver features and canonical static physical
instances. Existing local emitters, global sensor guards and audit expectations
are migration baselines, not requirements to reproduce them. Replace affected
checks with the new invariants in the implementation PR while preserving
portability, dependency, native-boundary and lifecycle coverage. The OS/provider
`selected` module convention below is separate from additive chip modules.

- For peripheral composition work, also read `docs/peripheral-composition.md`
  and `docs/peripheral-composition-plan.md`. Composition version 1, ordered
  overlays, resolved-profile build wiring, and the migrated IMU/GNSS bindings
  are implemented. Use the profile resolver and normal build/inspection entry
  points. Keep retired sensor selectors and their translation paths out of new wiring.
- Keep managed capability APIs and acquisition implementations in their HALs.
  HALs are capability/subsystem boundaries, not vendor register HALs. Reuse Rust
  `std` directly; do not add a mandatory driver layer for ordinary OS operations.
  Capability-specific native adapters stay with their owning HAL. Threads,
  queues and executors require an execution need, not merely another component.
  Reusable chip protocols belong in `driver/<device>` with sensor-specific APIs
  over existing Rust bus traits. Shared synchronous device contracts belong in
  `driver/peripheral`; chip drivers implement them directly. The IMU contract
  owns device configuration, physical measurements and device errors; its HAL
  adds stream metadata, resource ownership and delivery/lifecycle policy.
  Drivers must not depend on capability HALs, applications or services. Use `hal/<capability>/src/platform/` for OS
  implementations; the root
  `platform/` tree owns product profiles, device catalogues and concrete construction, not HAL runtime code.
  Profile bindings name actual operating systems: `linux`, `macos`, `windows`,
  or `nuttx`. The coordinated host `simulation` binding is available to IMU
  and GNSS only through a distinct simulation profile. `mock` is an explicit
  test fixture. Rust `std` is an available
  environment, never a platform, provider choice, profile, or Cargo feature.
  Shared Rust behavior belongs in ordinary modules outside `src/platform/`;
  OS-specific code stays under its actual OS directory. Do not copy std code
  into OS folders or reintroduce `std`/`native` pseudo-platforms.

- Reusable hardware resource access belongs in `driver/<resource>`. `driver/i2c`
  and `driver/gpio` support NuttX only and own their descriptors and embedded-hal
  implementations. GPIO owns registration/rollback and reports teardown failures
  through a task-context hook; the consuming HAL retains quarantine policy.
  `hal/acquisition` is shared worker/readiness implementation support, with one
  NuttX poll/pipe binding used by IMU and GNSS. It has no profile binding and
  shares worker code, not an executor. `imu::Session` and `gnss::Session` describe
  acquisition lifetimes; synchronous chip operations remain driver contracts.
  Catalogues supply concrete resource configuration; sensor HALs own acquisition policy; chip drivers
  consume bus traits without depending on the concrete adapter. Do not add speculative OS
  backends, host stubs or another logical profile alias. Host witnesses inject
  test-only bus bindings. IMU drivers own their `bindings.toml` declarations;
  the IMU catalogue under `platform/devices/imu` imports them through normal Cargo path dependencies. The IMU HAL must not depend on concrete sensors or the catalogue. Product
  profiles select BSP resources, native pin settings and peripheral addresses. The BSP mapping
  derives NuttX Kconfig and endpoint paths together; reject conflicting overrides
  and unsupported registrations. Shared tooling emits fixed
  typed resource bindings and checked-in factory selections. Acquisition consumes
  constructed devices through the shared contract, without chip constructor traits.
  Do not restore per-field Rust schemas or model-enum mappings.

- Product profiles are the single source for device/resource composition and
  supported pin selections. Reuse native NuttX Kconfig, board headers and
  initialization for MCU pins and electrical behavior. Resource `kconfig` tables
  name the binding's admitted native pin symbols; mappings identify their native
  numbering namespace and fixed reservations for conflict checks. Do not maintain
  a second GPIO/pinctrl catalogue or infer native port/mux encoding. NuttX checks
  ranges/dependencies; verify its effective configuration against requested values.
  Checked BSP mappings under `platform/nuttx/resources/`
  derive NuttX configuration and Rust endpoints together. NuttX owns actual MCU
  initialization and registration; declarations cannot invent missing BSP support. A resolved hardware profile must select concrete
  supported devices; simulation uses a separate explicit profile and coordinated
  session. Use mock bindings in explicit test and qualification profiles.
  Physical IMU and GNSS aliases select supported hardware bindings.
  Generate only validated constants and checked-in factory selections. Cargo
  dependencies must be declared before build scripts run. Keep runtime policy
  out of build support and preserve detached-consumer qualification.

- Each capability owns its Rust API and errors. Share a contract only when the
  consumers intentionally implement the same protocol. Similar enum variants
  alone are not a shared contract.
- Rust owns HAL behavior: validation, state machines, resource ownership, queues,
  synchronization, retries, cancellation, recovery, deadlines, and error policy.
- Use qualified `std::fs`, `std::io`, `std::net`, `std::thread`, and `std::sync`
  directly where they express the operation. Do not add a C/POSIX wrapper layer
  or duplicate an ordinary Rust implementation for each platform. IP data
  transport uses one shared Rust socket implementation. Wi-Fi/BLE
  control still needs native radio APIs. Share identical Rust protocol/resource
  helpers (such as Linux GIO); keep radio schemas and device policy local.
  For each NuttX endpoint, prefer qualified std operations first: UART data uses
  `File` and `Read`/`Write`; termios, readiness and device controls may need thin
  native bindings. Use embedded-hal adapters for transaction/pin semantics std
  cannot express, and chip/protocol drivers where Nxrs owns that behavior.
  Profiles borrow declarative bindings, overlays and aliases from Zephyr; NuttX
  retains MCU wiring and VFS registration. Resource/device/alias records describe
  composition, not mandatory runtime layers or a replacement for std handles.
- Private C/Objective-C is allowed only for target-header ABI translation and
  native SDK operations that Rust/std cannot directly express. Keep bindings
  mechanical. A scalar ABI does not make a C state machine a thin binding.
- Calls between Rust modules use typed Rust functions or private traits. Do not
  export a C HAL API, opaque context pointers, numeric status protocols or raw
  command/event layouts merely to connect Rust providers to Rust callers.
  C exports needed only by SDK witnesses belong under `tests/`, enabled only
  for those tests. Actual SDK callbacks still use the required native ABI.
- Keep private ABI glue beside its owning Rust provider in that provider's
  `ffi/` directory. Do not create a parallel `hal/<capability>/ffi/<platform>`
  tree or impose one C HAL API across platforms. Each provider consumes only
  the SDK/target operations it needs; shared behavior and contracts stay in Rust.
- Avoid redundant `nxrs_` prefixes on private functions, native ABI symbols,
  internal cfg names and files. Retain namespacing only where an external
  interface or global symbol collision makes it necessary.
- App and service source, manifests and build scripts must contain no OS/board
  cfg, native imports, device paths or platform inspection. Apps may depend on
  portable catalogues under `platform/devices/<capability>` to select named devices;
  services receive their normalized HAL references and have no production catalogue
  dependency. Other app/service platform dependencies remain forbidden. Only catalogue
  wiring and qualification may use `imu::construction`; apps/services cannot create
  a second instance for an endpoint. Catalogues contain no acquisition/runtime policy.
  Platform code stays application-agnostic; build frontends accept the selected entry
  as input and never name an app or add an app-specific runtime crate.

- Platform/provider selection belongs in crate/module wiring and platform-local
  factories. Shared APIs, controllers, workers, and I/O algorithms must not use
  platform/provider flags or inspect platform identity. Supply explicit resource
  configuration and provider-reported capabilities instead. Use Rust's
  `target_os` only at OS module/factory boundaries; profiles enable capabilities
  and select explicit simulation or qualification fixtures. HALs use one
  internal `hal_provider` value at their module boundary.
- Cargo manifests own component dependencies. Ordinary additive features may
  select optional functionality, formats, algorithms and driver code at their
  owning module boundary. Do not maintain a global feature-name allowlist.
  Keep OS selection in target configuration and physical resource instances in
  explicit configuration/ownership. Optional diagnostic instrumentation uses
  the explicit `probe` feature. The detector product/model rules below still apply.
- Group capability admission and acquisition selection in each HAL's
  `src/platform/mod.rs`. Select source modules as `selected`; keep constructors
  and unavailable stubs in those implementations, and host source-injection
  witnesses in separate test wiring. Prefer fixed named exports. The selector
  must not contain implementation bodies or force its entire public namespace
  into the crate root. A crate's public API should be explicit in `lib.rs`;
  private placeholder/backend types need not become public to support `open()`.
  Gate a coherent module once instead of repeating its condition on each type,
  implementation and constructor. All HAL selectors must name their exports;
  wildcard re-exports in selectors or crate roots are forbidden by the audit.
  Storage recording and transport UDP are explicitly accessible as
  `storage::recording` and `transport::udp` through the additive, nondefault
  `recording` and `udp` Cargo features, regardless of hardware admission. Use
  their explicit constructors; do not restore profile-dependent root factories
  or OS whitelists. The distinct synchronous `create_record_file` acquisition
  keeps one signature and an explicit unavailable result.
  Shared file replay is explicit functionality, not a platform backend:
  `nxrs-camera` exposes it through its additive `replay` Cargo feature, with no
  default and no hardware-profile requirement. Its native CLI forwards that
  functionality normally; core-only library fixtures disable the CLI defaults.
  Do not reintroduce OS whitelists or unavailable replay stubs. See
  `hal/camera/README.md` for the public API and compatibility aliases.
  Keep model features in the detector HAL; services consume `detector::Detector`
  directly, without model feature forwarding or compatibility facades.
  Qualification controls belong in fixtures and named experiment cases, not
  production platform APIs. Use one `probe` feature for optional diagnostics
  and test witnesses. Select coherent tracing implementations at a module boundary;
  keep cfg conditions out of acquisition logic and individual tracing fields/methods.
  Ordinary tests and executables need no diagnostic feature.
  Product profiles select a detector model and execution budget; build frontends
  derive its internal dependency features automatically when the product consumes
  detection. Do not add manual model switches, a second model-selection
  environment variable, or model features to app/service manifests. Explicit
  model features are for HAL qualification only. Model dependencies remain
  explicit in the detector HAL; benchmark choices use typed cases, never boolean
  cfg aliases or a feature for each measurement axis.
- Hardware acquisition is event-driven. GNSS UART reads and IMU FIFO
  draining belong to HAL providers, never service polling timers. Services receive
  normalized typed messages through bounded channels. ISRs and interrupt-context callbacks only
  perform qualified bounded notification; never call service handlers, allocate,
  wait for queue capacity, or invoke std/Crossbeam channels from ISR context.
  Defer parsing and I/O to task context. Declare overload/loss policy, retain
  terminal faults independently of delivery, wake waits on cancellation, and
  explicitly join acquisition before releasing resources. An unwired sensor may
  explicitly use an ODR-derived HAL timer feeding the same FIFO path; a timer is
  only a wake hint, never proof of data readiness. A configured IRQ failure must
  fail rather than silently switching modes. See
  `docs/concurrency-event-communication.md`.
- Keep native callback registration, execution-context constraints, and lifetime
  barriers explicit. Moving behavior to Rust must preserve quiescence before
  freeing callback context, DMA buffers, descriptors, or interface leases.
- Core-only legacy fixtures belong under `tests/`, including compatibility
  wrappers. They do not justify C/POSIX implementations in production HALs.
- Keep `hal/ffi-boundaries.toml` limited to actual native ABI glue. No radio
  migration exceptions remain. Do not introduce C orchestration or relabel it
  as ABI glue to make an audit pass. Private D-Bus schemas and SDK callback
  state belong to each owning Rust HAL; native objects stay opaque.

Run `python3 tools/check-architecture.py`, its host regression tests, and the
relevant HAL/provider qualification tests. Preserve overload, rollback, sticky
failure, shutdown, and callback-lifetime tests when moving implementation. A
host build is not evidence of NuttX ABI or physical hardware qualification.

The detector capability owns its common Rust API, model preprocessing and
native compute adapters. Its OS implementations live under
`hal/detector/src/platform/`. The root `platform/` tree supplies build/product wiring only. Shared models and
SDK compute adapters remain ordinary modules in that HAL. Gate optional models
at their module boundary with Cargo features; do not create generated model cfg
aliases or gate individual implementation items. Model compilation is independent
of profile admission; the platform factory owns acquisition. A Rust detection
service owns scheduling, temporal confirmation and alert policy. Keep camera
acquisition independent of model execution: transfer bounded owned copies,
discard excess work, and join workers before releasing their storage. Product
wiring supplies execution budgets; shared inference and alert policy must not
inspect platform identity. Expired/failed inference means unavailable, never
clear. Keep model/weight provenance and independent engine, dataset and device
measurements reproducible; video FPS and inference FPS are separate metrics.
On fixed-priority targets, nonblocking handoff must also avoid spinning behind
a preempted lower-priority producer. Use bounded try-lock transfers or a
qualified protocol; a channel method named `try_recv` alone is not that proof.

Keep detector compute contexts scoped to synchronous native numerical execution.
The Rust provider owns serialization and restoration of prior native state;
preprocessing, postprocessing and idle time must not leave a newly enabled
accelerator context behind. Qualify CPU/execution constraints explicitly.

For detector performance qualification, compare the exact pinned model's vendor
benchmark with measured pipeline stages, and distinguish CPU time from wall time
under load. Report achieved observation cadence separately from the requested
sampling budget and video FPS. Keep memory/cache/clock choices in product wiring
and qualify their resource costs and physical throughput; throttling alone does
not establish an efficient detector implementation.

Detection delivery is a versioned application protocol above the HAL. Keep its
bounded Rust wire contract shared by camera publication and gateway validation;
do not serialize SDK objects or expose native detector details. Media progress
and detection progress have independent freshness deadlines. Slow subscribers
must not block capture or accumulate inference work. Clients expire stale or
failed detection as unavailable, including across backgrounding and source
replacement. Live boxes require a qualified source-frame-to-rendered-frame
mapping; arrival order and similar clocks alone are not synchronization.
Carry source identity through encoded access units and match the actual decoded
image before rendering annotations. Reject missing, ambiguous or stale mappings;
bound retained image buffers and clear overlays without further network input.
For localized detectors, confirmed presence follows accepted objects. Rejected
raw peaks must not indefinitely preserve a positive state after boxes disappear.

# Native camera clients

Read `docs/client-camera-architecture.md` before changing client session ownership,
playback admission, native decoding/rendering or capture. Keep UI and native
resource lifetimes in each client; share the gateway schemas and one behavioral
fixture corpus. A camera owns its peer, detection namespace, retry and capture
jobs. Retiring resources and replaced enrollments still occupy decoder slots
until callbacks, renderers, WHEP cleanup and capture finalization have joined.
Full screen reuses its camera session; offscreen renderers release their surfaces.
Do not trade exact decoded-image identity for timestamp or arrival-order guesses.
Android MediaCodec input tokens and byte-buffer output require the independent
alternating-pixel witness before changes; the SDK texture path is not qualified.
Run the shared core/native suites and `clients/tests/multicamera_reference.py` with
the relevant native runner. Report actual rendered FPS separately from codec and
source FPS, and label emulator/simulator results separately from physical devices.
