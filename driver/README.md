# Device drivers

Repository-owned reusable driver mechanisms and physical device protocols belong
here. NuttX device-driver sources remain in `external/nuttx`, including the
ESP32-S3 camera work. Portable device protocols can use ordinary Rust and existing
bus traits; OS driver implementations can use their OS APIs. Drivers must not
depend on application/service policy or platform launchers.

The [peripheral composition design](../docs/peripheral-composition.md) makes this
split explicit. Chip packages such as `driver/bmi270/` expose their
sensor-specific configuration, samples and errors over `embedded-hal` interfaces.
They have no dependency on capability HALs or profile selection. BMI270 and
LSM6DSO directly implement the synchronous IMU contract in `driver/peripheral`.
Each consuming HAL adds stream metadata and owns acquisition lifecycle.
Retain existing upstream dependencies and protocol corrections when extracting
a driver. `bmi270/` and `lsm6dso/` own their respective IMU protocols. `ubx/`
owns receiver-specific GNSS behavior. Keep protocol tests with each driver
and use the [implementation plan](../docs/peripheral-composition-plan.md) for
capability integration and physical qualification gates.

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

`i2c/` and `gpio/` own reusable NuttX resource access and its private target-header
glue. Capability-specific native adapters, such as BLE and Wi-Fi control, remain
inside their owning HALs. Ordinary files, sockets and threads use Rust `std`
directly; they do not require another driver layer.

Shared acquisition execution lives in [`hal/acquisition`](../hal/acquisition/README.md).
Execution resources are an explicit implementation choice, not a requirement of
every driver. The current chip contracts are synchronous and create no workers.
