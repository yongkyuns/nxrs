# HAL architecture rules

Read `docs/hal-platform-architecture.md` and `docs/nuttx-device-access.md` before
changing a HAL, its platform wiring, or a native bridge. These are implementation
requirements, not aspirational guidance.

- Keep each hardware-resource abstraction and its implementations in its HAL
  crate. Use `hal/<capability>/src/platform/` for implementations; the root
  `platform/` tree owns product profiles and build wiring, not HAL code.
  Profile bindings name actual operating systems: `linux`, `macos`, `windows`,
  or `nuttx`; `mock` is an explicit test fixture. Rust `std` is an available
  environment, never a platform, provider choice, profile, or Cargo feature.
  Shared Rust behavior belongs in ordinary modules outside `src/platform/`;
  OS-specific code stays under its actual OS directory. Do not copy std code
  into OS folders or reintroduce `std`/`native` pseudo-platforms.

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
- Platform/provider selection belongs in crate/module wiring and platform-local
  factories. Shared APIs, controllers, workers, and I/O algorithms must not use
  platform/provider flags or inspect platform identity. Supply explicit resource
  configuration and provider-reported capabilities instead. Use Rust's
  `target_os` only at OS module/factory boundaries; profiles enable capabilities
  and select explicit mocks, not a second OS identity via feature/cfg values.
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
SDK compute adapters remain ordinary modules in that HAL. A Rust detection
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
