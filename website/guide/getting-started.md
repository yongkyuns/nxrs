# Getting started

Set up the tools to build a small Rust program on your computer. In the next guide you’ll write the program, and in the firmware tutorial you’ll run it under NuttX in QEMU. You’ll need basic familiarity with Rust, Cargo, and a terminal.

## Choose your starting point

| What you want to do | Start here | What you need |
| --- | --- | --- |
| Try ordinary Rust firmware code on your computer | [Your first application](first-application.md) | Git, Rust, and native build tools |
| Build and boot that application without a board | [Building for NuttX](firmware.md) | The host program plus an Ubuntu x86-64 target-build environment |
| Add threads and typed commands | [Services and concurrency](services.md) | The same application; no physical peripherals |
| Select a board or change its wiring | [Profiles and overlays](profiles.md) | A working target toolchain and supported BSP/resources |
| Understand an existing application | [Examples](examples.md) | The prerequisites listed for that example |

## Install the host tools

Install [Git](https://git-scm.com/install/) and [Rust through rustup](https://rustup.rs/). Install the native build tools for your host:

- **Linux:** a C compiler and linker, such as Ubuntu’s `build-essential` package.
- **macOS:** Xcode Command Line Tools (`xcode-select --install`).
- **Windows:** Visual Studio C++ Build Tools with the Rust MSVC toolchain.

The host steps work on all three operating systems. For the full firmware walkthrough, use Ubuntu 24.04 on x86-64. The pinned ESP32-S3 compiler and emulator run in that environment. The firmware and documentation tools also use Python 3.11 or newer.

## Get the NXRS source

```sh
git clone https://github.com/yongkyuns/nxrs.git
cd nxrs
rustup show
```

`rustup show` should list the toolchain selected by the checkout’s `rust-toolchain.toml`. Keep the terminal in the NXRS root. The next guide creates your program in an unused sibling directory, `../my-firmware`. The host steps can run before you initialize the NuttX submodules or install the target tools.

## Create and run your application

Follow [Your first application](first-application.md) to write a greeting and run it on your computer. Then [build it for NuttX](firmware.md), boot it in QEMU, and run `hello` at the `nsh>` prompt. Until you see that prompt, enter the commands in your computer’s terminal.

## Where to look when a step fails

Keep the first failing command and its output. [Troubleshooting](troubleshooting.md) walks through setup, build, and boot errors in order.

## Further examples

After the walkthrough, `std-demo` provides separate examples of collections, channels, synchronization, and deadlines:

```sh
cargo run --locked -p nxrs-std-demo -- --list
cargo run --locked -p nxrs-std-demo -- --case channels
```

See [Standard-library APIs](standard-library.md) for more recipes.
