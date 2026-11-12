# Platform composition

Applications use `composition::imu` and `composition::gnss` to select named
devices and pass their HAL references to services. Enable the Cargo features
for the capabilities the application consumes. The build resolves compatible
drivers, resources and aliases from the profile and generates one static
object per selected physical endpoint. Aliases share that object's lease and
fault state.

```rust,ignore
let session = navigation::ImuService::new(inputs.imu)
    .start_on(composition::imu::primary_imu())?;
```

## Register a driver

To add a sensor on a supported bus, export its driver type and declare the
compatible and bus in the driver's `bindings.toml`. Set its capability in
`package.metadata.nxrs.device`. Register the driver once as a renamed optional
path dependency in the composition crate's NuttX dependency table. Cargo uses that alias as
the driver's feature. Then select the sensor and its resources in the board
profile.

```toml
[target.'cfg(target_os = "nuttx")'.dependencies]
sensor = { package = "nxrs-sensor", path = "../driver/sensor", optional = true }
```

The exported type takes the bus adapter as its first type parameter. IMU drivers
that store a delay provider declare `stores-delay = true` in their device
metadata and take the delay type as a second parameter. The build generates the
typed constructor reference. Each HAL owns acquisition, lifecycle and cleanup.

Mock and simulation devices come from their HALs. With no profile, the primary
accessors return unavailable devices. Use the explicit `probe` feature and
`composition::probe` for physical IMU diagnostics. Drivers that support the
sensor-reference diagnostic declare `sensor-reference = true` and export
`enable_sensor_reference`.

## Select resource dependencies

The capability feature enables the portable HAL and shared construction support.
Separate additive features enable the concrete bus adapters. This catalogue
declares the mapping in its own metadata:

```toml
[package.metadata.nxrs]
device-capabilities = ["imu", "gnss"]
resource-features = { i2c = "i2c", spi = "spi" }

[features]
i2c = ["dep:nxrs-i2c"]
spi = ["dep:nxrs-spi"]
```

The values must name ordinary features declared by that catalogue. A product
catalogue can use different feature names or group several dependency edges in
one feature. The frontend derives the union required by devices of consumed
capabilities and asks Cargo to enable it alongside the driver features. An
I2C-only IMU configuration therefore excludes the SPI adapter and native helper;
a mixed I2C/SPI configuration selects both. Native preparation consumes Cargo's
resolved graph. These graph guarantees do not by themselves measure final
firmware size.

## Own the catalogue in an external product

A product can keep its composition and drivers outside the NXRS checkout. Select
the composition package relative to the product's Cargo workspace root:

```toml
[workspace.metadata.nxrs]
device-catalogue = "composition"

[workspace.dependencies]
sensor = { package = "product-sensor", path = "drivers/sensor" }
```

The composition member declares its capabilities and imports the driver through
the ordinary optional dependency edge:

```toml
[package.metadata.nxrs]
device-capabilities = ["imu"]
resource-features = { i2c = "sensor-bus" }

[features]
imu = ["dep:nxrs-imu"]
sensor-bus = ["dep:nxrs-i2c"]

[target.'cfg(target_os = "nuttx")'.dependencies]
sensor = { workspace = true, optional = true }
```

These excerpts show the registration fields; the composition package still owns
its checked-in typed factory, resource construction, generic preparation adapter
and normal Rust dependencies. The driver owns its `device` metadata and binding
files. No SDK chip registry or generator model branch is needed. The product
application declares the composition as a direct dependency, with the portable
capability feature enabled; renaming and workspace inheritance are supported.

From the NXRS checkout, use the same build frontend with the external application
manifest:

```sh
cargo nxrs --workspace-root /path/to/nxrs build \
  --manifest-path /path/to/product/app/Cargo.toml --profile product-board
```

The SDK root supplies profiles, BSP contracts and pinned native inputs. If the
product workspace has no catalogue selector, the SDK catalogue remains the
default. A product catalogue replaces only the capabilities it declares; the
SDK continues to supply the others. Cargo normalizes inherited dependency paths
and aliases. The product
workspace manifest, catalogue manifest, driver manifests and binding bytes enter
the receipt and generated-input freshness checks; changing the selector or an
inherited path requires a fresh build preparation.

Catalogue discovery currently reads local path sources, including inherited
workspace path dependencies. Ordinary Git and registry dependencies remain valid
Cargo declarations, but their binding files are not discovered. A requested
device must have a discoverable binding; explicit binding imports from unsupported
sources fail with a source diagnostic. Discovery does not trigger an all-features
dependency fetch. A remote catalogue publication contract needs a future
integration with Cargo's resolved package locations.
