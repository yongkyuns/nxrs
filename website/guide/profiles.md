# Profiles and overlays

A platform profile describes the configuration used for a host or firmware build. It selects hardware resources, device instances, implementations, and target settings outside reusable application code.

## Application and platform are separate inputs

An application is a Cargo package. A profile describes where it runs. For example:

```sh
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock --dry-run
```

The same application can be built with a different profile without adding board branches to its `main()` function.

`cargo nxrs platforms` lists platform families. `--platform` uses a family's default profile; `--profile` selects a specific configuration. When both are supplied, the profile must belong to that family. Use an explicit profile when reproducing a build.

## Portable development with Cargo

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

NuttX firmware and qualification builds use `cargo nxrs build`; their Cargo adapters require its prepared inputs. For host fixtures, use the profile matching the host and package, with the prerequisites in the owning HAL's documentation. Standard-library-only programs, including the first examples, do not need a profile. Without one, HAL crates expose portable contracts and leave hardware implementations unbound.

## Configuration responsibilities

| Input | Owns |
| --- | --- |
| Cargo manifest | Packages, dependencies, and optional features |
| Application firmware metadata | Binary, NuttX command, entry priority, and stack |
| Platform profile | OS, board, devices, resources, and implementation bindings |
| Overlay | An ordered change to a base profile |
| NuttX BSP and Kconfig | Native pin/controller configuration and device registration |

A reusable driver implements a device protocol. The profile selects its physical instance and resource bindings. A service receives the resulting capability, while the application chooses how that service participates in the firmware.

## Add a NuttX board

1. Start with a board configuration in the pinned NuttX BSP. Verify that its
   defconfig boots and that board bring-up registers the controllers or device
   nodes your application needs. Use a pinned BSP revision with the needed
   initialization, or add a [NuttX patch](../../upstream/nuttx/patches/) and
   register it in [upstream/build-patches.json](../../upstream/build-patches.json).
2. Create `platform/<family>/profile.toml`. Give it a unique `name`, select
   `board = "<board>:<defconfig>"`, and set `target`, `crossdev`, `machine`,
   and `image`. Set the capability bindings in `hal` and the NuttX settings in
   `[kconfig]`. Use a checked-in profile for the same supported target as a
   reference. A new target or OS needs build support and the providers for the
   capabilities you intend to use.
3. For composed peripherals, add or extend
   `platform/nuttx/resources/<board>.toml`, using the board ID before the colon.
   Map the BSP's registered endpoints to their required Kconfig and native pin
   settings. Select those mappings and supply the actual wiring in the profile.
4. Inspect the resolved profile with `cargo nxrs profile --platform <family>`.
   Run `cargo nxrs build --app <app> --platform <family> --dry-run`, then build
   without `--dry-run`. Boot the image and verify the endpoints and acquisition
   on the board.

For a variant within an existing family, add a named file under
`platform/<family>/profiles/` and set `extends` to the base profile's name.
Use an overlay for a wiring or device change you want to apply to an existing
profile. See the [CoreS3 profile](../../platform/cores3/profile.toml),
[its BSP mapping](../../platform/nuttx/resources/esp32s3-m5-cores3.toml), and the
[worked sensor example](examples.md#add-a-second-sensor-on-cores3).

## Required and optional devices

Cargo enables the capability APIs the app uses. The profile determines which
implementations are available on the target. A missing optional binding leaves
the HAL unbound. Handle `Unsupported` when opening the device and continue
with the features the platform can provide. A device declared in the profile
can still fail to open if it is missing or fails to initialize.

For hardware the app requires, list the capability in its manifest.

```toml
[package.metadata.nxrs]
required-capabilities = ["camera"]
```

`cargo nxrs build` rejects a profile with a missing required binding before
compilation. A mock binding in a qualification profile can satisfy this
check. Opening a physical device verifies that acquisition can start.

## Inspect a profile before changing it

```sh
cargo nxrs profile --help
cargo nxrs build --help
```

The default configurations are under [the platform directories](../../platform/). Use [tests/profiles](../../tests/profiles/) to find a specific test or qualification input.

The resolver validates device/resource bindings and records the resolved configuration with the build. Checked BSP mappings under `platform/nuttx/resources/` connect admitted native settings to Rust endpoints.

## Apply an overlay

An overlay changes devices, resources, or aliases in a base composition. The frontend applies repeated `--overlay` arguments in order. Keep product overlays in version control and review the resolved configuration after changing them.

For example, two revisions of an IoT device may use different supported peripheral parts while keeping the same application-level capability. Their profiles describe that difference. The application should not need to inspect a board name to choose its behavior.

## Device instances

Driver availability and physical instances are separate. Several instances can use the same driver, but each physical endpoint needs its own configured identity and ownership. The available acquisition API depends on the capability.

The [peripheral composition reference](../../docs/peripheral-composition.md) contains the schema and concrete device examples. The [static-device design](../../docs/static-device-composition.md) describes named devices and ownership. Keep those implementation details separate from the basic application/build workflow.
