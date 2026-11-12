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

To add a sensor on a supported bus, export its driver type and declare the
compatible and bus in the driver's `bindings.toml`. Set its capability in
`package.metadata.nxrs.device`. Register the driver once as a renamed optional
path dependency in this crate's NuttX dependency table. Cargo uses that alias as
the driver's feature. Then select the sensor and its resources in the board
profile.

```toml
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
