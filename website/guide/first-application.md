# Your first application

An NXRS application is a normal Rust package with a `main()` function. Create your own source project, run it on the host, and then build the same project for NuttX. NXRS supplies target integration and configuration; it does not supply a precompiled application.

The complete Rust source for this first program is:

<!-- tutorial:source -->
```rust
fn main() {
    println!("Hello from NXRS");
}
```

## Create a source project

Run this from the NXRS repository root, using an unused sibling directory:

<!-- tutorial:create -->
```sh
cargo new --bin --edition 2024 --name nxrs-basic-firmware --vcs none ../my-firmware
```

The command creates `Cargo.toml` and `src/main.rs`. Put the Rust source shown above in `../my-firmware/src/main.rs`.

## Add firmware metadata

Replace `../my-firmware/Cargo.toml` with the following complete manifest:

<!-- tutorial:manifest -->
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

`[package]`, `[workspace]`, and `[[bin]]` are Cargo settings. The package owns its workspace and lockfile. `hello` is the binary name; no NXRS runtime dependency is needed for this program.

The firmware metadata selects the binary and NuttX shell command, with the entry task's priority and stack allocation. The tutorial requests a 64 KiB entry stack. This is an example allocation, not a minimum or a recommendation for every application. Host Cargo execution does not use these NuttX settings.

## Build and run on the host

Generate the lockfile from the manifest you just wrote, then build and run:

<!-- tutorial:lock -->
```sh
cargo generate-lockfile --offline --manifest-path ../my-firmware/Cargo.toml
```

<!-- tutorial:host -->
```sh
cargo run --locked --manifest-path ../my-firmware/Cargo.toml --bin hello
```

Expected application output:

```text
Hello from NXRS
```

Cargo compiles your source into a host executable before running it. There is no executable-copy step. No platform profile or MCU toolchain is needed yet.

Keep running these commands from the NXRS checkout so its pinned Rust toolchain is selected. Commit your application's source, manifest, and lockfile to its own repository when ready.

## Build the same project for NuttX

[Building for NuttX](firmware.md) continues with `../my-firmware/Cargo.toml`, including complete Ubuntu setup, the build command, QEMU startup, expected shell output, and an edit/rebuild cycle.

The command will be:

```sh
cargo nxrs build --manifest-path ../my-firmware/Cargo.toml --profile esp32s3-qemu-mock
```

Run it after installing the tools in that chapter. `cargo nxrs` is an alias provided by the NXRS checkout. Its `--manifest-path` selects your external source package; `--profile` selects the target configuration. The build compiles the application, its Rust standard library, and NuttX into one target image.

The [build reference](../../docs/build-system.md#external-applications-and-package-boundaries) covers external workspaces and SDK-root selection in more detail.

## Add a worker

Once `hello` runs in QEMU, follow [Services and concurrency](services.md#run-the-worker-in-your-application). It provides the complete worker source to put in the same `src/main.rs`. Keep the manifest and target profile unchanged, then rebuild on the host and for NuttX.

The checked-in [basic-firmware sources](../examples/basic-firmware/) are a reference for the documentation tests. You do not need to copy that directory or any of its build output to follow the tutorial.
