# Event-based service comparison

This temporary ESP32-S3 experiment compares independent services on NuttX C,
NuttX Rust, Zephyr C and Embassy Rust. It adds no production dependency. See
the [RTOS analysis](../../docs/rtos-comparison.md) for conclusions; the earlier
[producer/worker/reply workload](../zephyr-comparison/README.md) is distinct
and its timings must remain separate.

## Workload and contract

Twenty state-owning services publish control, data and status events to three
peers at offsets 1, 3 and 7 modulo 20. Each inbox has one receiver and multiple
senders; there are no replies or collector. One wait-any point covers all
inboxes and the next publication deadline. Ready queues are round-robin; an
iteration handles at most one event and publishes at most one due batch.
The original Embassy policy yields between iterations; natural, budgeted and
chunked controls use the same service contract with timer-backed I/O waits.

The three-queue layout uses 60 queues of eight 64-byte slots; the mailbox
layout uses 20 queues of 24. Both provide 480 slots (30,720 payload bytes).
Class queues isolate event types; a mailbox shares capacity and FIFO order.
Sends never block, retry or overwrite: a full destination is counted as a
rejection. Normal and burst qualification requires all releases attempted,
all accepted traffic drained, and zero rejections or protocol errors.

Traffic runs for two seconds, followed by a 500 ms drain. Control/data/status
periods are 250/50/100 ms. First release is one period after start plus the
service ID in milliseconds and twice the class ID in milliseconds. All
releases follow the absolute shared-start calendar; late ones are attempted
and measured. Normal/burst attempt 3,900/6,240 deliveries; burst doubles data
events. Overload uses 10 ms data periods,
16 events per release and zero phase; rejections are expected and explicit.
Unattempted releases at drain end invalidate a run.

Each C/Rust handler validates the 44-byte payload, routing and per-flow
sequence, then applies the same wrapping 32-bit state update; host differential
tests compare generated events and handler results. Deadlines are 20/40/80 ms
for control/data/status and are reported separately from delivery.

## Measurement and evidence

The matched 2026-10-04 cohort uses one 240 MHz core, DIO 40 MHz flash, 16 KiB
instruction/32 KiB data caches, `-O2` applications and 10 ms timer resolution.
All timestamp the same 16 MHz ESP32-S3 SYSTIMER. Initialization, staging,
printing and reconciliation are outside the timed interval.

Post-to-handler runs from just before send to handler start, including the
send, queueing and scheduling. Release-to-handler also includes publisher
timing. Publication lateness, handler finish and control response are
separate metrics. Per-service histogram upper bounds are capped at the
observed maximum; summary p99 is the median of per-run p99 bounds, not pooled
samples. RAM separates queues, execution, service state and diagnostics.
NuttX heap high-water is not a full-capacity bound; static reservations count
once, and thread stacks are execution cost.

Original, control, scheduling and compiler measurements are separate frozen
cohorts; retain their records and hashes and never pool changed configurations.
Compiler results use a private pinned LLVM/Rust build with source revisions
and patch ledger. Normal builds use the installed SDK; the patchset remains
inactive pending wider qualification. See the
[public evidence index](results/README.md). Superseded exploratory matrices
are not part of the retained result set. Raw serial logs and full-flash
backups are private local data, not report artifacts.

## Reproduce locally

Use the existing pinned platform toolchains and prepared NuttX tree. Build
fresh output directories for both layouts and each platform; retain each
`build-provenance.json`. The builder checks frozen inputs and hashes. For
example, provide the platform-specific paths required by `build.py`:

```sh
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
python3 tests/event-services-comparison/build.py --help
python3 tests/event-services-comparison/run_matrix.py --help
```

Before any device run, make a private full 16 MiB flash backup, verify its
length and SHA-256, and protect it with `chmod 600`. Use the matrix runner
(not the low-level measurement script); it verifies frozen image hashes,
rotates case order, and restores and verifies the original full flash in
`finally`, including after failure. Keep the board under one tool's control.
The command shape is:

```sh
python3 tests/event-services-comparison/run_matrix.py \
  --artifacts target/event-services-images --out target/event-services-runs \
  --backup /private/path/device-before.bin --port /dev/cu.YOUR_BOARD \
  --flasher /path/to/esptool.py --runs 3 --blocks 2 \
  --profile normal --profile burst
python3 tests/event-services-comparison/report.py \
  --matrix target/event-services-runs/matrix.json \
  --out target/event-services-results.json
```

For the capacity/wake controls, use `control_matrix.py` with `--profile
saturation` or matched `--timer-ms 10`/`--timer-ms 1` builds. For the final
I/O/CPU comparison, build the three-queue variants with `--timer-ms 1`; select
`--embassy-scheduling natural` or `budget`, adding `--work-mode chunked` for
the chunked policy. Use `scheduling_matrix.py` with `--runs 2 --blocks 2`
and profiles `normal`, `work-medium`, `work-long` and `io-wait`.
Both matrix runners take the same artifact, output, backup, port and flasher
arguments shown above. Their `--help` lists case names. Export using
`control_report.py` or `scheduling_report.py`, respectively; retain compiler
ledgers separately when evaluating a private compiler.

Never commit or upload the backup; firmware may contain credentials. Preserve
failed runs. An overload run that misses scheduled releases or fails to drain
is not a valid speed result.
