# Developer walkthrough: service footprint

This fixture is an educational before/after study built around existing nxrs
Rust services and NuttX integrations. Read [the footprint analysis](../../docs/rust-std-footprint.md)
for interpretation and [the recorded ESP32-S3 results](results/esp32s3-2026-10-03.json)
for historical evidence. The historical firmware sources are not byte-identical
to a cleaned checkout: when cleanup changes source hashes, the recorded
measurements remain history and must not be attributed to the new build.

## Read the implementation

Start with [the std application](../../app/nxrs/src/main.rs) for ordinary
`std` startup, app-owned lifecycle, and a `std::thread` worker. The footprint
fixture keeps both sides of the runtime comparison in
[`scale.rs`](src/scale.rs): the default executable uses Rust `main` and
`thread::Builder`; `ffi-scale-entry` selects `no_main` on NuttX and exposes a
C-compatible entry called by NSH, while `native-scale-worker` uses the narrow
pthread adapter in [`native_thread.rs`](src/native_thread.rs). In the embedded
variant, NSH owns command/task startup and status return; Rust does not run its
usual process startup/cleanup path. The adapter supports this finite demo's
spawn-and-join path, with no detach, cancellation, parking, TLS inheritance,
or recoverable panic payload. Its immediate-abort target policy is part of
that contract.

[`mq_backend.rs`](src/mq_backend.rs) shows three queue choices behind the same
typed facade. `send`/`recv` transfer a `Copy` wire value by value;
`borrowed-mq-io` adds `send_ref` and receive-into-caller-storage to reuse
buffers; queue handles share one NuttX queue object through `Arc`; and
`shared-mq-code` outlines the byte-level send, receive, and open helpers so
typed uses reuse code. `borrowed-mq-io` changes buffer ownership at the call
boundary, while `shared-mq-code` changes code sharing. These are independent
choices, not rungs in an optimization ladder.

For packet construction, [`packet_service.rs`](src/packet_service.rs) uses a
temporary 96-sample array by default. `packet-inplace-samples` fills that same
array directly before encoding. It removes the temporary copy and lowers
producer stack high-water use; the reserved stack size stays fixed. The
measurement kernel and C/Rust processing configuration must otherwise match.
Keep pthread stack reservations equal and report them separately from the
language delta. Formatted report output is present for diagnostics and can
retain formatting support; logging is optional product behavior, not a
required part of the service.

The relevant feature declarations are in
[`Cargo.toml`](Cargo.toml). The Nxrs examples and footprint fixture are the
teaching material; this walkthrough does not introduce a second source tree
or a general messaging framework.

## Host checks

From the repository root, with Rust 1.90.0, Python 3.11+, and a C compiler:

```sh
cargo +1.90.0 test --locked -p nxrs-footprint-demo --bin cq-scale --features packet-service
python3 -m unittest discover -s tests/service-footprint -p 'test_*.py' -v
python3 tests/service-footprint/check_native.py
```

These checks exercise host packet logic and Python evidence/CLI validation.
The host build uses Crossbeam rather than NuttX queues or pthreads, so it does
not measure target footprint or establish target behavior.

## Target builds without flashing

Prepare a fresh ESP32-S3 NuttX tree using the exact setup command in the
[demo README](README.md#build-on-esp32-s3) and the pinned toolchain in
[`docs/nuttx-std.md`](../../docs/nuttx-std.md), then source its
`environment.sh`. The build tools preserve the prepared kernel settings;
matched C and Rust cases use the same kernel config, clock, queue transport,
gate, topology, and stack reservations. Only the selected application and
the case under study should vary. The stack reservation is a controlled common
cost, excluded when interpreting the C/Rust language delta.

Before a C matrix switches application selection, compare the ordinary Rust
entry/thread path with the narrower native lifecycle. These commands keep the
wire workload, gate, queue helpers and application optimization equal:

```sh
python3 tests/service-footprint/relink_rust.py \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --out target/service-footprint/std-runtime-run1 \
  --feature wire-only --feature synchronized-scale --feature shared-mq-code \
  --target-c-source tests/service-footprint/native_thread.c \
  --target-c-source tests/service-footprint/transport_gate.c

python3 tests/service-footprint/relink_rust.py \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --out target/service-footprint/native-runtime-run1 \
  --feature wire-only --feature synchronized-scale --feature shared-mq-code \
  --feature ffi-scale-entry --feature native-scale-worker \
  --target-c-source tests/service-footprint/native_thread.c \
  --target-c-source tests/service-footprint/transport_gate.c
```

This pair changes entry and thread lifecycle together. To isolate either one,
add only `ffi-scale-entry` to the first command, then compare that build with
the native-worker variant, each in a fresh output directory. The native-worker
helper is staged in both cases; unused functions can be discarded at link time.
For the packet temporary/direct-fill pair below, only `packet-inplace-samples`
changes. None of these commands flashes a device.

From the repository root, create fresh output directories and build a matched
transport control and packet-processing pair:

```sh
python3 tests/service-footprint/run_transport_matrix.py --build \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --baseline-config target/service-footprint/prepared/resolved.config \
  --out-root target/service-footprint/walkthrough-run1 \
  --case c-packet-wire-2 --case rust-packet-wire-2 \
  --case c-packet-2 --case rust-packet-2
```

The matrix adds its Rust-owned baseline and invokes `relink_rust.py` for Rust
cases. `relink_rust.py` saves each build under a new `--out` directory and
never flashes; use it directly for a single Rust variant when needed. For the
paired timing controls, `run_transport_paired.py` also builds by default and
requires a fresh output root:

```sh
python3 tests/service-footprint/run_transport_paired.py \
  --tree target/service-footprint/prepared \
  --sysroot target/service-footprint/prepared/toolchain \
  --out-root target/service-footprint/paired-run1 \
  --case wire --case packet --case packet-inplace
```

Neither command above accesses a device. Passing `--measure` explicitly
enables flashing and serial measurement; that is outside this walkthrough.
Consult each script's `--help` for supported cases and controls. Historical
image sizes and timing results are in the linked analysis and JSON record;
these recipes produce new evidence and make no claim about resulting device
size.
