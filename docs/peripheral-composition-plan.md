# Peripheral composition implementation plan

This is the executable work breakdown for the
[target architecture](peripheral-composition.md). The software steps produce
separate PR evidence. Protected-mode and physical timing claims require S8.
Baseline inspected at `3961a15d` on November 7, 2026. Recheck paths and APIs on the
branch used for each step. The architecture document defines the target contract.
This plan defines migration order and acceptance evidence.

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
`driver/acquisition/src/`, `platform/build-support/src/profile.rs`,
`platform/build/src/{deployment,cargo_graph}.rs` and
`platform/nuttx/platforms/m5stack-cores3-gnss.toml`.

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
`hal/imu/src/sensors/`, both Cargo manifests, workspace membership and
`tools/check-architecture.py` with its regression tests.

1. Create the ordinary `nxrs-bmi270` package and move chip initialization,
   configuration, bounded FIFO decoding and stop operations into it. Retain
   pinned `bmi2` and `embedded-hal` versions, firmware blob provenance and current
   exact post-init/configuration checks.
2. Replace driver imports of HAL `DeviceError` and `ImuSample` with driver-owned
   errors and sample types. Keep chip-specific capabilities/configuration in the
   driver. It must compile without std and without any Nxrs HAL dependency.
3. Add a private IMU sensor implementation that maps driver results into the
   existing public contract. Retain sequence, units, reception-time meaning,
   nominal interval, 100 Hz defaults and existing loss semantics.
4. Move protocol tests with the code and keep mapping and acquisition tests in the
   HAL. Add a bounded negative test for any conversion behavior the move changes.
   Update the dependency audit narrowly, preserving the ban on driver-to-HAL
   dependencies and app/service access to concrete drivers.

Run baseline checks, `cargo test --locked -p nxrs-bmi270`, and
`cargo check --locked -p nxrs-bmi270 --lib`. Inspect its Cargo graph for std/OS
dependencies and use an installed core-only target for an additional compile
witness where available. Exit when existing IMU callers and observations are
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

1. Define capability reporting and requested/accepted configuration types for
   ODR, physical ranges and delivery latency. Validate allowed combinations,
   including restrictions between independently supported values.
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

Depends on S3. Primary paths are `platform/build-support/src/profile.rs`, a new
focused `composition.rs`, `hal/imu/bindings/`, and host resolver tests.

1. Implement composition version 1, resources, devices, wake choices and roles as
   specified in the architecture. Use typed records/enums with bounded validation.
   Keep native resource paths in platform configuration.
2. Add the BMI270/I2C binding schema and an allowlisted constructor mapping. Reject
   unknown properties/models/buses, ambiguous matches, invalid addresses and
   inconsistent wake resources. Bindings identify implementations that exist.
3. Resolve a default IMU role and validate resource claims. Distinct I2C addresses
   may share a bus. Duplicate physical devices and exclusive IRQ conflicts fail.
4. Translate legacy IMU profiles to the same internal graph at one resolver
   boundary, including their omitted-IRQ periodic semantics. Reject simultaneous
   legacy and v1 fields for the same capability. Keep unrelated capabilities in
   their current representation during incremental migration.
5. Reuse application `required-capabilities` validation against the resolved role.
   Add deterministic serialization and source provenance. Implement schema
   version rejection and target/provider compatibility before code generation.

Run build-support tests, `test_deployment.py` and `test_platform_profiles.py`.
Exit with legacy/v1 equivalence and negative cases for unresolved references,
conflicting claims, incompatible roles and simulation in hardware mode.

## S5 — Implement explicit overlay composition

Depends on S4. Change the shared resolver, `platform/build/src/main.rs`, build
options and `platform/build/tests/cli.rs`. Reuse the profile inspection command.

1. Implement the documented ordering, field replacement, whole-record replacement
   on compatible/kind changes, whole wake-table replacement and explicit removals.
   Overlay parsing must reject target/BSP/mode changes and recursive inclusion.
2. Add named-profile overlay lists and repeatable CLI `--overlay` arguments to
   build and inspection. Resolve relative paths against their declaring files.
   Plain builds must never discover overlays from the working directory.
3. Validate after all merges so an overlay can replace a referenced device and
   repair roles together. Errors must identify the responsible file/property.
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

Depends on S5. Change `platform/build-support/src/lib.rs`,
`platform/build/src/{build,reuse,cargo_graph}.rs`, `hal/imu/build.rs`,
`hal/imu/src/platform/` and detached-consumer tests.

1. Emit a versioned resolved-profile file atomically per build and implement
   `NXRS_RESOLVED_PROFILE` validation. Direct Cargo consumers resolve named
   profiles with declared overlays using `NXRS_PLATFORM` and `NXRS_SDK_ROOT`.
   Both entry points must share the resolver and produce the same graph.
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
   acquisition. Reopening a physical device's role must retain Busy semantics.
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

Extract a shared resource HAL only when a second consumer needs its exact bus
protocol. Move its native ABI inventory and tests with it. Keep OS-specific
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
cargo test --locked -p nxrs-imu -p nxrs-gnss -p nxrs-acquisition --lib
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
