# Zed and clangd

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
