# Profiles and overlays

A platform profile is a build-time description of a product or explicit test configuration. It supplies device-tree-like composition without turning application code into a board-selection layer.

## Two entry points, one selection

For direct host Cargo work, select a profile with `NXRS_PLATFORM`:

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 2000
```

For a firmware build, use the frontend’s profile selection:

```sh
cargo nxrs build --app event-demo --profile esp32s3-qemu-mock --dry-run
```

Do not set unrelated profiles through both mechanisms. The frontend resolves the selected build configuration. Without a profile, HAL crates expose portable contracts; they do not silently select mocks.

## Platform families and product profiles

`cargo nxrs platforms` lists the public platform families. A family such as `cores3` describes a supported environment and has a default product profile. `--profile` selects a particular product or qualification configuration. When combined with `--platform`, it must belong to that family.

Useful examples include `host-demo` for synthetic host sensors, `esp32s3-qemu-mock` for the ESP32-S3 emulated demo, `mps2-an521-mock` for the Cortex-M33 QEMU proxy and `pico2-mock` for the Pico 2 build configuration. These names are not statements that physical sensors have been qualified.

Inspect the [host profiles](../../platform/profiles) and [NuttX product profiles](../../platform/nuttx/platforms), and use the current command help rather than copying a profile schema from an old discussion:

```sh
cargo nxrs profile --help
cargo nxrs build --help
```

## What a profile owns

Profiles describe resources, devices, logical aliases, provider selection and target/build settings. NuttX profiles also select the BSP and admitted native configuration. Package declarations describe supported implementations and resource constraints; Cargo remains the dependency resolver.

The application still owns its binary, NuttX command name, entry priority and stack metadata. Product configuration must not introduce OS imports, device paths or concrete chip choices into reusable application/service source.

## Native BSP wiring remains authoritative

Checked mappings under `platform/nuttx/resources/` connect a supported BSP resource to NuttX configuration and Rust endpoint bindings. Product pin settings use the binding’s admitted native Kconfig symbols; the configuration is checked after NuttX resolves it.

Do not duplicate endpoint paths and fixed configuration in several places. Do not invent a second GPIO numbering scheme or infer a native pin encoding from a label. Conflicts, unsupported registration and unavailable endpoints must fail explicitly.

## Overlays are deliberate changes

An overlay changes a base product composition. Order matters. The build/profile commands support repeated `--overlay` arguments; use their help for the accepted command shape and a checked-in overlay as the schema reference.

Keep a product overlay in version control. Review the resolved devices, resource bindings, native configuration and generated build receipt after applying it. A successful parse is not proof that the physical wiring matches the declaration.

Changing a sensor implementation should not require changing a service that consumes the same capability contract. Nevertheless, selecting a different device still requires its own driver, resource and hardware qualification.

## Multiple device instances

Do not assume every capability has the same multi-instance API. The current IMU work supports named device instances; the GNSS path retains its default-session API. The dual-IMU demonstration uses explicit primary and secondary device references with `ImuService::start_on`.

A second physical IMU needs a second admitted physical device binding, including the `imu_secondary` alias for that example. Two synthetic owners are useful ownership evidence, not proof that any board has two working physical IMUs.

Read [peripheral composition](../../docs/peripheral-composition.md), [the static-device decision](../../docs/static-device-composition.md) and [the implementation plan](../../docs/peripheral-composition-plan.md) for the distinction between implemented behavior and remaining work.
