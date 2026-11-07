# USERLED on ESP32-S3 QEMU

This isolated qualification executes the unified `nxrs-led` crate's private
NuttX implementation (`hal/led/src/platform/nuttx/mod.rs`), its C helper under
`hal/led/src/platform/nuttx/ffi/`, the actual NuttX `open/ioctl/close` path, and upstream
`drivers/leds/userled_upper.c` under the pinned Xtensa ESP32-S3 emulator.
Only the hardware lower half is an instrumented test implementation. It records
output state and callback counts; this is not physical GPIO/LED qualification.

The target binary uses the portable `led::open()` / `Led::set()` API. The
builder selects `NXRS_PLATFORM=esp32s3-userled`; no provider Cargo feature is
passed. No host FFI stubs or mock Rust provider are linked. The test path is
bound by `[led].device` in that profile and emitted as `NXRS_USERLED_PATH`
by the package build script.
A separate deliberately failing VFS node verifies the real native ioctl error
path and failed-acquisition close before registering the real USERLED driver.

Each positive fresh kernel checks missing-node open failure, native EIO,
failed-query cleanup, supported-mask holes/high bit, 128 observed on/off controls,
64 rejected indices, and 32 open/drop cycles with descriptor recovery. The
oracle requires clean return to NSH and status 0. A fourth fresh kernel performs
the same checks, rejects an intentionally incorrect output witness, and exits
nonzero. Transcript controls reject wrong counts, duplicate/missing evidence,
panics, and incorrect shell status.

```sh
bash tools/install-qemu-tools.sh
NUTTX_STD_COMPAT_FIXES=1 bash tests/nuttx-std/userled/build.sh
source target/qemu-tools/environment.sh
python3 tests/nuttx-std/userled/run.py
```

The ordinary NuttX build frontend builds the LED binary directly with the
`esp32s3-userled` profile and regenerates final ABI, symbol, and hash evidence.
The LED Cargo package compiles its own target-header helper into its native
archive; the only explicitly staged C source is the instrumented lower-half
fixture. The partial Rust ELF must define `userled_supported` and `userled_set`,
while the fixture and libc imports remain unresolved until the final NuttX link.
Outputs use `target/nuttx-esp32s3-std`; do not run the legacy std-probe builder
concurrently in that directory.

The GitHub workflow retains final firmware, config, toolchain provenance,
compiler-artifact evidence for the unified `nxrs-led` crate root target `led`,
and the NuttX build-script cfg `hal_provider="hardware"` in
`userled-hal-selection.json`, plus native ABI results, updated
`userled-sources.sha256`/image hashes and console transcripts. Successful
source compilation alone does not pass this gate.
Existing SDK patches and the documented unqualified general poll/signal APIs
remain unchanged. No production platform is enabled. Binary-size deltas,
allocation bounds, timing bounds, physical pins and optical output remain
separate qualification tasks.
