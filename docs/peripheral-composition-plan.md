# Peripheral composition implementation plan

This plan records the original composition-v1 migration and its qualification
sequence, followed by the accepted [static-device follow-up](#static-device-follow-up).
The [v1 architecture](peripheral-composition.md) describes current mechanics;
the [declarative build/static-device decision](static-device-composition.md)
defines the new target. Do not repeat completed v1 work or treat an old stage's
source paths as the current implementation. Physical claims still require S8.

The BSP resource follow-up resolves profile bindings and native pin settings
into both NuttX Kconfig and Rust endpoints. NuttX owns pin encoding and electrical
setup; Nxrs checks declared claims in the BSP's native numbering namespace.
CoreS3 and DevKit mappings, conflict checks, effective
configuration checks and input freshness are covered by existing build suites.
Additional BSP resources require verified registration support; this does not
complete pending physical qualification. See the [current resource flow](peripheral-composition.md#profile-composition-v1).

The original S0–S11 baseline was inspected at `3961a15d` on November 7, 2026.
The follow-up was inspected at `15f547594512528db7cae43a7bd1293ee54668e3` on
November 8, 2026. Recheck paths, tests and overlapping PRs at each implementation
head. Stages record required evidence, not an assertion that every gate passed.

## Working protocol

Implement one numbered step per focused PR, splitting a step further if its
acceptance criteria cannot be reviewed together. Start from the predecessor's
accepted commit. Each handoff must contain the commit SHA, changed paths,
commands/results, remaining limitations and the next eligible step. Update the
relevant capability README with actual behavior and qualification status.

Read `AGENTS.md`, `docs/hal-platform-architecture.md`,
`docs/nuttx-device-access.md` and the target architecture before editing.
Preserve public entry points, default sensor settings, terminal faults, overload
accounting and joined teardown unless the step explicitly changes their contract.
Use existing tests and add focused regression cases for newly introduced behavior.
Keep build-only dependencies out of target libraries and app/service manifests.

The sequence is S0 through S7, then S8 hardware qualification. S9 simulation can
proceed after S6 using host evidence. S10 generalizes after S7 and S9. S11 retires
legacy selection after all affected consumers have migrated. Physical claims
remain gated by S8 regardless of implementation progress. Hardware steps require
available devices and authorization for their actual effects. Record pending
hardware evidence and continue independent host work when devices are absent.

## S0 — Record the behavior being preserved

Read `hal/imu/src/{api,bmi270,cadence}.rs`,
`hal/imu/src/platform/nuttx/{mod,irq,signal}.rs`, `hal/imu/src/test_support.rs`,
`hal/acquisition/src/`, `tools/build-support/src/profile.rs`,
`tools/build/src/{deployment,cargo_graph}.rs` and
`platform/cores3/profile.toml`.

1. Trace profile values through `configure_hal` to resource acquisition. Record
   that driver selection currently accepts only BMI270 and construction is
   hardcoded. Record device path, address, frequency, wake and stack propagation.
2. Inventory the current startup/configuration checks, FIFO decode/overflow tests,
   ownership quarantine, cancellation, failed-start cleanup and join behavior.
3. Run the baseline commands below with Python 3.11+ and the pinned Rust toolchain.
   Inspect current CoreS3 and protected-build recipes without touching hardware.
4. Capture a short baseline in the PR description. Existing errors must be
   diagnosed separately from the refactor. Do not replace failed evidence with
   a different target's success.

Exit when the behavior inventory and actual test outcomes are available to S1.

## S1 — Extract the reusable BMI270 driver

Depends on S0. Primary paths are `driver/bmi270/`, `hal/imu/src/bmi270.rs`,
`hal/imu/src/adapters/`, both Cargo manifests, workspace membership and
`tools/check-architecture.py` with its regression tests.

1. Create the ordinary `nxrs-bmi270` package and move chip initialization,
   configuration, bounded FIFO decoding and stop operations into it. Retain
   pinned `bmi2` and `embedded-hal` versions, firmware blob provenance and current
   exact post-init/configuration checks.
2. Replace driver imports of HAL `DeviceError` and `ImuSample` with driver-owned
   errors and sample types. Keep chip-specific capabilities/configuration in the
   driver. It may use std and must remain independent of any Nxrs HAL dependency.
3. Add a private IMU sensor implementation that maps driver results into the
   existing public contract. Retain sequence, units, reception-time meaning,
   nominal interval, 100 Hz defaults and existing loss semantics.
4. Move protocol tests with the code and keep mapping and acquisition tests in the
   HAL. Add a bounded negative test for any conversion behavior the move changes.
   Update the dependency audit narrowly, preserving the ban on driver-to-HAL
   dependencies and app/service access to concrete drivers.

Run baseline checks, `cargo test --locked -p nxrs-bmi270`, and
`cargo check --locked -p nxrs-bmi270 --lib`. Inspect its Cargo graph for HAL/OS
coupling and retain the host protocol and NuttX build witnesses. Exit when
existing IMU callers and observations are
preserved and the reusable driver can be consumed without `nxrs-imu`.

## S2 — Separate NuttX resources and session execution

Depends on S1. Change `hal/imu/src/platform/nuttx/`, add `hal/imu/src/session.rs`
only for shared capability behavior, and reuse `nxrs-acquisition`.

1. Move descriptor-backed I2C behavior into `i2c.rs`, IRQ registration into its
   existing module, and delay/wait handling into focused resource modules.
   Keep `std::fs`, `std::io` and threading calls in Rust. Keep native ABI files
   beside the consuming resource adapter.
2. Separate sensor construction from the acquisition loop. Supply the loop with
   concrete sensor operations and accepted timing values. Remove its dependency
   on BMI270 constants without introducing a generic sensor API crate.
3. Preserve the supported I2C transaction semantics. If implementing a broader
   trait contract, test repeated starts, multiple operations, completion, errors
   and transfer bounds. Reject unsupported operations explicitly until supported.
4. Exercise every partial acquisition failure, including IRQ setup, sensor setup
   and worker start. Ensure sensor stop, IRQ detach, joined callbacks and resource
   release retain the correct order. A failed cleanup must retain quarantine.

Run baseline checks and `test_imu_i2c_ffi.py`. Exit with equivalent observable
behavior, no extra acquisition thread or queue, and an explicit account of which
I2C/IRQ operations have only host witness coverage.

## S3 — Make the common IMU contract precise

Depends on S2. Change `hal/imu/src/api.rs`, its sensor implementation,
`driver/bmi270` configuration support and the capability README. Review the pinned BMI270 API and the intended LSM6DSO data
sheet/driver before choosing shared configuration semantics.

1. Define unit-based requested/accepted configuration types for ODR, physical
   ranges and delivery latency. Resolve requests into concrete driver settings
   before resource acquisition, using the driver's supported settings. Validate
   combinations without duplicating capability tables in the HAL.
2. Add a configured acquisition entry point while keeping `open(sink)` with its
   current defaults. Unsupported requirements return a typed error before
   acquisition is published. Accepted configuration reports actual values.
3. Keep filter modes sensor-specific unless a shared property has a precise
   physical definition. Preserve current sample fields and their timestamp
   semantics. New timestamp provenance must be additive and explicitly tagged.
4. Test default equivalence, unsupported requests, actual-rate reporting and
   failure cleanup. Existing navigation tests must pass unchanged.

Exit when applications can specify common requirements without naming a chip,
and the supported combinations are documented with exact error behavior.

## S4 — Add typed composition resolution

Depends on S3. Primary paths are `tools/build-support/src/profile.rs`, a new
focused `composition.rs`, `hal/imu/bindings/`, and host resolver tests.

1. Implement composition version 1, resources, devices, wake choices and aliases as
   specified in the architecture. Use typed records/enums with bounded validation.
   Keep native resource paths in platform configuration.
2. Add the BMI270/I2C binding schema and an allowlisted constructor mapping. Reject
   unknown properties/models/buses, ambiguous matches, invalid addresses and
   inconsistent wake resources. Bindings identify implementations that exist.
3. Resolve a default IMU alias and validate resource claims. Distinct I2C addresses
   may share a bus. Duplicate physical devices and exclusive IRQ conflicts fail.
4. Translate legacy IMU profiles to the same internal graph at one resolver
   boundary, including their omitted-IRQ periodic semantics. Reject simultaneous
   legacy and v1 fields for the same capability. Keep unrelated capabilities in
   their current representation during incremental migration.
5. Reuse application `required-capabilities` validation against the resolved alias.
   Add deterministic serialization and source provenance. Implement schema
   version rejection and target/provider compatibility before code generation.

Run build-support tests, `test_deployment.py` and `test_platform_profiles.py`.
Exit with legacy/v1 equivalence and negative cases for unresolved references,
conflicting claims, incompatible aliases and simulation in hardware mode.

## S5 — Implement explicit overlay composition

Depends on S4. Change the shared resolver, `tools/build/src/main.rs`, build
options and `tools/build/tests/cli.rs`. Reuse the profile inspection command.

1. Implement the documented ordering, field replacement, whole-record replacement
   on compatible/kind changes, whole wake-table replacement and explicit removals.
   Overlay parsing must reject target/BSP/mode changes and recursive inclusion.
2. Add named-profile overlay lists and repeatable CLI `--overlay` arguments to
   build and inspection. Resolve relative paths against their declaring files.
   Plain builds must never discover overlays from the working directory.
3. Validate after all merges so an overlay can replace a referenced device and
   repair aliases together. Errors must identify the responsible file/property.
4. Print the resolved selection and provenance through profile inspection and
   dry-run. Hash normalized semantic inputs and record ordered source hashes.
   Track raw input fingerprints separately from semantic graph digests so that
   formatting/source changes rerun generation without changing the selected graph.

Test order-sensitive overrides, replacement with stale properties, deletion and
reference repair, invalid removals, mode mutation, inheritance cycles, missing
files, equivalent resolution and identical builds from different working
directories. Exit when the same input sequence always resolves identically and
the changed input/ordering appears in provenance and build identity.

## S6 — Connect resolution to normal builds and factories

Depends on S5. Change `tools/build-support/src/lib.rs`,
`tools/build/src/{build,reuse,cargo_graph}.rs`, `tools/build-support/cargo/hal.rs`,
`hal/imu/src/platform/` and detached-consumer tests.

1. Emit a versioned resolved-profile file atomically per build and implement
   `NXRS_RESOLVED_PROFILE` validation. NuttX profile builds use the frontend;
   Cargo adapters consume its receipt. Explicit host fixtures may resolve named
   profiles using `NXRS_PLATFORM` and `NXRS_SDK_ROOT` through the same resolver.
2. Generate typed constants and the selected checked-in factory into OUT_DIR.
   Use parsed enums and allowlisted mappings, escaping strings as Rust literals.
   Never interpolate profile text as Rust paths, shell commands or source code.
3. Update the NuttX factory to construct from the selected device binding.
   Preserve named exports and explicit unavailable acquisition for no-profile
   contract builds. Check physical identity at open and retain exclusive leases.
4. Keep driver dependencies ordinary Cargo declarations in v1. Qualify app-to-
   service-to-HAL transitive use and detached consumers. No build script may
   pretend it can enable a missing optional dependency after Cargo resolution.
5. Track profile, overlay, schema and generated-input freshness. Reject stale or
   target-mismatched resolved files and conflicting selection inputs. Extend the
   architecture audit to inspect the generated wiring and allowlisted mappings
   while retaining shared-code and native-boundary checks.

Run baseline checks, build-support/build tests, external-consumer tests and
`tools/check-hal-isolation.py` using its current documented arguments. Add a
same-output-directory A→B→A build regression to catch stale constructors and
resource constants. Exit when normal CLI, direct Cargo and detached entry points
select the same implementation without application source/manifest model flags.

## S7 — Prove reuse with a second supported composition

Depends on S6. First exercise BMI270 on two declared bus/address bindings through
host resource witnesses. Then add `driver/lsm6dso/`, its private IMU implementation
and binding as a separate focused change.

1. Select and pin a reviewed driver or implement the required chip operations.
   Record license, source/data-sheet revision and configuration assumptions.
2. Implement the common IMU contract using the driver's sensor-specific API.
   Test known raw-to-physical conversions independently, FIFO frame boundaries,
   invalid/status data, configuration acceptance and bounded initialization.
3. Build the replacement-device overlay through the normal S6 path. Verify that
   the constructor and resource values change together, with no new board Rust
   module or service code. Unsupported configurations still fail explicitly.
4. Compare default BMI270 behavior to S0 and label the new sensor's results as
   host-only until its physical qualification exists.

Exit when two different supported device models use the same application and
HAL contract, and switching profiles back cannot retain the previous binding.
This gate requires an implemented compatible with behavioral evidence.

## S8 — Qualify protected execution and physical deadlines

Depends on S6 for the BMI270 slice. Repeat after S7 for LSM6DSO. Use a spare or
explicitly authorized board. CoreS3 M135's unwired interrupt only qualifies the
periodic/FIFO path. Select an IRQ-wired fixture for interrupt evidence.

1. Inspect the pinned target's protected-build support and raw bus/GPIO interfaces.
   Build ABI witnesses for syscall pointers, ioctl layouts, buffer lifetime,
   interruption, cancellation and shutdown. Unsupported target mechanisms are
   blocking findings for the protected-mode claim.
   Run the [ESP32-S3 protected ABI witness](../tests/nuttx-std/imu-protected/README.md)
   first. Collect its separate kernel and userspace link evidence, then boot it
   on an authorized DevKit and require the serial pass marker within the declared
   deadline. This witness uses deterministic kernel test devices. Cross-build
   and boot the [Rust runtime witness](../tests/nuttx-std/imu-protected/README.md#rust-runtime-witness)
   in both modes to check group-local TLS, cancellation and joined cleanup.
   The matrix below requires the Rust acquisition path and physical sensors.
2. Prepare bounded recovery and an independently verified baseline before changing
   firmware. Keep signing/enrollment/runtime secrets outside tracked artifacts.
3. Define workload, ODR, watermark, FIFO headroom and latency/loss budgets before
   measurement. Measure IRQ-to-worker and IRQ-to-transfer, bus wait, drain time,
   sample age, loss, CPU and memory in equivalent flat and protected images.
   Start with the [physical acquisition witness](../tests/nuttx-std/imu-qualification/README.md)
   and enable its timing probe for the userspace stages. Keep the same firmware
   configuration across each flat/protected pair and retain a baseline without
   instrumentation to quantify the probe's overhead.
4. Repeat under declared camera/network load where authorized. Separate measured
   distributions/maxima from a proven worst-case bound. Sensor timestamps and
   reception timestamps must remain distinct.
5. Restore the original firmware/session and verify recovery. Record exact heads,
   toolchain, NuttX revision, build mode, wiring and sanitized result artifacts.

Run the hardware matrix on the same declared wiring in both NuttX build modes.
Use the [physical acquisition fixture](../tests/nuttx-std/imu-qualification/README.md)
to prove sample flow and joined shutdown before collecting timing measurements.

| Sensor and wake | Physical setup | Required builds |
| --- | --- | --- |
| BMI270 periodic | CoreS3 and stock M135, or equivalent declared raw I²C bus | Flat and protected |
| BMI270 interrupt | BMI270 with INT1 connected to a registered rising-edge GPIO | Flat and protected |
| LSM6DSO periodic | LSM6DSO at the address declared in the replacement profile | Flat and protected |

For the protected witness, verify `CONFIG_BUILD_PROTECTED=y` in the effective
configuration and record where the app runs. The pinned NuttX tree provides an
ESP32-S3 DevKit protected NSH configuration as a starting reference. Prepare
the equivalent raw resources for the selected fixture and prove the I²C/GPIO
path from the userspace app. [Pinned DevKit protected configuration](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/boards/xtensa/esp32s3/esp32s3-devkit/configs/knsh/defconfig)

Before each run, declare the exact bus speed, ODR, FIFO headroom, test duration,
load, and pass/fail bounds. Run at idle first, then apply the declared load.
Keep target cross-builds, QEMU mock execution and physical results separate.
A result counts toward this matrix only after the post-restore baseline passes.

Exit only for the combinations actually measured. If deadlines fail, locate the
delay before changing acquisition placement. A required kernel-side operation
must be reviewed as a specific resource mechanism with lifecycle evidence.

## S9 — Add the coordinated simulation platform

Depends on S6 and the documented IMU contract. Add `shared/simulation/`,
capability-local simulation implementations and a named simulation profile.

1. Define a logical clock, seeded world state and stable `(time, source, sequence)`
   event ordering. Add owned session leases, endpoint registration, a start barrier,
   bounded delivery and reset-after-join behavior. No dependency on HAL crates
   is allowed from the shared engine.
2. Make IMU and GNSS endpoints derive consistent observations from the same
   trajectory. OS factories share one session per run and apply the endpoint
   identities from the resolved profile. Expose explicit session construction
   to test harnesses and prevent scenario reuse while old callbacks are alive.
3. Preserve existing independent scripted-session tests as named qualification
   fixtures. Specify whether an endpoint allows subscribers or exclusive
   acquisition. Reopening a physical device's alias must retain Busy semantics.
4. Test repeated-run equality, cross-sensor consistency, cancellation while time
   is paused, overload, detach/rejoin and no state leakage between sessions.
5. For application determinism, route relevant timer/deadline inputs through an
   explicit logical-time harness and test differing host thread schedules. Until
   that passes, claim deterministic source sequences only.

Exit with a distinct simulation profile, coordinated IMU/GNSS evidence and
hardware-mode rejection tests. Keep scenario configuration out of services.

## S10 — Generalize the proven pattern

Depends on S7 and S9. Migrate GNSS next using `driver/ubx/` for receiver-specific
behavior and the same profile resolver. Preserve receiver configuration rollback,
UART settings and existing event semantics. Use `std::io` or an existing embedded
stream trait according to the actual driver contract.

Reusable controller access belongs in `driver/<bus>`; the NuttX-only I²C adapter
now follows this structure with its native ABI inventory and tests. Keep OS-specific
interfaces local and retain direct std operations for files/sockets.
Add a macOS/Linux source only when a real supported API or external-device
transport is identified. A native source can implement the capability directly.
Keep camera/radio/native client ownership out of this migration until separately
scoped and qualified.

Exit per capability with existing callers preserved, profile equivalence,
standalone driver tests and the appropriate provider qualification report.

## S11 — Retire redundant admission and legacy configuration

Depends on completed migrations of every affected consumer and fixture.

1. Inventory every `hal_enabled`, `hal_mock`, legacy profile field and fixture
   selector. Map each to a resolved physical, simulation, qualification or
   unavailable construction path. Preserve API-only builds and explicit mocks
   used by unit tests.
2. Migrate profile documentation, CLI help, isolation checks and build-script
   consumers before removing obsolete flags. Remove the legacy translation only
   with a documented version boundary and actionable diagnostics for old inputs.
3. Retain `target_os` at OS wiring boundaries. Preserve unrelated functional
   features such as replay/recording/UDP and detector model selection.
4. Run all affected host, native, core-only and firmware qualification paths.
   Record which hardware combinations remain pending. Update normative docs and AGENTS rules to
   match implemented behavior, removing migration-only guidance.

Exit when one profile/overlay resolution is the sole product selection authority,
simulation is explicit, old selector paths are retired and required checks pass.

## Verification commands and handoff

These commands exist at the baseline. Use Python 3.11+ and the repository's
pinned toolchain. Clear unrelated selection variables in the test environment.

~~~sh
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py
python3 -m unittest discover -s tests/host -p test_hal_boundaries.py
python3 -m unittest discover -s tests/host -p test_imu_i2c_ffi.py
python3 -m unittest discover -s tests/host -p test_deployment.py
python3 -m unittest discover -s tests/host -p test_platform_profiles.py
cargo test --locked -p nxrs-imu -p nxrs-gnss -p nxrs-acquisition
cargo test --locked -p nxrs-build-support -p nxrs-build
~~~

Run the applicable subset for each step plus the architecture audit/regressions
for every ownership or native-boundary change. Run additional external-consumer,
isolation and physical suites at their specified gates. Reuse existing CI jobs.
Add cases where they catch a new failure mode. Add a job only for coverage an
existing job cannot provide.

Each agent handoff must answer these questions in its PR description.

1. Which step and predecessor SHA were used?
2. Which normal entry points and compatibility behaviors changed?
3. Which exact commands passed, failed or could not run, and why?
4. What evidence is host, emulator, target ABI or physical evidence?
5. Which acceptance criteria remain open, and which step can start next?


## Shared IMU device contract follow-up

BMI270 and LSM6DSO now implement `driver/peripheral::imu::Imu` directly. This
supersedes S1/S7's private normalization wrappers: device settings, measurements
and errors are shared below the HAL. The selected HAL factory constructs the
chip; shared acquisition accepts only the normalized device contract. HAL IMU
keeps stream metadata, readiness, acquisition and lifecycle policy. Drivers own
their binding metadata; product profiles select actual resource assignments.
No other peripheral is migrated by this change; the static-device work below
remains separate.

## Static-device follow-up

**Accepted direction; implementation in progress in
[issue #246](https://github.com/yongkyuns/nxrs/issues/246).** D0–D5 implement the
[declarative build and static-device contract](static-device-composition.md).
They supersede S4/S6's local emitter, unconditional-driver and singleton
construction mechanisms. Preserve the earlier steps' behavior and qualification
evidence; do not restart the sensor extraction or simulation work.

IMU and GNSS now use the shared pre-Cargo configuration/native handoff, optional
chip and resource features, and canonical per-device storage. Product-owned
catalogues and inherited path drivers use the same frontend. Common instance
checks have a build-only third-capability witness; this does not qualify a new
production HAL. The steps below retain the design sequence and evidence gates.
Physical results remain specific to the revision and configuration measured.

Continue from main; do not create stacked PRs.
Update affected architecture and regression checks in the same PR that replaces
the mechanism they enforce. Every intermediate PR must retain passing relevant
gates; D5 audits the integrated result, not deferred repair of earlier tests.
Preserve the current NuttX resource contracts and GNSS receiver/descriptor lease
release ordering. Keep
physical S8 results and unrelated radio/camera work separately scoped.

### D0 — Freeze the contract and record the baseline

Record current build duties and lifetime behavior before moving them. Shared
preparation now loads package-owned catalogs and generates direct implementation
selection with typed resource bindings. The composition crate supplies named references
to apps and use additive optional chip dependencies. Services receive normalized
device references. IMU and GNSS have permanent `Device` references and static
per-device acquisition storage; uncertain cleanup quarantines only that device.

Extend the typed resource construction convention to permanent instances. Settle
generated-input delivery and package-specific compiler/link configuration; the
public permanent-device versus session API; static state inventory; initialization
and recovery policy; and reacquisition/terminal-event ordering. Define active
instances from consumed, admitted capabilities and how unconsumed board-profile
records are validated without pulling in their HAL packages or catalogs.
The IMU API and retirement ordering are recorded in the static-device contract.
No design may rely on per-HAL host callbacks, arbitrary Rust snippets,
a global provider flag or Cargo features reaching undeclared dependency edges.

Preserve public defaults and configured sampling semantics. IMU consumers now
use `Device::open`/`open_default` and `Session`; root compatibility functions and
the old session alias are removed. No-profile builds return `Unsupported`.
IMU now retains its lease through final delivery, sink destruction and cleanup.
Exit with the reviewed contract and a bounded baseline of relevant tests.

### D1 — Prove shared preparation and Cargo integration

The first slice, D1a, implements package-relative catalog discovery and
shared typed rendering for the actual IMU/GNSS outputs. Both local build scripts
become generic adapters, with their custom callbacks and the IMU emitter removed.
The production shared preparation API is exercised by a detached Cargo witness;
catalog changes invalidate receipts and generated outputs. Isolated
IMU-only, GNSS-only and camera-only consumers check package closure.

D1a initially preserved provider flags, OUT_DIR handoff, simulation preparation
and native compilation in generic adapters. The current frontend now publishes
validated configuration and consumed native archives before target compilation;
adapters consume that handoff. Preserve the corresponding freshness, helper
exclusion and target-context checks as composition is extended.


Start in `tools/build-support/src/{composition,profile,resolved,lib}.rs`,
`tools/build/src/{build,cargo_graph}.rs` and existing native build support.
Extend the controller's pre-Cargo `configure_profile` path. Preserve package-relative catalog discovery and typed resource generation while
moving their outputs into the pre-Cargo handoff.

First demonstrate one profile-selected optional implementation through the real
Cargo entry point, using a small fixture or a minimal production feature seam
before D2 migrates all IMU implementations. Include app-to-service-to-HAL and a
detached consumer. Feature
activation must precede target compilation and preserve aliases, inheritance,
defaults and Cargo's additive feature semantics. Do not copy the detector's
mutually exclusive model policy into sensor selection.

For each migrated package, replace all local build-script duties: typed
generation, per-package provider selection, simulation configuration, native source compilation/linking and
profile/source/header/environment freshness. Keep native sources beside the
runtime owner and compile only consumed, feature-enabled helpers. Generated
files stay outside source and have target/configuration/schema/generator identity,
atomic writes and concurrent-build isolation.

Exercise NuttX builds through `cargo nxrs build`, including detached products
and qualification apps. Cargo adapters and editor checks consume prepared inputs;
unprepared or stale firmware inputs fail clearly. Preserve ordinary Cargo for
no-profile contracts and explicit host fixtures. Update affected generation,
freshness and native-helper tests in this PR.
Exit with a working handoff; do not delete HAL scripts while any of their
responsibilities lacks a replacement.

The configuration cleanup uses version 2 binding declarations, fixed I²C/UART
resource contracts and direct checked-in implementation selection. The IMU platform catalogue imports
its driver-owned declarations; GNSS does likewise through its catalogue. This
removes field-projection schemas and both model-enum tables. The reusable NuttX
I²C adapter is owned by `driver/i2c`. Profile/overlay semantics are preserved;
detached build tests compile the actual IMU factory across both sensor
selections. Optional dependency activation and per-device ownership are
implemented below; current native preparation follows the consumed target graph.

### D2 — Make implementations optional and additive

The catalogues now declare additive optional chip dependencies. Product builds
derive features through direct catalogue dependencies before compiling; graph
witnesses cover either IMU driver alone and both. Catalogue-owned
`resource-features` additionally select I2C/SPI adapters from validated devices.
Graph and native archive witnesses cover I2C-only, SPI-only, mixed and unconsumed
IMU configurations. Feature names remain catalogue-owned ordinary Cargo features.

Change `platform/Cargo.toml`, catalogue module admission, binding metadata
and concrete construction; keep chip dependencies out of `hal/imu`. Use ordinary optional dependencies and one feature gate per named
implementation module. Keep direct implementation selection when extending generation to multiple
instances; the global `MODEL` match has already been removed. No redundant `source` metadata,
generated file selector, feature-per-instance or duplicate `mod selected`.

Compile BMI270-only, LSM6DSO-only and both implementations using normal build
entry points. Explain Cargo feature unification and distinguish production
packages from host/dev dependencies. Preserve explicit `probe` feature and
observer activation semantics. Runtime detection is opt-in policy, not a
prerequisite for this work.
Replace emitter/model-match architecture expectations alongside the new wiring.
Exit with valid mixed-model compilation and target graph evidence that the
unselected driver is absent in each single-model production build.

### D3 — Enforce canonical static objects and per-device ownership

**IMU implemented in PR #306; GNSS now follows the same ownership boundary.** Named accessors refer to permanent objects;
same-model and mixed-model devices acquire independently. Tests cover concurrent
settings/events, Busy aliases, failure isolation, cancellation-setup rollback,
terminal backpressure, callback retirement and per-instance IRQ quarantine.
Actual generated factories compile across profile switches in one Cargo output.
Flat/protected target compile/link checks do not qualify physical concurrency.
In the pinned 32-bit Xtensa release/probe witness, two BMI270 I²C slots and one
LSM6DSO I²C slot occupy 144 + 144 + 168 = 456 static bytes; four aliases emit three
objects. These sizes exclude worker stacks, transient buffers and OS allocations.
GNSS owns per-receiver static storage and
retains admission through terminal delivery, sink destruction and restored UART cleanup.

Generate one actual static object for each active physical device with private
configuration, persistent instance storage and ownership/fault state. Common HAL
machinery controls construction; aliases return the same object. Keep the
session's stop/join and typed callback contract independent from permanent identity.

Migrate `hal/imu/src/platform/nuttx/{mod,device,irq}.rs`, session integration and
every global poison path. Define private runtime access and prove that Drop cannot
allow reacquisition while old workers/callbacks can still use the instance.
Preserve rollback, IRQ detach, sensor stop, worker-start failure and quarantine.
Report static bytes separately from stack, queue, transient and backend allocation.

Exercise two same-model devices and a mixed pair concurrently with independent
configuration/events/state. Verify same-object Busy through two aliases,
other-device availability after a fault, normal cleanup/reopen and the chosen
terminal-event ordering. Normalize physical endpoint identities and reject
duplicate owners and conflicting IRQ/CS claims. Sharing an I2C controller must
retain qualified transaction serialization without claiming the whole bus for
one sensor. Exit with deterministic lifecycle/resource witnesses; hardware
behavior remains a separate gate.

### D4 — Prove reuse and detached package closure

GNSS now uses the same declarative preparation path and a platform catalogue. Preserve
UART configuration/restore ordering, receiver initialization rollback, terminal
faults and joined cleanup. Use it to demonstrate that the schema and instance
machinery do not encode IMU knowledge; do not expand into an unrelated HAL rewrite.

Use a small declarative fixture to show that a supported binding can be added
without central chip enums or emitter branches. A board profile's unconsumed
devices must not add packages, helpers or linked instance storage. Exercise isolated IMU-only and
GNSS-only detached dependency closures; retain camera-only and other existing
external-consumer witnesses. Do not let a fixture that includes both sensor HALs
hide undeclared sibling catalog access. Preserve explicit simulation/qualification
profiles and portable service logic.

The external product witness additionally selects a product-owned catalogue,
inherits a renamed optional path driver through Cargo workspace metadata,
compiles generated construction, and rejects stale workspace/catalogue inputs.
Git/registry binding-source discovery remains outside the current path-package
contract; it must not be approximated by an all-features fetch or another resolver.

Exit with common preparation for both capabilities, no migrated HAL-local
`build.rs`/host emitter, complete package-owned inputs and correct native-helper
selection/linking. Record any separately scoped HALs that still await migration.

### D5 — Audit integration and publish implementation evidence

Confirm that D1–D4 updated `tools/check-architecture.py::inspect_generated_imu_factory`
and `tests/host/test_hal_boundaries.py` alongside their corresponding changes.
They must enforce the new ownership/generation boundary rather than the old
emitter and model match. Retain checks for trusted
construction, escaped data, dependency direction, private ABI glue, named exports
and platform-free app/service code.

Verify the adapted `tools/build/tests/imu_profile_freshness.rs` exercises the
real common preparation path. Confirm `nuttx_helper_ownership.rs` and
`native_header_freshness.rs` retained their behavior as compilation moved. Exercise one output
directory through endpoint/model/instance-count/catalog changes and back to the
original profile, plus stale-target and concurrent-profile negative cases.

The implementation acceptance matrix is:

| Area | Required evidence |
| --- | --- |
| Generic build ownership | IMU and GNSS plus a declarative fixture use common generation; no chip-specific central branch or hidden sibling file |
| Implementation selection | One implementation for two same-model instances; both for a mixed pair; unused target driver absent in single-model builds |
| Identity and ownership | Actual static identity, shared aliases, duplicate-acquire Busy and independent device availability |
| Lifecycle and faults | Cancel/drop/cleanup barriers, successful reopen, partial-start and IRQ failures, per-device quarantine |
| Resource sharing | Duplicate endpoint/IRQ/CS rejection and correct shared-controller transactions with documented enforcement limits |
| Freshness and package use | A→B→A, changed catalog/instance count, stale/mismatched inputs, concurrent profiles and isolated detached consumers |
| Native and memory evidence | Selected helper/ABI checks, final map/code/data inspection, static state versus remaining allocation inventory |
| Portability | The same application/service policy and normal entry points work across selected sensor implementations |

Run the relevant existing commands above, build-support/build tests, boundary
regressions and external-consumer suites. Inspect actual target Cargo artifacts
and the final linked image; Cargo.lock presence or total binary size alone is
not proof of driver inclusion/exclusion. Cross-link the affected NuttX paths
with their pinned ABI witnesses. Run physical S8 checks only when the required
hardware is available and authorized, and label pending cases.

Update architecture docs, HAL READMEs and AGENTS rules to the implemented state,
remove superseded migration guidance and retain source/qualification provenance.
A documentation-only PR references #246; it does not close this implementation
issue or count as runtime acceptance.
