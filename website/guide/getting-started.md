# Getting started

Start with a small Rust program on your development computer. Then run the standard-library examples supplied with NXRS. Neither step needs a board, a peripheral, or a NuttX cross-compiler.

## Install the host tools

Install Git and Rust through [rustup](https://rustup.rs/), together with the native build tools for your operating system. On Linux, install a C compiler and linker; on macOS, install the Xcode command-line tools; on Windows, use the Rust MSVC toolchain with the Visual Studio C++ build tools.

Python 3 is used by repository checks and firmware tooling. Target-specific compilers are covered in [Building for NuttX](firmware.md).

## Get the source

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
rustup show
```

The repository's `rust-toolchain.toml` selects the Rust version. Run the following commands from the repository root. The Cargo commands work in both a POSIX shell and PowerShell.

## Run a minimal application

```sh
cargo run --locked --manifest-path website/examples/basic-firmware/Cargo.toml --bin hello
```

The program prints:

```text
Hello from NXRS
```

This is an ordinary Rust executable with its own manifest and lockfile. It uses no NXRS runtime API. [Your first application](first-application.md) explains how to copy it into a project and add firmware metadata.

## Try standard-library facilities

The existing `std-demo` application contains short examples of collections, ownership, channels, synchronization, and deadlines:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

The channels example checks a bounded queue, transfers a buffer to a worker, receives the result, and joins the thread. A completed run ends with:

```text
STD_DEMO case=channels status=pass
STD_DEMO PASS cases=1
```

Other cases include `vec`, `fixed`, `maps`, `queues`, `bytes`, `ownership`, `synchronization`, and `deadlines`. Run all cases by omitting the arguments after `--`:

```sh
cargo run --locked -p nxrs-std-demo
cargo test --locked -p nxrs-std-demo
```

These examples use `std` directly and do not need `NXRS_PLATFORM`. Profiles become relevant when selecting hardware implementations or building a NuttX image.

## Next steps

Continue with [Your first application](first-application.md), then [Building for NuttX](firmware.md). [Services and concurrency](services.md) introduces a bounded event loop, while [Standard-library APIs](standard-library.md) covers the facilities available to application code.

For errors during setup, see [Troubleshooting](troubleshooting.md). Application-specific examples are collected separately under [Examples](examples.md).
