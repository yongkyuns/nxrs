# Profiles and overlays

A platform profile describes a product's hardware and build configuration. Profiles provide device-tree-like composition while keeping board selection out of reusable application code.

## Select a profile

For direct host Cargo builds, use `NXRS_PLATFORM`:

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

For firmware builds, use the frontend's profile option:

```sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock --dry-run
```

Keep the selections consistent when using these entry points. Without a profile, HAL crates expose portable interfaces with no mock providers selected.

## Platform families and product profiles

`cargo nxrs platforms` lists the platform families. A family such as `cores3` describes an environment and has a default product profile. `--profile` selects a particular product or test configuration. When combined with `--platform`, the profile must belong to that family.

Examples include `host-demo` for synthetic host sensors, `esp32s3-qemu-mock` for the ESP32-S3 emulated application, `mps2-an521-mock` for the Cortex-M33 QEMU model and `pico2-mock` for the Pico 2 build configuration.

Inspect the [host profiles](../../platform/profiles) and [NuttX product profiles](../../platform/nuttx/platforms). The command help describes the current options:

```sh
cargo nxrs profile --help
cargo nxrs build --help
```

## What a profile configures

Profiles describe resources, devices, logical aliases, provider selection and target/build settings. NuttX profiles also select the BSP and supported native configuration. Package declarations describe implementations and resource constraints; Cargo resolves the dependencies.

Application metadata defines the binary, NuttX command name, entry priority and stack size. Keep OS imports, device paths and concrete chip selection in the platform integration rather than reusable applications and services.

## BSP wiring

Mappings under `platform/nuttx/resources/` connect supported BSP resources to NuttX configuration and Rust endpoints. Pin settings use the binding's native Kconfig symbols. The build checks the effective configuration after NuttX resolves it.

Keep endpoint paths and fixed configuration in these mappings. Use the BSP's pin numbering and reject conflicting settings, unsupported registrations or unavailable endpoints.

## Apply overlays

An overlay changes a base product configuration. Order matters. The build/profile commands accept repeated `--overlay` arguments; use the command help and a checked-in overlay as a starting point.

Keep overlays in version control. Review the resolved devices, resource bindings, native configuration and build record, and check them against the board wiring.

Changing a sensor can leave services unchanged when the new driver provides the same capability interface. Test its resource operations and device behavior on the selected hardware.

## Multiple device instances

The IMU interface supports named device instances. GNSS uses its default-session API. The dual-IMU example selects primary and secondary device references through `ImuService::start_on`.

A second physical IMU requires its own device binding and the `imu_secondary` alias used by that example. Synthetic profiles test instance ownership; hardware profiles provide the actual device configuration for on-board tests.

See [peripheral composition](../../docs/peripheral-composition.md), [the static-device decision](../../docs/static-device-composition.md) and [the implementation plan](../../docs/peripheral-composition-plan.md) for implementation details and ongoing work.
