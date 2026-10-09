# Lean service qualification

This local ESP32-S3/NuttX demo compares C and Rust service dispatch on a shared
native pthread, message-queue and `poll` adapter. It uses the frozen
[29-patch Rust/LLVM package](../../upstream/rust-llvm/README.md), not the
proposed [production event API](../../docs/concurrency-event-communication.md).
Builds and measurements run locally, not in CI.

## Workload and contract

Three or twenty services forward 16-byte LED events to GPIO2 USERLED and a
terminal monitor; the large case has 20 workers and 60 queues. Stop/control/data
capacities are 1/8/8. Owners use one `poll`, retain one pending event under
backpressure and prioritize control. C/Rust share validation, counters, HAL,
traffic and 4,096-byte stacks, with separate service loops. Rust keeps normal
startup/CLI accounting. This is not a `std::thread` or Crossbeam device test.
The 64-byte stress contract is separate; these results do not generalize to
larger payloads or arbitrary graphs.

## Current message-image results

The [2026-10-09 evidence](results/esp32s3-message-2026-10-09.json) covers three
alternating-order blocks, fresh boots and 1,000 messages per language/topology.
All 12,000 arrived with valid sequences. Kernel config/archive/header hashes,
C compiler and stacks match; images use the frozen compiler and normal flash
placement.

| ESP32-S3 image | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Binary file | 213,380 B | 214,040 B | +660 B |
| Code + initialized data | 174,536 B | 184,770 B | +10,234 B |
| Resident RAM before live heap | 66,360 B | 66,504 B | +144 B |

| Metric | C: 3 | Rust: 3 | C: 20 | Rust: 20 |
| --- | ---: | ---: | ---: | ---: |
| Full-capacity RAM | 98,396 B | 98,724 B | 193,596 B | 193,924 B |
| Peak total RAM | 98,784 B | 99,112 B | 193,984 B | 194,312 B |
| Median run mean, input to LED | 55.0 µs | 36.0 µs | 327.3 µs | 311.0 µs |
| Worst observed event | 529.4 µs | 591.4 µs | 953.7 µs | 1,032.5 µs |
| Events over 1 ms / 3,000 | 0 | 0 | 0 | 2 |

Rust adds 328 B RAM in both topologies (144 B resident, 184 B entry-task heap);
worker/queue growth is equal. The Rust large-case peak leaves 55,688 B of the
250,000-byte budget. This PSRAM-enabled board does not qualify a no-PSRAM
product. Rust's mean is lower here but its tail is worse; no hard 1 ms guarantee
is established.

The [flash-fetch/layout evidence](results/esp32s3-fetch-layout-2026-10-07.json)
explains build-to-build changes: 16 unexecuted padding bytes shifted functions
and added about 33/43 µs. Fetch counters and matched IRAM placement removed 99%
of Rust's recorded fetch waits and nearly all of the gap. Flash layout explains
most of the earlier observed difference,
though the exact cache conflict and production fix are unknown. Measure the
final linked image.

## Qualification status and evidence

The current [same-boot restart matrix](results/esp32s3-message-restart-2026-10-09.json)
passed 80 calls/8,000 messages, with post-warmup heap flat at 7,332 B C / 7,372 B
Rust. The [shutdown-fault matrix](results/esp32s3-shutdown-faults-2026-10-08.json)
passed 316 calls/12,400 events. [Linux shutdown](results/linux-shutdown-2026-10-08.json),
[and lifecycle](results/linux-lifecycle-2026-10-08.json) retain complementary
host fault/recovery evidence from before the GPIO snapshot update, not an
identical-source comparison or target memory/timing claims. Superseded
pre-hardening and traced images are omitted.

This demo does not qualify production: no jumper was available to test IRQ
delivery. Wedged handlers, foreign-task recovery, untested OS failures,
sustained overload and a no-PSRAM budget remain open. The current matrix
verified restoration of the full 16 MiB firmware.

## Local checks and target measurement

Linux host checks need GCC, Rust and mounted `/dev/mqueue`; they test behavior,
not target RAM/timing:

```sh
cd tests/service-qualification
SQ_HOST_DIR="$(mktemp -d)"
SQ_HOST_C="$SQ_HOST_DIR/c"
SQ_HOST_CARGO="$SQ_HOST_DIR/cargo"
SQ_HOST_RUST="$SQ_HOST_CARGO/release/nxrs-service-qualification"
cc -std=c11 -D_DEFAULT_SOURCE -O2 -Wall -Wextra -Werror \
  runtime.c worker.c hal_host.c ../service-footprint/native_thread.c \
  -pthread -lrt -o "$SQ_HOST_C"
CARGO_TARGET_DIR="$SQ_HOST_CARGO" cargo test --offline --locked
CARGO_TARGET_DIR="$SQ_HOST_CARGO" cargo build --release --offline --locked
SQ_HOST_C="$SQ_HOST_C" SQ_HOST_RUST="$SQ_HOST_RUST" \
  python3 -m unittest discover -s . -p 'test_*.py' -v
```

Build with the pinned instructions in
[`upstream/rust-llvm/BUILDING.md`](../../upstream/rust-llvm/BUILDING.md) and
[`docs/nuttx-std.md`](../../docs/nuttx-std.md), using an isolated SDK, fresh
output directories and matched kernel config/archive/header hashes.
`build.py rust`, `prepare` and `link` create the images; `measure.py` flashes
only when explicitly run. Before flashing, save a private full-device backup,
verify its hash and preflight the device against it. The harness restores and
verifies the full image; capture or restoration failure prevents publication.

```sh
python3 tests/service-qualification/measure.py \
  --c "$SQ_C_IMAGE" --rust "$SQ_RUST_IMAGE" --port "$SQ_PORT" \
  --flasher "$SQ_ESPTOOL" --backup "$SQ_PRIVATE_BACKUP" \
  --backup-sha256 "$SQ_BACKUP_HASH" --out "$SQ_MEASUREMENTS"
python3 tests/service-qualification/publish.py \
  --report "$SQ_MEASUREMENTS/report.json" --out "$SQ_PUBLIC_RESULT"
```

Keep backups, credentials, paths and transcripts private; GPIO mode requires
the confirmed physical fixture.
