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

The [no-PSRAM evidence](results/esp32s3-message-no-psram-2026-10-09.json) covers three
alternating-order blocks, fresh boots and 1,000 messages per language/topology.
All 12,000 arrived with valid sequences. Kernel config/archive/header hashes,
C compiler and stacks match; images use the frozen compiler and normal flash
placement. PSRAM is disabled in both resolved configurations.

| ESP32-S3 image | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Flat flash image | 213,436 B | 214,096 B | +660 B |
| Code + initialized data | 172,792 B | 183,026 B | +10,234 B |
| Resident RAM before live heap | 64,744 B | 64,888 B | +144 B |

Flat images include address padding, not debug sections; see the
[comparison](../../docs/rtos-comparison.md) for gap-free distribution packaging.

| Metric | C: 3 | Rust: 3 | C: 20 | Rust: 20 |
| --- | ---: | ---: | ---: | ---: |
| Full-capacity RAM | 96,780 B | 97,108 B | 191,980 B | 192,308 B |
| Peak total RAM | 97,168 B | 97,496 B | 192,368 B | 192,696 B |
| Median run mean, input to LED | 45.0 µs | 36.0 µs | 317.2 µs | 311.1 µs |
| Worst observed event | 548.6 µs | 623.6 µs | 974.2 µs | 1,062.9 µs |
| Events over 1 ms / 3,000 | 0 | 0 | 0 | 3 |

Rust adds 328 B RAM in both topologies (144 B resident, 184 B entry-task heap);
worker/queue growth is equal. The Rust large-case peak leaves 57,304 B of the
250,000-byte budget without PSRAM, before adding business logic and drivers.
Rust's mean is lower here but its tail is worse; no hard 1 ms guarantee is
established. The [earlier PSRAM-enabled cohort](results/esp32s3-message-2026-10-09.json)
remains separate; disabling PSRAM also changes code layout, so timing changes
cannot be attributed to memory placement alone.

The [flash-fetch/layout evidence](results/esp32s3-fetch-layout-2026-10-07.json)
explains build-to-build changes: 16 unexecuted padding bytes shifted functions
and added about 33/43 µs. Fetch counters and matched IRAM placement removed 99%
of Rust's recorded fetch waits and nearly all of the gap. Flash layout explains
most of the earlier observed difference,
though the exact cache conflict and production fix are unknown. Measure the
final linked image.

## Qualification status and evidence

The no-PSRAM [sustained-delivery check](results/esp32s3-sustained-no-psram-2026-10-09.json)
passed 80,000 events through twenty services, followed by 400 normal-rate
recovery events on the same boots. Each long command took about 40 seconds:
the requested 100 µs sleep did not produce 10,000 events/s; whole-command
throughput was about 500/s, including setup, teardown and serial completion.
Post-load/recovery heap and allocation counts matched (7,332 B C / 7,372 B Rust).
These are unchanged message images, not saturation instrumentation;
neither language has a hard deadline guarantee.

The separate [controlled-pressure check](results/esp32s3-pressure-no-psram-2026-10-09.json)
passed 40 calls: 26,176 verified deliveries and 12 intentional cancellations
on real producer queue-full responses. Unpaced production and bounded receiver
pauses produced real producer/forwarder `EAGAIN` retries; successful runs retained
and delivered every event. Each call joined 20 workers and closed/unlinked all
60 queues, then returned to its warm heap baseline after deferred frees were drained.
NuttX can defer exit-context frees until the next allocation: one worker's stack/thread
state occupies 4,328 B including heap headers. Both pre-drain heap and reclaimed
bytes are recorded, not hidden as a leak tolerance. This is diagnostic evidence,
not size/speed or arbitrary-overload qualification; cancelled traffic is not
counted as verified delivery.

The no-PSRAM [same-boot restart matrix](results/esp32s3-message-restart-no-psram-2026-10-09.json)
passed 80 calls/8,000 messages, with post-warmup heap flat at 7,332 B C / 7,372 B
Rust and stable allocation counts. These are the same images as the message test.
The separate no-PSRAM [shutdown-fault matrix](results/esp32s3-shutdown-faults-no-psram-2026-10-09.json)
passed 316 calls/12,400 events: 192 expected failures and 120 recoveries, with
tracked resources and heap returning to baseline. It tests stop-send, join and
reported close errors in the same owning task, not size/speed. Close errors are
reported after the real close succeeds; retained-descriptor close failures are
not qualified. Earlier PSRAM [restart](results/esp32s3-message-restart-2026-10-09.json)
and [fault](results/esp32s3-shutdown-faults-2026-10-08.json) cohorts remain separate.
[Linux shutdown](results/linux-shutdown-2026-10-08.json) and
[lifecycle](results/linux-lifecycle-2026-10-08.json) retain complementary
host fault/recovery evidence from before the GPIO snapshot update, not an
identical-source comparison or target memory/timing claims. Superseded
pre-hardening and traced images are omitted.

This demo does not qualify production: no jumper was available to test IRQ
delivery. Wedged handlers, foreign-task recovery, untested OS failures,
arbitrary overload, cyclic graphs and a complete driver/buffer budget remain open.
All current device matrices verified restoration of the full 16 MiB firmware.

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
`build.py rust`, `prepare --no-psram` and `link` create the measured images;
omit `--no-psram` only for a separate PSRAM-enabled study. `measure.py` flashes
only when explicitly run. Before flashing, save a private full-device backup,
verify its hash and preflight the device against it. The harness restores and
verifies the full image; capture or restoration failure prevents publication.
`restart_device.py` repeats normal app invocations on each boot;
`device_faults.py` requires separate images linked with `--faults`, and
`pressure.py` requires separate images linked with `--pressure`. They accept
the same image/flasher/backup arguments and export path-free evidence through
their `public_report()` functions. Never use diagnostic images for footprint
or latency comparisons.

```sh
python3 tests/service-qualification/measure.py \
  --c "$SQ_C_IMAGE" --rust "$SQ_RUST_IMAGE" --port "$SQ_PORT" \
  --flasher "$SQ_ESPTOOL" --backup "$SQ_PRIVATE_BACKUP" \
  --backup-sha256 "$SQ_BACKUP_HASH" --out "$SQ_MEASUREMENTS"
python3 tests/service-qualification/publish.py \
  --report "$SQ_MEASUREMENTS/report.json" --out "$SQ_PUBLIC_RESULT"
```

For the separate sustained/recovery cohort, add
`--services 20 --events 20000 --period-us 100 --recovery-events 100 --blocks 2`
to `measure.py`, and `--sustained` to `publish.py`. The timeout accounts for
the frozen board's timer resolution; wall time is not processing-only latency.

For controlled pressure, link each language with `build.py link --pressure`
using the same prepared kernel, flags and frozen Rust bundle as ordinary links.
Run `pressure.py` with the same image/flasher/private-backup arguments above
and `--blocks 2`; export only its successful `public_report()` after restoration.
The fixed diagnostic suppresses successful producer pacing, preserves retry
sleeps, stalls the terminal receiver for 20 ms every 64 events, and cancels on
a real producer queue-full response. Startup capacity probes are excluded.
These links are rejected by the ordinary size/latency publisher.

Keep backups, credentials, paths and transcripts private; GPIO mode requires
the confirmed physical fixture.
