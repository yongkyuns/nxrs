# P2 physical transport screening — November 4

All 18 fresh-boot captures and six official per-pass comparisons completed:
three boots for each requested ready/blocked std, messaging and Crossbeam
`try_send` case. The initial six cases form pass one. There were **zero failed
captures, zero rejected transfers and zero ROM digest warnings**. Each capture
retained 1,000 checked samples after 100 excluded warmups; all 1,100 admitted
values were handled and checksums verified. All workers joined; blocked cases
verified native receiver waiting and terminal quiescence.

Transport order within each ready/blocked family was std → messaging → Crossbeam,
then messaging → Crossbeam → std, then Crossbeam → std → messaging.
`campaign.json` preserves that order. Every capture has its own frozen
`event-path-session-v1` session and output directory. The capture tool's calculated
131-second serial deadline was used without an override. No retries occurred.

## Per-boot measurements

All numbers are **raw counter ticks**, in **p50 / p95 / p99 / observed maximum**
order, with nearest-rank quantiles. Each row contains 1,000 retained samples;
boots are not pooled. For ready cases the last column is `ready_receive_call`;
for blocked cases it is `admission_to_receive_return`. Their boundaries differ
and are not an additive decomposition. Every other measured interval remains in
the official analyses and comparison reports.

| Pass | Case | `admission_call` | Ready receive call / blocked delivery |
| --- | --- | --- | --- |
| 1 | ready-std-try | 141 / 143 / 143 / 143 | 150 / 152 / 152 / 152 |
| 1 | ready-messaging-try | 147 / 149 / 149 / 149 | 152 / 154 / 154 / 154 |
| 1 | ready-crossbeam-try | 167 / 170 / 170 / 170 | 214 / 217 / 217 / 217 |
| 1 | blocked-std-try | 30226 / 31456 / 31456 / 33097 | 13624 / 13885 / 13885 / 15442 |
| 1 | blocked-messaging-try | 22009 / 24952 / 24952 / 28015 | 13355 / 13355 / 13355 / 15368 |
| 1 | blocked-crossbeam-try | 33963 / 33969 / 35010 / 35871 | 21931 / 21937 / 21937 / 23611 |
| 2 | ready-std-try | 141 / 143 / 143 / 143 | 150 / 152 / 152 / 152 |
| 2 | ready-messaging-try | 147 / 149 / 149 / 149 | 152 / 154 / 154 / 154 |
| 2 | ready-crossbeam-try | 167 / 170 / 170 / 170 | 214 / 217 / 217 / 217 |
| 2 | blocked-std-try | 30226 / 31456 / 31456 / 32938 | 13624 / 13885 / 13885 / 15255 |
| 2 | blocked-messaging-try | 22009 / 24952 / 24952 / 26597 | 13355 / 13355 / 13355 / 14683 |
| 2 | blocked-crossbeam-try | 33963 / 33969 / 34929 / 35970 | 21931 / 21937 / 22141 / 23593 |
| 3 | ready-std-try | 141 / 143 / 143 / 143 | 150 / 152 / 152 / 152 |
| 3 | ready-messaging-try | 147 / 149 / 149 / 149 | 152 / 154 / 154 / 154 |
| 3 | ready-crossbeam-try | 167 / 170 / 170 / 170 | 214 / 217 / 217 / 217 |
| 3 | blocked-std-try | 30226 / 31456 / 31456 / 32938 | 13624 / 13885 / 13885 / 15268 |
| 3 | blocked-messaging-try | 22009 / 24952 / 24952 / 26149 | 13355 / 13355 / 13355 / 15236 |
| 3 | blocked-crossbeam-try | 33963 / 33969 / 35022 / 35871 | 21931 / 21937 / 22405 / 23608 |

The six `board/pass-{1,2,3}-{ready,blocked}-try.json` files are unchanged output
from the provided `compare.py`. All report `matched: true`; they preserve each
member's distribution and qualification separately. Every saved replay and
comparison was independently regenerated and compared exactly.

## Engineering interpretation

Keep the existing facade. The ready std and messaging paths have small but
repeatable, nonidentical distributions in this executable; rotation did not
change their reported quantiles. Inlining does not establish zero residual cost,
and these results do not isolate a facade-only mechanism. Surrounding generated
code and actual placement must be inspected before changing its API.

Blocked delivery and admission-call observations are substantially larger than
the ready call intervals. At sender/receiver FIFO 120/180, admission-call duration
includes preempting receiver execution and fixture work before the sender
resumes. It is not exclusive queue CPU time. Blocked tails vary across boots and
remain visible above. No percentile or counter overhead was subtracted to infer
a wake, context-switch or wrapper cost. These observations prioritize a separate
placement and correlated notification/parker/scheduling investigation; no such
instrumentation or production optimization is included here.

The current unequal-priority schedule remains outside P1's admitted equal-FIFO
Crossbeam contract. Successful finite samples do not erase P1's reserved-slot
stall or approve general transport progress. No transport ranking or replacement
recommendation follows from this screening campaign. No optional selection,
timer, saturation or notification cases were added.

## Conditions and remaining qualification

ESP32-S3 N16R8 QFN56 rev v0.2, USB/MAC `20:6E:F1:A7:6F:58`, 16 MiB flash and
8 MiB octal PSRAM. One configured 240 MHz core, PM disabled, 16 KiB instruction /
32 KiB data cache, eight ways, 32-byte lines, PSRAM at 40 MHz in the common heap.
Payload `bytes16`, capacity eight, 16 KiB worker stacks. Ready cases run in one
task; blocked cases record effective FIFO sender 120 / receiver 180. Normal
tick/USB interrupts remain; no camera or radio application is started. Actual
queue/stack addresses, cache residency and IRQ phase coverage were not measured.

No independent reference calibration was available for this P2 image/session.
All captures retain `physical_timing_qualified: false` and blockers
`P0-matched-physical-cohort-review-required` and
`clock-not-independently-calibrated`. Execution-context, physical-execution and
priority-progress qualification remain false. Operator identity/flash evidence
does not become independent authentication. Three boots do not establish WCET,
representative load or a hard realtime bound; values are not converted into
calibrated microseconds.

## Provenance, preservation and replay

The exact selected [artifact 13286791409](https://github.com/yongkyuns/nxrs/actions/runs/41752066250/artifacts/13286791409)
is from successful [workflow 41752066250](https://github.com/yongkyuns/nxrs/actions/runs/41752066250).
ZIP SHA-256: `5b8faa0925c110d00bbeee822392611342d0ef24e8a8ec840ce1c4c15c1f39f5`.
Firmware head is `af6738fc9559b2e0b2d1c57a049b70e4b7ac9580`, CI checkout
`bcc2da205687fa0e70f5900b3cac938ab24a6927`; verifier/report revision is
`fbc45294cc853caa047736ff368d0584915d94d4`. All 168 independently selected inputs
and all 16 retained artifacts verify. Candidate reconstruction, loader/patch
proof, ELF/image hashes and runtime build ID match the decision report exactly.
One unchanged digest-candidate executable serves all 18 boots.

`board/runs/` retains original console, build/session, capture, full analysis,
evidence manifest and replay bytes. `board/sessions/` preserves the supplied
per-boot sessions. Six comparisons remain at the shared root; `observations.json`
adds a descriptive per-run index without replacing official output. Large build
artifacts remain in the pinned CI ZIP. For Git-archive replay, copy `board/` into
a **new** working directory, then extract the verified CI ZIP into its `build/`
directory. Verify every entry in `ARTIFACT-ROOT-SHA256SUMS` against that root.
`archive-replay.json` records the tested relocation/rehydration check.

Using the pinned verifier and Python 3.11+, replay each run with its new shared
root, then invoke the existing comparisons for each pass separately:

```sh
python3 tests/event-path-perf/replay.py "$ROOT/runs/pass-1-ready-std-try" --artifact-root "$ROOT"
python3 tests/event-path-perf/compare.py ready-try \
  "$ROOT/runs/pass-1-ready-std-try" "$ROOT/runs/pass-1-ready-messaging-try" \
  "$ROOT/runs/pass-1-ready-crossbeam-try" --artifact-root "$ROOT"
```

Repeat for both groups and all three passes; do not pool them. A complete portable
artifact-root archive, including every original build file, was also retained
outside Git. `complete-artifact-root.json` records its size/hash.

A fresh private full-flash backup was verified before flashing, restored after
the campaign and verified again. Backup bytes/credentials are excluded from both
public evidence and the complete artifact-root archive. `operator.txt` records
the operator used; future tests require another fresh backup and new directories.
`SHA256SUMS` covers all public evidence except itself, preserving raw bytes.

Local checks: 131 P2 Python tests, the 53-package architecture audit, 23
architecture regressions, 10 HAL-boundary regressions and three documentation
tests pass. No runtime code, firmware inputs or qualification gates changed.
