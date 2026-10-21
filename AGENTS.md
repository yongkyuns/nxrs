# HAL architecture rules

Read `docs/hal-platform-architecture.md` and `docs/nuttx-device-access.md` before
changing a HAL, its platform wiring, or a native bridge. These are implementation
requirements, not aspirational guidance.

- Each capability owns its Rust API and errors. Share a contract only when the
  consumers intentionally implement the same protocol. Similar enum variants
  alone are not a shared contract.
- Rust owns HAL behavior: validation, state machines, resource ownership, queues,
  synchronization, retries, cancellation, recovery, deadlines, and error policy.
- Use qualified `std::fs`, `std::io`, `std::net`, `std::thread`, and `std::sync`
  directly where they express the operation. Do not add a C/POSIX wrapper layer
  or duplicate an ordinary Rust implementation for each platform. IP data
  transport belongs to the shared std-based transport provider. Wi-Fi/BLE
  control still needs native radio APIs. Share identical Rust protocol/resource
  helpers (such as Linux GIO); keep radio schemas and device policy local.
- Private C/Objective-C is allowed only for target-header ABI translation and
  native SDK operations that Rust/std cannot directly express. Keep bindings
  mechanical. A scalar ABI does not make a C state machine a thin binding.
- Keep private ABI glue beside its owning Rust provider in that provider's
  `ffi/` directory. Do not create a parallel `hal/<capability>/ffi/<platform>`
  tree or impose one C HAL API across platforms. Each provider consumes only
  the SDK/target operations it needs; shared behavior and contracts stay in Rust.
- Platform/provider selection belongs in crate/module wiring and platform-local
  factories. Shared APIs, controllers, workers, and I/O algorithms must not use
  platform/provider flags or inspect platform identity. Supply explicit resource
  configuration and provider-reported capabilities instead.
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
