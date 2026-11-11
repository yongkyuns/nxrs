# Getting started

This walkthrough takes one Rust application from source code on your computer to a NuttX image running in QEMU. You create the application with Cargo, edit its source, and build it locally. There is no precompiled application to download or copy.

## Install the host tools

Install Git and Rust through [rustup](https://rustup.rs/), together with your operating system's native build tools: a C compiler and linker on Linux, the Xcode command-line tools on macOS, or Visual Studio C++ build tools with the Rust MSVC toolchain on Windows.

The host application works on these platforms. The next chapter's pinned ESP32-S3 compiler and QEMU bundle requires **Linux x86-64**; use an Ubuntu 24.04 x86-64 environment for the complete target walkthrough. It does not require a physical board. Python 3.11 or newer is used by the documentation tests and firmware tooling.

## Get the NXRS source

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
rustup show
```

The checkout's `rust-toolchain.toml` selects the Rust toolchain. Keep this terminal in the NXRS repository root throughout the walkthrough. The application will live beside the checkout, not inside the NXRS workspace:

```text
work/
  nxrs/          # SDK source, profiles and build tools
  my-firmware/   # your Cargo package and Rust source
```

Choose an unused application directory. `cargo new` refuses to overwrite an existing one.

## Create and run your application

Continue with [Your first application](first-application.md). It shows the complete source and manifest, generates a local lockfile, and runs the program on your host. [Building for NuttX](firmware.md) then builds and boots **that same source**, changes its greeting, and rebuilds it.

The sequence is:

1. Create `my-firmware` with `cargo new` and run `hello` on the host.
2. Install the target tools and build `hello` with an NXRS profile.
3. Boot the image in QEMU and run `hello` at the NuttX shell.
4. Edit the source, rebuild, and check the changed output.
5. Add the bounded worker from [Services and concurrency](services.md).

## Further examples

After the walkthrough, `std-demo` provides separate examples of collections, channels, synchronization, and deadlines:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

These are optional exercises, not a replacement for your application. See [Standard-library APIs](standard-library.md), [Examples](examples.md), and [Troubleshooting](troubleshooting.md) for more detail.
