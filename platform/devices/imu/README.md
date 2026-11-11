# IMU device catalogue

`nxrs-imu-devices` is the portable selection surface for applications. Profiles
and overlays select concrete drivers and resources here; services and `hal/imu`
consume normalized contracts. The catalogue contains construction, not acquisition
or application policy. Separate capability catalogues avoid pulling unrelated
peripherals into a consumer's dependency graph.

```rust,ignore
let session = navigation::ImuService::new(inputs.imu)
    .start_on(imu_devices::primary_imu())?;
```

`[devices.front_imu]` generates `imu_devices::front_imu()`.
`[aliases] imu = "front_imu"` selects `primary_imu()` as a second reference to
that same object. `imu_backup = "rear_imu"` adds `backup()`; physical device
names remain accessible directly. A single IMU infers its default alias.
Conflicting accessor names and duplicate physical endpoints fail resolution.
There is no runtime name lookup. Mock supplies `primary_imu()` and `secondary()`;
coordinated simulation supplies `primary_imu()`. Without a profile, `primary_imu()`
returns an unavailable device.

The dependency direction is:

```text
app -> IMU catalogue -> IMU HAL + concrete drivers/resource adapters
service -------------> IMU HAL -> normalized peripheral contract
```

The HAL exposes `construction::Instance` for checked platform declarations.
The catalogue generates one static instance per validated physical endpoint;
aliases share it. Resource opening and sensor initialization are separate typed
callbacks, so the HAL retains validation, exclusivity, partial-start failure
policy, static driver storage and joined cleanup. Apps/services cannot use that
construction namespace under the architecture rules.

Driver-owned binding files are imported through normal Cargo dependencies.
Shared build support loads this catalogue for IMU profile composition and emits
its checked literal values and instance declarations. The catalogue declares
optional NuttX sensor dependencies and selects the concrete bus adapters.

Physical diagnostics live in `imu_devices::probe`; enable its `probe` feature.
It forwards the HAL's tracing feature and selects the device and optional
sensor-reference setup. Ordinary acquisition does not enable those controls.
The existing factory/lifetime witnesses preserve rollback and cleanup behavior;
host source injection is not physical qualification.

The additive `bmi270` and `lsm6dso` Cargo features include concrete drivers; neither
is enabled by default. `cargo nxrs build` derives the required features from all
configured IMUs when this catalogue is consumed. They are internal dependency
switches, not firmware build options. Use the frontend for NuttX qualification
builds as well. A mixed profile enables both. Cargo feature
unification can include additional drivers requested by another dependency;
selection never disables explicitly requested features.
