# Your first application

Create an independent Cargo package that prints a greeting. You’ll run it on your computer and add the metadata the NuttX builder needs.

Finish the [host setup](getting-started.md) and run the commands from the NXRS root. Use an unused `../my-firmware` directory.

The program’s complete source is below. You’ll put it in `src/main.rs` after creating the package.

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

The firmware builder reads the metadata below. The 64 KiB stack is the allocation for this tutorial’s entry task. Measure the stack your program needs separately.

| Manifest value | Meaning in this example |
| --- | --- |
| Package `nxrs-basic-firmware` | Cargo's package identity |
| Binary `hello` | The executable target selected by Cargo and the frontend |
| Firmware `command = "hello"` | The command you will type at the NuttX shell |
| Firmware `priority = 100` | The requested priority of the application entry task |
| Firmware `stack-size = 65536` | The entry task's requested stack allocation in bytes |

Keep firmware metadata with the application because it describes how that entry point runs. Board names, device addresses, and native pin settings belong in [profiles](profiles.md). Additional threads choose their own stack requirements; changing the entry stack does not set every worker's stack.

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

Cargo builds and runs the host executable with the checkout’s Rust toolchain. Keep the source, manifest, and lockfile together in your application’s repository.

## Understand the files you own

| File | What you change as the application grows |
| --- | --- |
| `src/main.rs` | Startup, component construction, error handling, and shutdown |
| Other Rust modules under `src/` | Application-specific behavior and tests |
| `Cargo.toml` | Dependencies, supported features, binary targets, and entry metadata |
| `Cargo.lock` | The resolved dependency versions used for reproducible builds |

The `[workspace]` table makes this package its own workspace. You do not need to add the application to the NXRS repository's workspace or copy target integration code into it. Use ordinary Cargo dependencies when you later add reusable NXRS components or other Rust libraries.

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

The [checked-in source](../examples/basic-firmware/) matches the code used in the tutorial tests.
