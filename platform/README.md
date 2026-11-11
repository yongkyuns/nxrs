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

The composition crate imports driver-owned binding declarations through normal
Cargo dependencies. For NuttX profiles, `cargo nxrs build` enables the
selected drivers and prepares the validated instance declarations. Each HAL owns acquisition, lifecycle and cleanup. The composition crate owns
the typed constructors.

Mock and simulation devices come from their HALs. With no profile, the primary
accessors return unavailable devices. Use the explicit `probe` feature and
`composition::probe` for physical IMU diagnostics.
