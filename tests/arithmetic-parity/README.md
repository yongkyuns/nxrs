# C/Rust arithmetic qualification

This local ESP32-S3 diagnostic compares defined arithmetic results, generated
code and timing for paired C and Rust kernels. It is qualification evidence for
the listed cases and build, not a production dependency or a claim of universal
language parity. Dependency changes are private compiler or std candidates;
they do not modify an installed SDK or the application.

The catalog has **1,136 cases and 72,704 vectors**: 942 have matched C/Rust
implementations and 194 are Rust-only because Xtensa GCC has no native 128-bit
integer type. Independent Python expectations cover integer, conversion and
floating operations. C avoids undefined overflow, invalid shifts and invalid
float-to-integer casts. Checked errors are values, not traps. Basic arithmetic
is exact; square root, libm and bounded `powi` use 1, 2 and 4 ULP limits.
NaN payloads may differ, and permitted signed-zero differences are retained.
See the [measured assessment](RESULTS.md) for the scope and limits of each
device cohort.

## Host checks

Python dependencies are test-host-only. From the repository root:

```sh
python3 -m venv target/arithmetic-python
target/arithmetic-python/bin/pip install -r tests/arithmetic-parity/requirements-test.txt
target/arithmetic-python/bin/python -m unittest discover -s tests/arithmetic-parity
target/arithmetic-python/bin/python tests/arithmetic-parity/generate.py --out target/arithmetic-corpus
target/arithmetic-python/bin/python tests/arithmetic-parity/host_check.py \
  --generated target/arithmetic-corpus --exhaustive-8
```

Use `--integers-only` to omit floating dependencies or repeated `--case`
filters to generate a shard. The exhaustive 8-bit checks run on the host, not
the ESP32-S3. The full vector corpus uses about 5 MiB of flash, so a board with
less space requires a shard.

## Reproducing a device report

Build an inactive compiler selection using the pinned private inputs and
independent NuttX std snapshot in [the compiler build guide](../../upstream/rust-llvm/BUILDING.md).
The current compiler evaluation is frozen at `mul-range`; it produced no new
full-corpus device run. The default application toolchain remains unchanged.
Create fresh output directories for corpus, package, linked image and capture.
Build the Rust bundle with its compiler ledger and provenance, then link it
into a prepared private NuttX tree with the existing `nxrs_std_app` helper:

```sh
python3 tests/arithmetic-parity/build.py rust \
  --generated "$AQ_GENERATED" --sysroot "$AQ_SYSROOT" \
  --cargo "$AQ_CARGO" --target "$AQ_TARGET_JSON" --ld "$AQ_XTENSA_LD" \
  --ledger "$AQ_PATCH_LEDGER" --compiler-provenance "$AQ_COMPILER_PROVENANCE" \
  --cargo-target "$AQ_CARGO_CACHE" --out "$AQ_RUST_BUNDLE"
python3 tests/arithmetic-parity/build.py link \
  --generated "$AQ_GENERATED" --bundle "$AQ_RUST_BUNDLE" \
  --tree "$AQ_PRIVATE_NUTTX_TREE" --prefix "$AQ_GCC_PREFIX" --out "$AQ_LINKED"
```

For a device run, use a board intended for flashing, sole serial-port access,
and a fresh protected **full 16 MiB flash backup**. The collector restores and
verifies that image in `finally`, including failure paths; keep backups,
device identifiers and raw captures private.

```sh
python3 tests/arithmetic-parity/measure.py \
  --image "$AQ_LINKED/image.bin" --coverage "$AQ_GENERATED/coverage.json" \
  --backup "$AQ_FULL_PRIVATE_BACKUP" --port "$AQ_SERIAL_PORT" \
  --flasher "$AQ_ESPTOOL" --out "$AQ_PRIVATE_CAPTURE" --runs 3
python3 tests/arithmetic-parity/report.py \
  --generated "$AQ_GENERATED" --linked "$AQ_LINKED" \
  --captured "$AQ_PRIVATE_CAPTURE" --out "$AQ_PUBLIC_REPORT"
```

Reports retain every run and verify source, toolchain, image and restoration
identities. They include input loads, result stores, calls and batch overhead;
normal samples include interrupts and preemption. Function sizes exclude shared
helpers and aliases. Neither batch timing nor the combined diagnostic image is
a production firmware cost estimate.
