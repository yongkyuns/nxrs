# USERLED on ESP32-S3 QEMU

This isolated qualification executes the unified `nxrs-led` crate's private
NuttX implementation (`hal/led/src/platform/nuttx/mod.rs`), its C helper under
`hal/led/src/platform/nuttx/ffi/`, the actual NuttX `open/ioctl/close` path, and upstream
`drivers/leds/userled_upper.c` under the pinned Xtensa ESP32-S3 emulator.
Only the hardware lower half is an instrumented test implementation. It records
output state and callback counts; this is not physical GPIO/LED qualification.

The target binary uses the portable `led::open()` / `Led::set()` API. The
builder selects `NXRS_PLATFORM=nuttx-userled`; no provider Cargo feature is
passed. No host FFI stubs or mock Rust provider are linked. The test path is
bound using the provider's existing compile-time `NXRS_USERLED_PATH`, not a new
public API.
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

The builder reuses the existing ESP32-S3 std bootstrap rather than creating a
second SDK/ABI setup. It then cleans the intermediate image, adds the target
fixture/helper, enables USERLED, builds the actual LED binary, and regenerates
final ABI/symbol/hash evidence. Only the final rebuilt image is run. The initial
thread-probe build in the build log is not evidence that the LED test passed.
Outputs use the existing `target/nuttx-esp32s3-std` working directory; do not run
both builders concurrently in one checkout.

The GitHub workflow retains final firmware, config, toolchain provenance,
compiler-artifact evidence for the unified `nxrs-led` crate root target `led`,
and the executed build-script cfg `nxrs_hal_platform="nuttx"` in
`userled-hal-selection.json`, plus native ABI results, updated
`userled-sources.sha256`/image hashes and console transcripts. Successful
source compilation alone does not pass this gate.
Existing SDK patches and the documented unqualified general poll/signal APIs
remain unchanged. No production platform is enabled. Binary-size deltas,
allocation bounds, timing bounds, physical pins and optical output remain
separate qualification tasks.
