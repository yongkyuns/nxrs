# Public measurement evidence

Read the [RTOS analysis](../../../docs/rtos-comparison.md) for conclusions.
These records are separate frozen cohorts; do not pool their timing samples.
Each report preserves configuration, source and image hashes, case order,
per-run qualification and numeric observations. It excludes private backups
and raw serial logs. To inspect compact JSON, format a copy with
`python3 -m json.tool`.

| Record | Cohort |
| --- | --- |
| `esp32s3-minimal-task-trim-2026-10-09.json` | Current footprint: shell-free, no-PSRAM NuttX `-Os`, unused environment/child-task bookkeeping removed; unchanged work, stacks, queues and flash driver. Four-platform local controls and full-capacity checks. |
| `esp32s3-image-packages-task-trim-2026-10-09.json` | Gap-free, uncompressed packages for those exact measured images, with verified reconstruction hashes. |
| `esp32s3-minimal-2026-10-09.json` | Earlier shell-free profile, before environment/child-task cleanup; a separate frozen cohort. |
| `esp32s3-image-packages-2026-10-09.json` | Packages for that earlier cohort, not the current charts. |
| `esp32s3-controls-10ms-2026-10-04.json` | Instrumented 10 ms timing-control baseline. |
| `esp32s3-controls-1ms-2026-10-04.json` | 1 ms timer, synchronous-work and GPIO profiles. |
| `esp32s3-controls-saturation-2026-10-04.json` | Full-capacity fill/drain and deliberate-overflow qualification. |
| `esp32s3-scheduling-2026-10-04.json` | Natural I/O waits, cooperative budgets and chunked CPU work. |
| `esp32s3-compiler-isolation-2026-10-05.json` | Focused 29 kB compiler-cycle record; decisive proof that the default six-patch backend removes the measured instruction-scheduling penalty. Not service-deadline qualification. |
| `esp32s3-compiler-scheduled-2026-10-05.json` | Six-patch service confirmation; separate timing cohort, loss-free deliveries and remaining deadline results. |
| `esp32s3-compiler-aligned-2026-10-05.json` | Uninstrumented rebuilt-driver NuttX C/Rust service confirmation; separate cohort with footprint and deadline observations. |
| `thread-wrapper-costs-2026-10-03.json` | Historical `std::thread` versus native-wrapper control; not the event-service workload. |

Compiler reports use a private build with pinned source revisions and a patch
ledger; they do not change the normal SDK or portable applications. The
five-patch baseline and patched JSON records remain to satisfy the image-hash
provenance chain referenced by `compiler-scheduled`, but are not required in
this short reading index. Keep all cohorts distinct. Regenerate reports from
protected local measurement directories; never edit values to change a
qualification result.
