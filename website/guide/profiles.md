# Profiles and overlays

A platform profile describes the configuration used for a host or firmware build. It selects hardware resources, device instances, implementations, and target settings outside reusable application code.

## Application and platform are separate inputs

An application is a Cargo package. A profile describes where it runs. For example:

```sh
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock --dry-run
```

The same application can be built with a different profile without adding board branches to its `main()` function.

`cargo nxrs platforms` lists platform families. `--platform` uses a family's default profile; `--profile` selects a specific configuration. When both are supplied, the profile must belong to that family. Use an explicit profile when reproducing a build.

## Direct Cargo builds

For host code using profile-selected HALs, `NXRS_PLATFORM` selects the profile. For example, check the storage package with the Linux host integration:

```sh
NXRS_PLATFORM=linux cargo check --locked -p nxrs-storage
```

On Windows, PowerShell uses an environment assignment before the command and removes it afterward:

```powershell
$env:NXRS_PLATFORM = "windows"
cargo check --locked -p nxrs-storage
Remove-Item Env:NXRS_PLATFORM
```

Use the profile matching the host and package, with the prerequisites in the owning HAL's documentation. Standard-library-only programs, including the first examples, do not need a profile. Without one, HAL crates expose portable contracts and leave hardware implementations unbound.

## Configuration responsibilities

| Input | Owns |
| --- | --- |
| Cargo manifest | Packages, dependencies, and optional features |
| Application firmware metadata | Binary, NuttX command, entry priority, and stack |
| Platform profile | OS, board, devices, resources, and implementation bindings |
| Overlay | An ordered change to a base profile |
| NuttX BSP and Kconfig | Native pin/controller configuration and device registration |

A reusable driver implements a device protocol. The profile selects its physical instance and resource bindings. A service receives the resulting capability, while the application chooses how that service participates in the firmware.

## Inspect a profile before changing it

```sh
cargo nxrs profile --help
cargo nxrs build --help
```

The seven default configurations are under [platform/profiles](../../platform/profiles/). Use [tests/profiles](../../tests/profiles/) to find a specific test or qualification input.

The resolver validates device/resource bindings and records the resolved configuration with the build. Checked BSP mappings under `platform/nuttx/resources/` connect admitted native settings to Rust endpoints.

## Apply an overlay

An overlay changes devices, resources, or aliases in a base composition. The frontend applies repeated `--overlay` arguments in order. Keep product overlays in version control and review the resolved configuration after changing them.

For example, two revisions of an IoT device may use different supported peripheral parts while keeping the same application-level capability. Their profiles describe that difference. The application should not need to inspect a board name to choose its behavior.

## Device instances

Driver availability and physical instances are separate. Several instances can use the same driver, but each physical endpoint needs its own configured identity and ownership. The available acquisition API depends on the capability.

The [peripheral composition reference](../../docs/peripheral-composition.md) contains the schema and concrete device examples. The [static-device design](../../docs/static-device-composition.md) describes named devices and ownership. Keep those implementation details separate from the basic application/build workflow.
