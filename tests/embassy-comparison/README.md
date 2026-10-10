# Historical Embassy comparison

This isolated ESP32-S3 experiment compares Embassy Rust tasks with the
historical NuttX/Zephyr producer/worker/collector fixture. It adds no
production dependency and does not propose an OS migration. Current conclusions
are in the [RTOS analysis](../../docs/rtos-comparison.md); the newer
[event-service comparison](../event-services-comparison/README.md) has a
different independent-service workload and hardware-timer executor. Do not
pool their measurements.

## Workload and pinned setup

Four producers, 15 workers and one collector exchange 5,760 event/reply pairs
through 60 bounded queues. The 45 input queues hold one 248-byte event each;
15 reply queues hold four replies. Each queue has one receiver and multiple
possible senders. Workers wait on three inputs, the collector on 15, and
ready queues are selected round-robin. Full queues apply backpressure. Wire
mode checks routing, sequence, order and checksum; packet mode also processes
the shared 200-byte packet of 32 XYZ frames and verifies each filtered result.
Every run checks 5,760 replies and digest `441445568`. Timing spans the start
gate through final validation, includes backpressure, and excludes setup,
printing and teardown. Latency samples begin immediately before send.

Embassy uses 20 cooperative tasks, bounded static channels and no heap; these
are not preemptive threads. The primary experiment uses the official
`platform-spin` executor, so it does not qualify idle power. A separate
one-event-per-turn yield control measures the latency/throughput tradeoff.
Long synchronous work can still delay peers.

Pins are `embassy-executor` 0.10.0, `embassy-sync` 0.7.2, `esp-hal` 1.0.0,
and ESP Rust 1.90.0.0; all Cargo dependencies are frozen in this directory's
`Cargo.lock`. Builds use `cargo +esp`, `build-std=core`, static channel/task
storage and an app-owned 8,192-byte shared main/executor/interrupt stack.
The build checks the ELF stack and guard. No Wi-Fi, Bluetooth, network or
PSRAM support is included. The superseded worker/reply measurements are not
retained as another platform ranking; the current independent-service evidence
is indexed [here](../event-services-comparison/results/README.md).

## Reproduce locally

For opt-in dependency setup, use the
[shared installer](../event-services-comparison/README.md#optional-toolchain-setup)
with `--platform embassy`, then source its generated `environment.sh`.
Rust toolchains and Cargo downloads stay in ignored `target/rtos-comparison/`.
Adding `--build` builds the newer event-service fixture; use the commands below
for this historical packet/reply workload.

Use the pinned ESP Rust toolchain, matching Xtensa GCC linker, espflash 4.6.0,
and target GNU `readelf`/`nm`. NuttX and Zephyr controls come from their
existing pinned fixtures. Use fresh output directories.

```sh
python3 -m unittest discover -s tests/embassy-comparison -p 'test_*.py'
(cd tests/embassy-comparison && cargo +1.90.0 test --locked \
  --target x86_64-apple-darwin --features packet,cooperative-yield)
python3 tests/embassy-comparison/build.py --out target/embassy-packet \
  --mode packet --profile release --espflash "$ESPFLASH" --readelf "$READELF"
```

Use `--mode wire`, `--profile size`, or `--cooperative-yield` for the other
controls. Before flashing, make a private full 16 MiB flash backup, verify its
length and SHA-256, and `chmod 600` it. Never commit or upload it; firmware may
contain credentials. Run measurements through `run_matrix.py`, which checks
frozen image hashes and restores and verifies the original full flash in
`finally`, including after failure. Retain private backups and raw serial
logs locally; the public report contains sanitized numeric evidence.
