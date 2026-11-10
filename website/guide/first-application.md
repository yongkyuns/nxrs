# Your first application

An NXRS application starts with a normal Rust package and `main()` function. Application code selects its components; a platform profile supplies the target configuration.

The [basic-firmware example](../examples/basic-firmware/) is a small, self-contained starting point. Its entry point is:

```rust
fn main() {
    println!("Hello from NXRS");
}
```

## Copy the example

From the NXRS repository root, copy `website/examples/basic-firmware` into a sibling directory named `my-firmware`.

POSIX shell:

```sh
cp -R website/examples/basic-firmware ../my-firmware
```

PowerShell:

```powershell
Copy-Item -Recurse website/examples/basic-firmware ../my-firmware
```

Run the copied application:

```sh
cargo run --locked --manifest-path ../my-firmware/Cargo.toml --bin hello
```

The copy has its own Cargo workspace and lockfile. There is no need to add it to the NXRS workspace or copy any board code.

## Understand the manifest

```toml
[package]
name = "nxrs-basic-firmware"
version = "0.1.0"
edition = "2024"

[workspace]

[[bin]]
name = "hello"
path = "src/main.rs"

[package.metadata.nxrs.firmware]
bin = "hello"
command = "hello"
priority = 100
stack-size = 65536
```

The package and binary are ordinary Cargo definitions. The empty `[workspace]` table makes this directory an independent workspace even while the example lives inside the NXRS checkout.

The firmware metadata tells the NXRS build frontend which binary to build, the command to register with NuttX, and the entry task's priority and stack allocation. The values above match the example configuration; adjust the stack and priority for your target and workload. Host Cargo execution does not use those NuttX settings.

## Build against an NXRS profile

Keep working from the NXRS repository root when using its `cargo nxrs` alias:

```sh
cargo nxrs build --manifest-path ../my-firmware/Cargo.toml --profile esp32s3-qemu-mock --dry-run
```

This prints the build plan. After preparing the target tools described in [Building for NuttX](firmware.md), remove `--dry-run` to build the image. The SDK root supplies NuttX, the toolchain integration, and the selected profile; the application remains in its own workspace.

The [build reference](../../docs/build-system.md#external-applications-and-package-boundaries) describes external workspaces, path resolution, and direct Cargo use with `NXRS_SDK_ROOT`.

## Add functionality in ordinary Rust

Add modules and dependencies through Cargo. Use `std` directly for threads, synchronization, time, files, and sockets. Introduce a hardware interface when the application needs a peripheral or subsystem that `std` does not describe.

The copied example also contains a small `worker` example:

```sh
cargo run --locked --manifest-path ../my-firmware/Cargo.toml --example worker
```

It prints `Processed total: 6`. [Services and concurrency](services.md) walks through its typed events, bounded queue, and shutdown.

As the application grows, move reusable behavior into library crates and pass dependencies through constructors. Keep board pins, device paths, and implementation selection in platform configuration and hardware integration code. [Drivers and HALs](hardware.md) and [Profiles and overlays](profiles.md) explain those responsibilities.
