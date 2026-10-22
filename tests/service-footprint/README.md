# Matched C and Rust service-footprint demo

This local fixture demonstrates runtime entry, worker creation, queue ownership
and packet construction in a Rust/NuttX application. The
[footprint analysis](../../docs/rust-std-footprint.md) interprets the costs;
[recorded results](results/esp32s3-2026-10-03.json) are historical because the
cleaned checkout no longer has the measured source hashes.

The packet/reply workload sends 5,760 event/reply pairs through 60 queues and
20 threads. Host tests use Crossbeam; NuttX uses its message queues, common
start/completion gate and finite native pthread adapter. This is a qualification
fixture, separate from the production Rust execution model.

## Workload cases and probes

[`experiment.rs`](src/experiment.rs) defines closed, typed workload cases:
owned or borrowed wire I/O, packet-sized wire controls, packet processing with
copied samples, and packet processing with samples filled in place. The matrix
runner selects one `--case`; the paired runner selects its matching paired case.
[`relink_rust.py`](relink_rust.py) records that choice and tracks it through
Cargo's build script. Unknown cases fail the build. No Cargo features select
queues, worker types, packet layouts or scheduling.

`probe` is the only Cargo feature. It enables phase clocks and counters; disabled
counters and stamps occupy no storage. Primary footprint measurements exclude
probes. The native worker is separately tested by
[`check_native.py`](check_native.py).

Historical BSS padding, flash attribution ladders, legacy payload copying,
checksum substitution and alternate topology toggles have been retired. Their
recorded evidence and original source revisions remain in Git; these new sources
require fresh device measurements before making size or timing claims.

## Host checks

Requires Rust 1.90.0, Python 3.11 or later, and a C compiler:

```sh
NXRS_FOOTPRINT_CASE=rust-packet-inplace-2 cargo +1.90.0 test --locked -p nxrs-footprint-demo --bin cq-scale
python3 -m unittest discover -s tests/service-footprint -p 'test_*.py' -v
python3 tests/service-footprint/check_native.py
```

## Prepare and build for ESP32-S3

Use the pinned ESP Rust/GCC environment in [`NuttX std setup`](../../docs/nuttx-std.md).
Prepare one disposable SDK tree and source its environment once. The installer
is for the reproducible Linux tool setup; native macOS tools require
`NXRS_ESP32S3_NATIVE_TOOLS=1` and an explicit matching `NXRS_QEMU_TOOLS_DIR`.
The application build is local and does not flash:

```sh
bash tools/install-qemu-tools.sh
source target/qemu-tools/environment.sh
NXRS_FOOTPRINT_CASE=rust-borrowed-2 bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/service-footprint/Cargo.toml --app-package nxrs-footprint-demo \
  --bin cq-scale --command cq_scale --priority 100 --stack-size 8192 \
  --platform esp32s3-service-footprint --out target/service-footprint/prepared \
  --size-optimized --trace-only-backtrace --panic-immediate-abort \
  --target-c-source tests/service-footprint/esp32s3_cycles.c \
  --target-c-source tests/service-footprint/native_thread.c \
  --target-c-source tests/service-footprint/transport_gate.c
```

Use the named primary and paired experiments with fresh output directories:

```sh
python3 tests/service-footprint/run_transport_matrix.py --build \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --baseline-config target/service-footprint/prepared/resolved.config \
  --out-root target/service-footprint/run1 \
  --case c-packet-wire-2 --case rust-packet-wire-2 \
  --case c-packet-2 --case rust-packet-2
python3 tests/service-footprint/run_transport_paired.py \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --out-root target/service-footprint/paired-run1 \
  --case wire --case packet --case packet-inplace
```

Use fresh, serial output directories and matched C/Rust kernel inputs.
The historical GCC/compiler identity is in the result record, and a different
compiler needs new evidence. `panic-immediate-abort` is an explicit experiment
policy, not the workspace default. The PSRAM-enabled board configuration does
not stand in for a 250 kB product. Before explicitly adding `--measure`, make a
private full-device backup, verify its hash and arrange to restore it; pass the
backup credentials only through local arguments. Never publish the backup or
raw transcripts. See each script's `--help` for case options and measurement
controls. Nominal allocation ledgers are not measured peaks or fragmentation
guarantees.
