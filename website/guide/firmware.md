# Building for NuttX

Continue with the `hello` source project from [Your first application](first-application.md). This chapter builds that same Cargo package for **ESP32-S3 under NuttX**, boots it in QEMU, and checks an edit/rebuild cycle. No board or precompiled firmware download is needed.

The supported `cargo nxrs build` command derives driver features from the platform profile; you do not need to select chip features manually.

## Prepare Ubuntu

Use **Ubuntu 24.04 on Linux x86-64** for this walkthrough. The repository's pinned compiler/emulator installer checks the host architecture. Host-only development can still use macOS or Windows; this target setup is a separate Linux environment.

Run the commands below from the NXRS repository root in Bash. Install the native build tools and libraries:

<!-- tutorial:packages -->
```sh
sudo apt-get update
sudo apt-get install -y build-essential git curl xz-utils unzip patch cmake ninja-build pkg-config python3 python3-venv bison flex gperf kconfig-frontends genromfs libncurses-dev zlib1g-dev libgcrypt20 libglib2.0-0 libpixman-1-0 libsdl2-2.0-0 libslirp0
```

Rust must already be installed through rustup, as described in [Getting started](getting-started.md). Initialize the pinned native sources and a Python environment:

<!-- tutorial:prepare -->
```sh
git submodule update --init
python3 -m venv target/tutorial-venv
source target/tutorial-venv/bin/activate
python3 -m pip install kconfiglib==14.1.0 esptool==4.8.1
bash tools/install-qemu-tools.sh
```

The installer downloads checksum-verified **development tools**: the Xtensa compiler, Rust toolchain/source bundle, and QEMU. Your application and NuttX firmware are compiled from source in the next step. The tools stay under `target/qemu-tools`; they do not replace your global Rust installation.

The build frontend discovers this local tool bundle. Do not source its `environment.sh` in your main development shell: it also selects an Xtensa-capable compiler. The QEMU command below uses a subshell so those settings cannot leak into later host builds.

## Build your source

<!-- tutorial:build -->
```sh
cargo nxrs build --manifest-path ../my-firmware/Cargo.toml --profile esp32s3-qemu-mock
```

This compiles the `hello` binary, Rust standard-library integration, and NuttX. The profile supplies the board and operating-system configuration; it does not add hardware dependencies to this standard-library-only application.

The output directory is:

```text
target/build/hello/esp32s3-qemu-mock/
```

Its `nuttx/nuttx` file is the target ELF. `nuttx/nuttx.merged.bin` is the flash image QEMU will boot. These are local build outputs, not files to copy into your application directory.

The directory also records the resolved profile, effective NuttX configuration, selected sources, and image checks. [Understand the build system](build-system.md#read-the-build-output) explains those records and how to distinguish configuration, Cargo compilation, native linking, and artifact validation.

## Boot QEMU

<!-- tutorial:qemu -->
```sh
(
  source target/qemu-tools/environment.sh
  qemu-system-xtensa -nographic -machine esp32s3 -nic none -no-reboot -snapshot \
    -drive file=target/build/hello/esp32s3-qemu-mock/nuttx/nuttx.merged.bin,if=mtd,format=raw
)
```

Wait for the `nsh>` prompt. Enter the following commands at that prompt, not in the host terminal:

```text
nsh> uname
NuttX
nsh> hello
Hello from NXRS
nsh> echo $?
0
```

`hello` is the command registered by your manifest. Run it again to check that the program returns to the shell. Exit QEMU with **Ctrl+A, then X**; this returns to your host Bash shell. `-snapshot` keeps emulator writes out of the built flash image. Networking is disabled for this example.

## Edit, rebuild, and run again

Change `../my-firmware/src/main.rs` to:

<!-- tutorial:modified-source -->
```rust
fn main() {
    println!("Hello again from NXRS");
}
```

Run the host command again:

```sh
cargo run --locked --manifest-path ../my-firmware/Cargo.toml --bin hello
```

Then repeat the **build** and **QEMU** commands above. At `nsh>`, `hello` must now print:

```text
Hello again from NXRS
```

Exit the previous emulator before rebuilding and boot a new process afterward. Rebuilding a file does not replace the firmware already executing in QEMU. Normal builds reuse compatible compilation output; this source edit should not need `--clean`.

## What this walkthrough establishes

You have compiled application source for a target, linked it with the selected Rust standard library and NuttX, and executed it through the NuttX shell. The edit/rebuild step confirms which source and image you are running.

This QEMU profile disables networking and uses qualification bindings. It does not exercise physical pins, radios, storage media, or board timing. When moving to a device, select its supported profile and follow its bootloader, flashing, and peripheral checks. [Profiles and overlays](profiles.md) explains that transition without changing the application entry model.

## Add ordinary Rust concurrency

[Services and concurrency](services.md#run-the-worker-in-your-application) supplies a bounded worker for the same source project. Replace `src/main.rs` with that code, then use the same host, target-build, and QEMU commands. The shell command remains `hello`; its output becomes `Processed total: 6` after the worker has been joined.

## Common setup problems

| Symptom | Check |
| --- | --- |
| `no such command: nxrs` | Run `cargo nxrs` from the NXRS checkout, where `.cargo/config.toml` defines the alias. |
| `Linux x86_64 required` | Use an x86-64 Linux environment for the pinned target bundle. |
| Missing Kconfig tools, compiler, or shared library | Complete both dependency-installation blocks and the tool installer above. Keep the Python environment active. |
| Manifest or lockfile error | Check `../my-firmware/Cargo.toml` and regenerate its lockfile after editing dependencies. |
| Host compilation unexpectedly uses the target compiler | Open a fresh Bash shell and activate only `target/tutorial-venv`. Use the QEMU environment in the shown subshell. |
| `hello: command not found` in NSH | Check the manifest's `command`, the completed build, and the exact image path used to boot QEMU. |
| Old greeting after rebuilding | Stop the old QEMU process, check the build exit status, and boot the newly built image. |
| QEMU starts without an NSH prompt | Keep the boot output and consult [Troubleshooting](troubleshooting.md) and the [QEMU reference](../../docs/nuttx-qemu.md). |

## Automated execution

The repository's `First firmware tutorial` workflow creates a fresh Cargo package from the guide's source and manifest blocks. It runs the host program, builds and boots it on NuttX/QEMU, then repeats after the greeting edit and worker extension. Each target run checks `uname`, exact application output, the shell exit status, and a second invocation in the same boot.

[The runner](../tests/first_firmware.py) records build logs, source and image hashes, and console transcripts. It tests source builds; it does not fetch a firmware artifact. [Testing and debugging](testing.md) covers broader test suites, and [Profiles and overlays](profiles.md) covers selecting other configurations.
