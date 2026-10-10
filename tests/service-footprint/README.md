# Matched C and Rust service-footprint demo

This local fixture demonstrates runtime entry, worker creation, queue ownership
and packet construction in a Rust/NuttX application. The
[footprint analysis](../../docs/rust-std-footprint.md) interprets the costs;
[recorded results](results/esp32s3-2026-10-03.json) are historical because the
cleaned checkout no longer has the measured source hashes.

The packet/reply workload sends 5,760 event/reply pairs through 60 POSIX queues
and 20 threads. `packet-service` adds matching packet
validation and persistent XYZ filtering in C and Rust; `wire-only` bypasses
processing, while retaining the larger reply layout as a transport control.
It is distinct from the later [lean event-service qualification](../service-qualification/README.md),
which sends 16-byte LED events through a no-reply forwarding pipeline. Do not
transfer measurements between those workloads. Host smoke tests use Crossbeam;
the target uses NuttX message queues, `poll` and native pthreads. Host results
check logic and evidence tooling, not target behavior or footprint.

## Before/after variants

The std application in [`app/nxrs/src/main.rs`](../../app/nxrs/src/main.rs)
uses ordinary Rust `main` and `std::thread`. In the fixture,
[`scale.rs`](src/scale.rs) can instead use `ffi-scale-entry` for an NSH-owned
C-compatible entry and `native-scale-worker` for its finite pthread
spawn-and-join adapter. These two features change entry and worker lifecycle
together; isolate entry with only `ffi-scale-entry`. The adapter does
not provide detach, cancellation, parking, TLS inheritance or recoverable
panic payloads, and the embedded policy aborts immediately on panic.

[`mq_backend.rs`](src/mq_backend.rs) keeps a typed facade over queue choices.
`borrowed-mq-io` reuses caller buffers; `shared-mq-code` shares byte-level
helpers. These are independent comparisons. `packet-inplace-samples` fills
the same 96-sample array directly instead of making a temporary copy. It
reduces producer stack high-water use without changing the reserved stack.
Keep C/Rust configuration and stack reservations matched when comparing any
variant. Feature definitions are in [`Cargo.toml`](Cargo.toml).

## Host checks

Requires Rust 1.90.0, Python 3.11 or later, and a C compiler:

```sh
cargo +1.90.0 test --locked -p nxrs-footprint-demo --bin cq-scale --features packet-service
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
bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/service-footprint/Cargo.toml --app-package nxrs-footprint-demo \
  --bin cq-scale --command cq_scale --priority 100 --stack-size 8192 \
  --platform esp32s3-service-footprint --out target/service-footprint/prepared \
  --size-optimized --trace-only-backtrace --panic-immediate-abort \
  --feature mq-backend --feature ffi-scale-entry --feature native-scale-worker \
  --feature shared-mq-code --feature synchronized-scale --feature wire-only \
  --target-c-source tests/service-footprint/esp32s3_cycles.c \
  --target-c-source tests/service-footprint/native_thread.c \
  --target-c-source tests/service-footprint/transport_gate.c
```

For a runtime-entry comparison, use `relink_rust.py` against that prepared tree
with fresh `--out` directories: build once with `wire-only`,
`synchronized-scale` and `shared-mq-code`, then repeat adding
`ffi-scale-entry` and `native-scale-worker`. To isolate entry, add only
`ffi-scale-entry`; compare the native worker by adding both features. For the
matched packet/wire and direct-fill cases, use:

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
