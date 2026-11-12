# Getting started

This walkthrough takes one Rust application from source code on your computer to a NuttX image running in QEMU. You create the application with Cargo, edit its source, and build it locally. There is no precompiled application to download or copy.

You need basic familiarity with Rust, Cargo, and a terminal. Start with the host steps even if your eventual target is a physical board: they establish that your source package and Rust toolchain work before adding a cross compiler and an operating-system build.

## Choose your starting point

| What you want to do | Start here | What you need |
| --- | --- | --- |
| Try ordinary Rust firmware code on your computer | [Your first application](first-application.md) | Git, Rust, and native build tools |
| Build and boot that application without a board | [Building for NuttX](firmware.md) | The host program plus an Ubuntu x86-64 target-build environment |
| Add threads and typed commands | [Services and concurrency](services.md) | The same application; no physical peripherals |
| Select a board or change its wiring | [Profiles and overlays](profiles.md) | A working target toolchain and supported BSP/resources |
| Understand an existing application | [Examples](examples.md) | The prerequisites listed for that example |

## Install the host tools

Install Git and Rust through [rustup](https://rustup.rs/), together with your operating system's native build tools: a C compiler and linker on Linux, the Xcode command-line tools on macOS, or Visual Studio C++ build tools with the Rust MSVC toolchain on Windows.

The host application works on these platforms. The next chapter's pinned ESP32-S3 compiler and QEMU bundle requires **Linux x86-64**; use an Ubuntu 24.04 x86-64 environment for the complete target walkthrough. It does not require a physical board. Python 3.11 or newer is used by the documentation tests and firmware tooling.

## Get the NXRS source

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
rustup show
```

The checkout's `rust-toolchain.toml` selects the Rust toolchain. `rustup show` should report that override rather than an unrelated global default. Keep this terminal in the NXRS repository root throughout the walkthrough. The application will live beside the checkout, with these relative paths:

| Path from the NXRS root | Purpose |
| --- | --- |
| `.` | NXRS source, platform profiles, and build tools |
| `../my-firmware/` | Your independent Cargo package and Rust source |
| `target/` | NXRS tooling and generated build outputs |

Choose an unused application directory. `cargo new` refuses to overwrite an existing one.

The host-only first application does not need NuttX submodules, a board SDK, or an emulator yet. [Building for NuttX](firmware.md#prepare-ubuntu) initializes and installs those inputs when they are needed.

## Create and run your application

Continue with [Your first application](first-application.md). It shows the complete source and manifest, generates a local lockfile, and runs the program on your host. [Building for NuttX](firmware.md) then builds and boots **that same source**, changes its greeting, and rebuilds it.

The sequence is:

1. Create `my-firmware` with `cargo new` and run `hello` on the host.
2. Install the target tools and build `hello` with an NXRS profile.
3. Boot the image in QEMU and run `hello` at the NuttX shell.
4. Edit the source, rebuild, and check the changed output.
5. Add the bounded worker from [Services and concurrency](services.md).

Every command block in the walkthrough runs on the host unless it explicitly shows an `nsh>` prompt. That prompt is the NuttX shell inside the emulator. The Rust binary name, its NSH command, and the product profile are explained as they first appear.

## Where to look when a step fails

Keep the first failing command and its output. A host compiler error belongs to the source package or host setup; a target-tool error belongs to the firmware setup; an NSH command failure happens after the image has booted. [Troubleshooting](troubleshooting.md) follows those boundaries.

You can read the [architecture overview](overview.md) at any point, but the first program needs no custom service, hardware abstraction, or build script. Add those pieces when your application needs their behavior.

## Further examples

After the walkthrough, `std-demo` provides separate examples of collections, channels, synchronization, and deadlines:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

These are optional exercises, not a replacement for your application. See [Standard-library APIs](standard-library.md), [Examples](examples.md), and [Troubleshooting](troubleshooting.md) for more detail.
