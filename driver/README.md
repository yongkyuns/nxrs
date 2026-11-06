# Device drivers

Repository-owned reusable driver mechanisms and physical device protocols belong
here. NuttX device-driver sources remain in `external/nuttx`, including the
ESP32-S3 camera work. Portable device protocols can use ordinary Rust and existing
bus traits; OS driver implementations can use their OS APIs. Drivers must not
depend on application/service policy or platform launchers.

The synthetic NuttX sensor and syscall fault injectors are qualification-only
fixtures in `tests/nuttx/c`, not production drivers. Scripted HAL substitutes live
in each capability's `src/platform/mock.rs`. Board pin assignments, registration
and configuration belong in `platform`, while OS-device adaptation belongs in
`hal/<capability>/src/platform/<os>`.

`linux/gio/` is the `nxrs-linux-gio` Cargo package. Linux Wi-Fi and BLE providers
share it through ordinary dependencies. The package owns opaque GIO resources,
call/reply mechanics and one native ABI archive. Each HAL owns its radio-specific
D-Bus schemas, control sequencing and errors. Rust std supplies memory, strings,
clocks, sleeping and callback ownership; the remaining FFI expresses SDK
operations unavailable in std.

`acquisition/` is the `nxrs-acquisition` Cargo package. It shares the task-context
lifecycle of GNSS/IMU event producers: bounded sink admission, loss counts,
terminal-fault retention, explicit stop/join and resource ownership through
worker completion. Each HAL supplies its device wait, cleanup and error type.
`acquisition/src/mock.rs` supplies scripted fixture mechanics. On Unix targets,
`acquisition/src/readiness.rs` shares Rust descriptor ownership and cancellation;
native ABI glue remains beside each provider.
