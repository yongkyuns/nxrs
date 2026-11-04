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
Every capture has its own frozen
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

The original captures and replay reports are retained privately. The table preserves individual boots without pooling.

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

## Scope

This publication contains aggregate measurements and public firmware provenance only. Device identifiers, host paths, raw sessions, console logs, private flash metadata and credentials are excluded. The original evidence is retained locally; the earlier raw publication was withdrawn from the current diff but remains in Git history. Independent clock calibration, placement and progress qualification remain open.
