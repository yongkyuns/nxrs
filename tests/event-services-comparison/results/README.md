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
| `thread-wrapper-costs-2026-10-03.json` | Historical controlled std/native thread-wrapper costs, not the scheduling firmware |

The final scheduling source inventory matches the packaged firmware inputs.
Older records identify their own historical sources. Regenerate new reports
from protected local measurement directories using the package's report tools;
never edit numbers to make a run qualify.
