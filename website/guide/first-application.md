# Your first application

Use `app/event-demo` as the first example, rather than starting from an invented framework skeleton. Its entry point is an ordinary Rust `main()`. The app knows the services, their connections and a logical device name, not the board, native endpoints or sensor driver types.

## Read the composition

This is an excerpt from the [application source](../../app/event-demo/src/main.rs), not a complete standalone program:

```rust
let (fusion, inputs) = FusionService::new();
let imu = ImuService::new(inputs.imu);
let gnss = GnssService::new(inputs.gnss);

let fusion = fusion.start()?;
let imu = imu.start_on(devices::primary_imu())?;
let gnss = gnss.start()?;
```

The fusion constructor supplies typed input endpoints. Sensor services receive those endpoints before execution starts. The application selects a logical IMU through the portable device catalogue; the profile supplies its concrete construction. Starting the services establishes their execution and acquisition lifetimes. The full app then sends control commands, observes output and checks the completed run.

There is no required application trait, generated service graph or platform launcher. Cargo dependencies describe the components; Rust code describes how the application uses them.

## Follow the lifecycle

Read the rest of `run()` in order. It constructs the graph, starts the owners, pauses and resumes GNSS processing, receives fusion output, stops sensor producers, drains already delivered output and explicitly stops fusion.

The ordering matters. A component waiting for a producer to close cannot finish if another owner still holds a sending endpoint. Conversely, dropping an owner handle is not the same contract as explicitly requesting stop and joining cleanup.

Do not extract the startup excerpt above as a complete application without also designing the error and teardown paths.

## Make a small first change

First change only the duration, without editing code:

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo -- --duration-ms 5000
```

Then inspect `app/event-demo/src/orchestration.rs` and the service tests before changing control policy. Keep bus reads and sensor polling out of the application. A product behavior change belongs in orchestration or a service; physical device selection belongs in a profile.

## Create a separate in-repository app

For a new app, copy the small `app/event-demo` directory to a sibling such as `app/my-firmware`. Give the package and binary unique names, add the new path to the root workspace members, and keep its relative navigation-service and device-catalogue dependencies valid. Preserve the original demo as a regression example.

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

Change `bin` together with `[[bin]].name`, and choose a distinct NuttX command name. The example priority and stack size are **example configuration, not recommended production budgets**. Review them for your workload and target. Capability metadata provides admission checks; it does not replace Cargo dependencies.

After editing the manifest and workspace, use `cargo nxrs apps` to confirm discovery. Run your new package with the same explicit host profile before attempting a target build. A manifest change may require intentionally regenerating the workspace lockfile; review and commit that change rather than permanently removing `--locked` from validation.

## Keep portable code portable

Do not add board `cfg` branches, `/dev` paths, toolchain logic or native imports to reusable app/service source. Keep actual platform integration in the owning HAL and build configuration. Use ordinary Rust APIs directly where they already express the operation.

External applications can use their own workspace and handwritten main, but packaging and prepared-build boundaries remain experimental. Read [the external-application section of the build reference](../../docs/build-system.md#external-applications-and-package-boundaries) before treating the checkout as a published SDK.
