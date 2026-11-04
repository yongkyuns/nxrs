# P0: isolate the seeded latency alternation

This is a fixture experiment, not a channel, parker or scheduler optimization.
The [physical archive](results/2026-11-03-esp32s3-simpleboot/README.md) remains
unchanged. In its seed-1 run, retained even indices contain 498 receives at
3,673 counter ticks plus two larger observations; odd indices contain 375 at
2,990 and 125 at 2,997. Tight runs have a different distribution. This describes
iteration dependence; neither channel slot placement nor a cache cause has been
measured. Do not subtract percentile values to infer a mechanism's latency.

## New fixed-delay control

The existing no-fourth-argument (tight) and numeric-seed commands remain valid.
A rebuilt latency image also accepts an explicit `fixed:N` fourth argument:

```sh
std_channel_perf latency 1000 100 fixed:1000
std_channel_perf latency 1000 101 fixed:1000
```

`N` is 1..2000 nominal microseconds converted to counter ticks using the recorded
frequency. It is **active setup pacing**, not a sleep, interrupt or timer-driven
provider, and executes before the per-sample blocked-receiver proof. No delay,
logging or instrumentation is added inside send/receive. Pacing remains bounded
by the unchanged iteration limit, per-wait deadline and 15-second watchdog.
Fixed and seeded runs are limited to 1024 retained samples and 128 warmups.

`capture.py` exposes mutually exclusive `--fixed-delay-us N` and `--phase-seed N`.
The fixed profile is `setup-fixed-counter-delay-v1`; its summary has seed zero
and an explicit `release_fixed_delay_us`. The host replays every requested fixed
delay and rejects old firmware, wrong profiles, wrong values, malformed fields
and undeclared pacing. Tight/seeded historical logs remain readable. All existing
boot, payload, clock, terminal-quiescence and source-identity gates stay intact.

## Small physical campaign

Use one freshly built **digest-candidate latency image** for every row, a verified
private flash backup, and a fresh boot/session/output directory for every command.
Do not overwrite the original physical evidence or reuse a prior backup blindly.
Flashing and restoration follow the [existing recipe](README.md#physical-board-recipe).
Old CI artifact 13254836111 does not support `fixed:N`; it must not be relabelled.

| Case | Samples | Warmup | Additional capture argument |
| --- | ---: | ---: | --- |
| Tight baseline | 1000 | 100 | none |
| Seed-1 repeat | 1000 | 100 | `--phase-seed 1` |
| Alternate seeded setup | 1000 | 100 | `--phase-seed 17` (then 113 separately) |
| Fixed short/even warmup | 1000 | 100 | `--fixed-delay-us 100` |
| Fixed short/odd warmup | 1000 | 101 | `--fixed-delay-us 100` |
| Fixed long/even warmup | 1000 | 100 | `--fixed-delay-us 1000` |
| Fixed long/odd warmup | 1000 | 101 | `--fixed-delay-us 1000` |

Repeat the seed-1 and fixed even/odd pairs on fresh boots; counterbalance pair
order. Compare all timing profiles within the **same new executable**, including
its tight baseline: adding fixture code can itself change code/layout placement.
Changing warmup from 100 to 101 also advances a seeded PRNG by one step, so that
comparison alone cannot isolate transfer parity. The fixed pair removes that
particular confound; it does not establish allocation or IRQ causation.

Example (after selecting the actual port, verified build and new session file):

```sh
python3 tests/std-channel-perf/capture.py physical \
  --build-directory "$BUILD" --mode latency --samples 1000 --warmup 101 \
  --fixed-delay-us 1000 --port "$PORT" --session "$SESSION" --output "$RUN"
```

Reset only after the runner is ready. These controls never flash automatically.

## Descriptive report, independently of new hardware execution

```sh
python3 tests/std-channel-perf/analyze_alternation.py \
  "$RUN/console.log" --output "$RUN/alternation.json"
```

This first applies the normal transcript validators, then reports tick-only
nearest-rank distributions and most-common values grouped by retained index mod
2, zero-based total transfer index mod 2, and total transfer index mod capacity.
Total transfer index is `warmup + retained_index`; **it is not an observed std
internal slot index**. Small cohorts retain empty bins explicitly. The final
sample is shown and never dropped. Reports hash the original console bytes,
refuse to overwrite files, subtract no overhead and grant no physical timing,
phase-coverage, performance-improvement or root-cause qualification.

A repeatable warmup-parity shift is a lead for a separately instrumented image,
not proof of a particular source line. Actual allocation/stack addresses and
ready/switch/IRQ correlations remain the next diagnostic boundary. Independent
clock calibration remains required for absolute timing qualification. No new
hardware observations are created by these controls or by their host CI tests.
