# P4 blocked-delivery attribution

This fixture investigates P2's blocked-receiver gap. Production messaging,
parker and scheduler policy are unchanged. Use a separate diagnostic executable,
the exact pinned std/Crossbeam implementations, and three matched arms:

| Image | Command arm | Purpose |
| --- | --- | --- |
| `baseline` | `quiet` | Workload with note instrumentation disabled |
| `trace` | `quiet` | Note hooks installed, recording disabled |
| `trace` | `record` | Same workload with bounded recording |

Both images wrap `nxsched_add_readytorun`. The wrapper records **after** the
real function returns, before its caller acts on the preemption result. This
is a post-insertion marker, not the first ready instruction. Pinned source
copies and linked references are required in the build receipt; correlated
physical ready events must prove the hook actually ran. Xtensa long-call
literal loads alone are not counted as direct calls.
Native source pins describe prepared sources, including the repository's
existing ESP32-S3 PSRAM size-mismatch return fix. IRQ records use task ID zero;
they identify interrupts without guessing the interrupted task from a ready head.

The Rust recorder stores at most 4,096 events (64 KiB plus indices), without
allocation, output, blocking or overwrite in callbacks. Single-core flat
ESP32-S3 and nonrecursive note configuration are required. Interrupt masking
protects fixed writes; every borrow ends before restoring interrupts.
The native callback table and Rust storage have boot-long lifetime because
NuttX has no note-driver unregister operation. Run **one command per boot**.
Cache-disabled execution and production ISR use are not qualified.

Rust owns setup, finite sampling, blocked-state proof, overflow rejection,
cancellation and joining. C only translates native TCB/note/IRQ/region ABI.
Failures abort the diagnostic instead of issuing a successful result; physical
operation requires an external deadline and board reset after any failure.

## Build and run

Linux x86-64 with the repository's hash-pinned Xtensa SDK is required:

```sh
python3 tests/event-path-trace/build.py --mode baseline --out target/p4-baseline
python3 tests/event-path-trace/build.py --mode trace --out target/p4-trace
```

Use a fresh private full-flash backup, verify its readback, flash the exact
receipt image at offset zero and verify flash before collecting. The capture
tool never flashes. Each private session file must contain:

```json
{"schema":"event-path-trace-session-v1","fresh_backup_verified":true,"flash_verified":true,"image_sha256":"<receipt image hash>"}
```

```sh
python3 tests/event-path-trace/capture.py serial \
  --build-directory target/p4-trace --port "$P4_PORT" \
  --session "$PRIVATE_SESSION" --output "$PRIVATE_RUN" \
  --backend crossbeam --policy unequal --recording record --samples 64 --warmup 16
python3 tests/event-path-trace/capture.py replay "$PRIVATE_RUN" --build-directory target/p4-trace
```

Open the console first, then reset when prompted. Retain all failures. Restore
and verify the fresh original flash in a `finally` path. Rotate std, messaging
and Crossbeam, with three fresh boots per case/arm. Add equal-FIFO Crossbeam as
a diagnostic candidate, keeping its results separate from unequal FIFO.
Prompt validation admits NSH's trailing erase-to-EOL sequence only. Comparison
also admits absent/off RTT and SYSVIEW note options becoming visible; it rejects
either driver being enabled and rejects unrelated configuration changes.

## Interpretation and privacy

Application, ready, switch and IRQ events are correlated within each sample.
The first ready marker and last resume marker are explicitly scoped; counts
preserve extra wakes/resumptions rather than inventing one unique transition.
Switch notes precede architectural restoration. CCOUNT includes preemptions
and interrupts: intervals are wall elapsed ticks, not exclusive CPU or pure
context-switch costs. Compare each arm/pass directly to quantify perturbation;
do not subtract independent percentiles. Frequency is configuration-derived
until independently calibrated on this setup.

Stack regions use actual TCB bases/sizes. Constructor allocation observations
use the Rust system allocator and native runtime PSRAM bounds. They do not
alone identify every deferred channel allocation or prove ring residency.
Regions are `0` unknown, `1` internal DRAM, `2` runtime allocable PSRAM.

Raw consoles, sessions, addresses, device identities, paths and flash backups
remain private. Only the analyzer's explicit aggregate allowlist is suitable
for a separately reviewed public summary. Never publish the complete run tree.

P1's equal-FIFO controls establish only their named scoped progress contract.
Re-running them does not qualify arbitrary blocking/full queues, production
owner progress or this instrumented image. No scheduling policy is adopted by
this fixture. Host tests and CI image builds are not physical qualification.
