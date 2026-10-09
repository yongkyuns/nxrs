# Matched C and Rust service footprint demo

An isolated, local-only measurement fixture, not a production messaging API.
Read the [analysis](../../docs/rust-std-footprint.md) for RAM, image size,
speed, and shared versus recurring costs. [Historical results](results/esp32s3-2026-10-03.json)
are from the previously measured firmware, not new measurements of this
cleaned checkout. No device backup or private SDK is committed.

The [development walkthrough](DEVELOPMENT.md) explains the retained before/after
variants and how to build them. For the later no-reply service architecture and
other execution models, start with the independent [RTOS analysis](../../docs/rtos-comparison.md).

The large case exchanges 5,760 event/reply pairs through 60 POSIX queues and
20 spawned threads. `packet-service` adds identical packet validation and
persistent XYZ filtering in C and Rust. `wire-only` bypasses computation;
combining it with `packet-service` keeps the larger reply layout for a fair
transport control. The device path uses mqueues, poll, and native pthreads.
Host smoke tests use Crossbeam instead of NuttX mqueues.

## Run host checks

Requires Rust 1.90.0, Python 3.11 or later, and a host C compiler.

```sh
cargo +1.90.0 test --locked -p nxrs-footprint-demo --bin cq-scale --features packet-service
python3 -m unittest discover -s tests/service-footprint -p 'test_*.py' -v
python3 tests/service-footprint/check_native.py
```

The Python suite checks C and Rust against common independent packet golden
values and malformed inputs, verifies the release gate, and rejects changed
artifacts, mismatched protocols, incomplete runs, and bad heap accounting.

## Build on ESP32-S3

Use a disposable prepared build tree, initialized submodules, and the pinned
ESP Rust/GCC environment documented in [NuttX std setup](../../docs/nuttx-std.md).
The provided Linux tool installer is the reproducible starting point.
The historical GCC version is recorded in the results; a different compiler
requires a new measurement, not reuse of the old numbers. Native macOS tools
require `NXRS_ESP32S3_NATIVE_TOOLS=1` and an explicit `NXRS_QEMU_TOOLS_DIR`
containing the matching `environment.sh`.

From the repository root, create a fresh tree without flashing:

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

`panic-immediate-abort` omits recoverable panic handling and diagnostic panic
output. It is an explicit experimental policy, not the workspace default.
The platform preserves the study's kernel settings, including USERLED;
this packet workload does not exercise the LED. Its 8 MiB PSRAM configuration
is not a preset for a 250 kB product. NuttX board fixes remain build-applied
patches in `upstream/nuttx/patches`, not changes committed to upstream sources.

Build matched cases serially into fresh evidence directories:

```sh
python3 tests/service-footprint/run_transport_matrix.py --build \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --baseline-config target/service-footprint/prepared/resolved.config \
  --out-root target/service-footprint/run1 \
  --case c-packet-wire-2 --case rust-packet-wire-2 \
  --case c-packet-2 --case rust-packet-2
```

The scripts refuse reused case directories and concurrent tree mutation.
Only application selection may differ between C and Rust kernel configs.
Use `--help` for all cases and paired timing controls. Mixed C/Rust images
are timing controls, not separate-language size comparisons.

## Measure safely

Before any flash operation, save a full local device backup, verify its hash,
and arrange to restore it afterward. The build scripts do not flash.
Adding `--measure --port <your-device-port>` to the matrix command explicitly
enables flashing and serial measurement. Do not use a port copied from
another machine. The 240 MHz single-core clock is part of the measurement
contract; reject results from a different CPU configuration.

Use `threads` on a fresh boot as the live idle-thread baseline, then `large`
for the workload. NuttX `free` high-water counters do not reset between
commands. The driver flashes/resets before each measurement group and runs
the separate thread baseline afterward on a fresh boot. Subtract live heap,
not only stack reservations, and label the remaining increment as workload
memory rather than pure queue storage.

`transport_pipeline_report.py`, `paired_transport_report.py`, and
`transport_resource_report.py` validate new evidence; use their `--help`.
The full matrix report requires all ten cases. The layout probe compiles to
assembly only and neither links nor flashes firmware. Nominal allocation
ledgers are not measured peaks or fragmentation guarantees.
