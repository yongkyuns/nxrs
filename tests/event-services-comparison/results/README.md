# Public measurement evidence

Start with the [independent analysis](../../../docs/rtos-comparison.md), not the
individual runs. These are separate, frozen cohorts; do not pool their timings.
The report schemas retain configurations, source/image hashes, rotated blocks,
per-run qualification and numeric observations, without raw serial logs or
private device backups. Large generated JSON files use compact whitespace;
format a copy with `python3 -m json.tool` when inspecting individual fields.

| Record | What it answers |
| --- | --- |
| `esp32s3-2026-10-04.json` | Initial independent service loops, 10 ms wakes, 20/60 queues, normal/burst |
| `esp32s3-controls-10ms-2026-10-04.json` | Matched 10 ms baseline for the finer-wake control |
| `esp32s3-controls-1ms-2026-10-04.json` | 1 ms wakes, longer handlers and GPIO actuation/readback |
| `esp32s3-controls-saturation-2026-10-04.json` | Repeated full-capacity fill/drain and deliberate overflow |
| `esp32s3-scheduling-2026-10-04.json` | Final natural I/O, cooperative budgets and CPU chunking; 168 invocations |
| `esp32s3-arithmetic-2026-10-05.json` | Discarded assembly diagnostic and fresh C controls; 80 invocations, not patched-compiler results |
| `esp32s3-compiler-baseline-2026-10-05.json` | Fresh unpatched NuttX C/Rust baseline; unchanged portable apps, 32 invocations |
| `esp32s3-compiler-patched-2026-10-05.json` | Private patched LLVM/Rust; five cases, 80 invocations; compiler/driver, patch and firmware provenance; long-work deadline failures retained |
| `esp32s3-compiler-isolation-2026-10-05.json` | Five/six-patch C/GCC, C/LLVM and Rust/LLVM diagnostic; flash/IRAM layout sweep, 864 samples per cohort; arithmetic parity, not service deadline qualification |
| `esp32s3-compiler-scheduled-2026-10-05.json` | Six-patch full-service confirmation; 80 invocations, 312,000 loss-free deliveries; improved long handlers, remaining deadline/timing differences retained |
| `esp32s3-handler-alignment-2026-10-05.json` | Matched diagnostic kernel: native versus GNU-assembled Rust input, 16 invocations; final-loop alignment explains the repeated execution cost, not full-service deadline parity |
| `esp32s3-handler-rust-driver-2026-10-05.json` | Rebuilt Rust driver with the inactive alignment proposal, native objects and matched diagnostic kernel; eight invocations, 936 jobs, original flash restored |
| `esp32s3-compiler-aligned-2026-10-05.json` | Same rebuilt driver in the uninstrumented NuttX C/Rust service matrix; 32 invocations, 124,800 loss-free deliveries; footprint and remaining deadline misses retained; other RTOSes not rerun |
| `thread-wrapper-costs-2026-10-03.json` | Historical controlled std/native thread-wrapper costs, not the scheduling firmware |

Each record identifies its own frozen firmware inputs; the portable Rust
scheduling matrix predates the compiler follow-up. Its private rebuilt compiler
uses the pinned source revisions and includes a patch ledger, rather than
claiming the discarded assembly experiment's speed. Regenerate reports
from protected local measurement directories using the package's report tools;
never edit numbers to make a run qualify.
