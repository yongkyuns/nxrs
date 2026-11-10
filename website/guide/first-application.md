# Your first application

Start with `app/event-demo`. Its entry point is an ordinary Rust `main()`. The app connects services and selects a logical device name; the profile supplies the board-specific implementation.

## Read the composition

This excerpt shows the startup sequence from the [application source](../../app/event-demo/src/main.rs):

```rust
let (fusion, inputs) = FusionService::new();
let imu = ImuService::new(inputs.imu);
let gnss = GnssService::new(inputs.gnss);

let fusion = fusion.start()?;
let imu = imu.start_on(devices::primary_imu())?;
let gnss = gnss.start()?;
```

The fusion constructor supplies typed input endpoints. Sensor services receive those endpoints before execution starts. The application selects a logical IMU through the portable device catalogue; the profile supplies its concrete construction. Starting the services establishes their execution and acquisition lifetimes. The full app then sends control commands, observes output and checks the completed run.

Cargo dependencies describe the components, and ordinary Rust code describes how the application connects and runs them.

## Follow the lifecycle

Read the rest of `run()` in order. It constructs the graph, starts the owners, pauses and resumes GNSS processing, receives fusion output, stops sensor producers, drains already delivered output and explicitly stops fusion.

The ordering matters. A component waiting for a producer to close cannot finish if another owner still holds a sending endpoint. Dropping an owner handle may request cancellation; explicit stop joins cleanup. Include the error and teardown paths when adapting the example.

## Make a small first change

First change only the duration, without editing code:

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 5000
```

Then inspect `app/event-demo/src/orchestration.rs` and the service tests before changing control policy. A behavior change belongs in orchestration or a service; physical device selection belongs in a profile, and bus reads and acquisition stay in the driver and HAL code.

## Create a separate in-repository app

Copy the small `app/event-demo` directory to a sibling such as `app/my-firmware`. Give the package and binary unique names, add the new path to the root workspace members, and keep its relative navigation-service and device-catalogue dependencies valid. Preserve the original demo as a regression example.

The original manifest's firmware metadata is:

```toml
[package.metadata.nxrs]
required-capabilities = ["imu", "gnss"]

[package.metadata.nxrs.firmware]
bin = "event-demo"
command = "event_demo"
priority = 100
stack-size = 65536
```

Change `bin` together with `[[bin]].name`, and choose a distinct NuttX command name. Set the priority and stack size for your workload and target. Capability metadata provides admission checks; Cargo dependencies select the components.

After editing the manifest and workspace, use `cargo nxrs apps` to confirm discovery. Run your new package with the host profile before attempting a target build. Regenerate and review the lockfile when dependencies change, then use `--locked` for validation.

## Keep application code portable

Keep board `cfg` branches, `/dev` paths, toolchain logic and native imports in platform integration code rather than reusable applications or services. Use ordinary Rust APIs directly where they express the operation.

External applications can use their own workspace and handwritten main. Follow [the external-application section of the build reference](../../docs/build-system.md#external-applications-and-package-boundaries) for package dependencies and build preparation.
