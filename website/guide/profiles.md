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

### Prepare the NuttX BSP

1. Start with the revisions of [NuttX](../../external/nuttx/) and
   [NuttX apps](../../external/nuttx-apps/) pinned by NXRS. For a
   supported board, inspect its schematic and existing defconfig for the
   console, flash and RAM layout, and the buses you need. For a new board on a
   supported SoC, add `boards/<arch>/<chip>/<board>/` with board headers,
   startup and bring-up sources, build rules, and
   `configs/<config>/defconfig`. Register the board in `boards/Kconfig`.
   A new SoC also needs a NuttX architecture or chip port.
   [NuttX board support](https://nuttx.apache.org/docs/13.1.0/components/boards.html)
2. In Kconfig, enable the controller, the required driver interface, and the
   board initialization hook that registers it. In the BSP, configure pin mux,
   power and reset as required by the hardware. Use the board's bring-up path
   to initialize controllers and register their endpoints before the app opens
   them. For example, `CONFIG_BOARD_LATE_INITIALIZE=y` enables
   `board_late_initialize()`, where NuttX can run setup that needs I/O or
   waits. [NuttX initialization hooks](https://nuttx.apache.org/docs/latest/reference/os/board.html)
3. Build a native NSH image first. In a clean, separate NuttX checkout with
   the board changes applied, the matching apps checkout at `../apps`, and
   the board's cross compiler, Kconfig and image tools installed, run the
   following with the actual board and configuration names. Use `-m` in
   place of `-l` on macOS. Enable the native resource settings in
   `make menuconfig` and use `make savedefconfig` to export defaults you
   want to keep in the BSP.

   ```sh
   ./tools/configure.sh -l -a ../apps BOARD:CONFIG
   make menuconfig
   make olddefconfig
   make
   ```

   Use the board's documented flashing procedure for the chosen bootloader
   and image layout. Boot to `nsh>`, check the expected nodes with `ls /dev`,
   and exercise the needed I/O. A visible node confirms registration. A
   transfer or sample checks the wiring and driver together.
   [NuttX build prerequisites](https://nuttx.apache.org/docs/latest/quickstart/install.html)

### Connect the BSP to NXRS

1. Put the BSP changes in a pinned revision, or add a
   [NuttX patch](../../upstream/nuttx/patches/) and register it in
   [upstream/build-patches.json](../../upstream/build-patches.json).
   The NXRS build extracts the pinned sources into its output directory
   before applying the registered patches.
2. Create `platform/<family>/profile.toml`. Give it a unique `name`, select
   `board = "<board>:<defconfig>"`, and set `target`, `crossdev`, `machine`,
   and `image`. Set the capability bindings in `hal` and the NuttX settings in
   `[kconfig]`. Use a checked-in profile for the same supported target as a
   reference. A new target or OS needs build support and the providers for the
   capabilities you intend to use.
3. For composed peripherals, add or extend
   `platform/nuttx/resources/<board>.toml`, using the board ID before the colon.
   Map each registered endpoint to the Kconfig requirements and native pin
   settings that produce it. Select those mappings and supply the actual
   wiring in the profile. See [Add a peripheral](hardware.md#add-a-peripheral).
4. Inspect the resolved profile with `cargo nxrs profile --platform <family>`.
   Run `cargo nxrs build --app <app> --platform <family> --dry-run`, then build
   without `--dry-run`. The build applies the profile to the board defconfig,
   runs NuttX Kconfig, and saves the effective settings in `resolved.config`
   in the build output. Boot the resulting image and verify the endpoints and
   application acquisition on the board.

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
