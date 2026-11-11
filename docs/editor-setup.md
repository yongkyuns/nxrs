# Zed platform setup

In Zed, run **task: spawn** and search for **Nxrs editor**. Choose **Host**
for the current desktop OS or choose the board you are editing for. The task
updates this checkout's local Zed settings and Zed reloads rust-analyzer.
The terminal equivalent for the host is `cargo nxrs editor host`. Run
**Nxrs editor: Show active profile** (or `cargo nxrs editor status`) to see the
exact profile name and, for NuttX, the target used by rust-analyzer.

NuttX needs a prepared build. Run your usual `cargo nxrs build` with
`--out target/editor/wroom`, `target/editor/cores3`, or `target/editor/pico2w`
to use the corresponding task. For any other output directory, use
`cargo nxrs editor --build-dir <path>`. It uses that build's resolved profile,
target, derived driver features, generated device configuration, compiler,
native headers and prepared resource archives, and checks the selected package. Zed invokes Cargo with these validated
inputs; no manual chip-feature selection is needed. Rebuild
if the resolved profile is stale or if an older output predates editor support
and used custom Cargo options. The application must be in the Nxrs workspace.
The task reads and writes standard JSON project settings. It only changes
rust-analyzer configuration.

To verify the selection, use Go to Definition on `selected` in a HAL's
`src/platform/mod.rs`. Zed can dim code that rust-analyzer reports as
inactive. Some branches inside `cfg_if!` still use ordinary syntax highlighting.

The WROOM camera profiles leave IMU unbound. For the NuttX IMU provider, choose
**WROOM IMU (DevKit qualification)** after preparing its editor build:

```sh
cargo nxrs build --manifest-path tests/nuttx-std/imu-qualification/Cargo.toml \
  --profile esp32s3-devkit-imu-qualification --out target/editor/wroom-imu
```

## C and Objective-C

Select an editor profile once to install the project clangd settings.
Run `python3 tools/setup-clangd.py` from this checkout, then run
`editor: restart language server` from Zed's command palette (or reopen the
project). Project settings enable clangd's configuration support.

The script generates an ignored `.clangd` and `target/clangd/compile_commands.json`.
On macOS it discovers the installed SDK through `xcrun` and configures the native
HAL bridges with their include paths and Objective-C ARC, matching the native
build. Real diagnostics remain enabled for these sources and portable C files.
Rerun after moving the checkout, updating Xcode, or adding a native source.

NuttX requires a configured build's generated headers, board configuration,
compiler flags, and target headers. Empty submodules or host SDK includes cannot
provide these. Diagnostics and background indexing are therefore disabled for
unconfigured NuttX, target-specific comparison, and other-host bridge sources. Syntax
highlighting and formatting remain available; semantic navigation requires the
target headers. This also hides genuine errors in those inactive files.

To enable diagnostics for target sources, capture a compilation database from
the actual configured build (for example with Bear), then run:

```sh
python3 tools/setup-clangd.py --compile-commands /absolute/path/to/compile_commands.json
```

The database must reference source files in this checkout. A database referring
to copies in a firmware build directory does not configure their originals;
capture commands for the originals or adapt the file entries while preserving
the configured build directory and include flags. Only explicitly configured
target files regain diagnostics. Cross compilers must be understood by the
installed clangd; compiler-specific flags may need adjustment. Linux native
bridges also need their real build database for pkg-config include flags.

This setup configures editing only; it does not qualify NuttX ABI or hardware.
See the [Zed C documentation](https://zed.dev/docs/languages/c) and
[clangd configuration reference](https://clangd.llvm.org/config).
