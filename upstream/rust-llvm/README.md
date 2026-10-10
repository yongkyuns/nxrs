# Xtensa LLVM evaluation patches

Status: **inactive and evaluation-only**. The default compiler evaluation is
the six-patch series in [`proposals/series.json`](proposals/series.json),
pinned by [`upstream.json`](upstream.json) to Espressif LLVM
`14b9f5575f37489d4c25c069d04c9ed216dadc31` and Rust
`abf50ae2e46066e67e29fe856f9764c23aa9a3ca`. It passes 99 Xtensa CodeGen/MC
tests. It does not alter the installed SDK or normal application builds.

The series lowers 32-bit rotates through Xtensa's funnel-shift instruction,
enables legal hardware loops, fixes loop control-flow metadata and constant
shift selection, and models ESP32-S3 multiply latency for instruction
scheduling. Exact patch bytes and affected-file preimages are recorded in the
manifest and applied only to a private compiler source archive.

The isolated six-patch probe matched C/GCC cycles after GNU assembly, which
also corrected alignment; this does not prove native-object parity. The
[probe report](../../tests/event-services-comparison/results/esp32s3-compiler-isolation-2026-10-05.json)
preserves the focused result. The historical five-patch
[baseline](../../tests/event-services-comparison/results/esp32s3-compiler-baseline-2026-10-05.json)
and [patched matrix](../../tests/event-services-comparison/results/esp32s3-compiler-patched-2026-10-05.json)
are separate cohorts. The six-patch
[service matrix](../../tests/event-services-comparison/results/esp32s3-compiler-scheduled-2026-10-05.json)
delivered 312,000 messages and records 11/50 long-work misses for C/Rust. The
later alignment-candidate
[NuttX service report](../../tests/event-services-comparison/results/esp32s3-compiler-aligned-2026-10-05.json)
records 11.311/11.762 ms longest handlers and 1/16 misses. These results do
not establish universal parity.

The separate arithmetic series is frozen at 29-patch `mul-range`: range-proven
probes improve, but full-corpus output matches its parent and has no new device
result. The [assessment](../../tests/arithmetic-parity/RESULTS.md) distinguishes
the recurrence gain from the checked-multiply gap. The six-patch default is
unchanged. See the [private build guide](BUILDING.md); never install a candidate
into an SDK.
