# P2 event-path engineering decision

## Current decision

Keep the existing messaging facade while measuring the underlying queue and
wakeup path. Its source adds no second queue, lock, worker thread or per-message
allocation. In the retained ESP32-S3 executable, its `send`, `try_send` and
`wait` methods are inlined. This rules out an extra out-of-line facade call in
this build; it does not establish identical instructions or equal cycle cost.

The immediate architectural constraint is scheduling. P1 now reproduces an
unequal-priority selected-channel reservation stall on the physical board.
Changing the wrapper cannot repair that underlying progress problem. P2's
current sender/provider 120 and receiver/owner 180 relationship remains a
characterization baseline, outside P1's equal-FIFO Crossbeam contract.

The next performance deliverable is six existing cases, three fresh boots each,
with separate queue-call and blocked-delivery measurements. No production
transport or scheduler optimization is justified by the current P2 evidence.
The [sanitized November 4 summary](results/2026-11-04-esp32s3/README.md) now records
18 successful captures and six matched comparisons. Identifiers and original raw
evidence are excluded from that publication. Timing calibration and placement
review remain open; no firmware input or runtime behavior changes.

## Evidence that informs the decision

### The facade is thin in source and in the target build

The actual [messaging facade](../../shared/messaging/src/channel.rs) contains one
`SyncSender` or `Receiver` field per endpoint. `send` and `try_send` delegate
directly; `wait(None)` delegates to `recv` and maps disconnection, while
`wait(Some(timeout))` delegates to `recv_timeout`. The
[P2 transport adapters](rust/src/transport.rs) use static dispatch over these
actual types.

The inspected executable is from the existing
[digest-candidate artifact 13286791409](https://github.com/yongkyuns/nxrs/actions/runs/41752066250/artifacts/13286791409),
built from P2 head `af6738fc9559b2e0b2d1c57a049b70e4b7ac9580`. Its retained
receipt, sixteen artifact hashes, all 168 source-input hashes and reconstruction
of the image from its retained original were checked again for this report.

| Item | Exact identity |
| --- | --- |
| Artifact ZIP SHA-256 | `5b8faa0925c110d00bbeee822392611342d0ef24e8a8ec840ce1c4c15c1f39f5` |
| ELF SHA-256 | `a931b1ae565042b859d2071a0048ef07f5635c0f21122259ca93cf6de9a0d541` |
| Selected image SHA-256 | `5e738b3ae480ec6fb920ce1d9dd2b04d2f49265a9d31047e51c96b724224c583` |
| Runtime build ID | `b42d3d84adc4c52bd2420067e4de7c8442030d26288e0269bcfaac7942e93b26` |
| Compiler / optimization | ESP Rust 1.90.0 nightly, `abf50ae2e`; application opt-level 2 |

DWARF records the concrete `messaging::Sender::{send,try_send}` and
`messaging::Inbox::wait` instances as inlined for both fixture payloads.
The final symbol table contains no standalone implementation of those facade
methods. This is useful compiled evidence against a speculative wrapper-call
optimization. It does not prove that error mapping, layout, surrounding caller
code, cache behavior or the complete raw-std/messaging paths have identical cost.

The following abstract DWARF entries each have `DW_AT_inline = 1`, concrete
`DW_TAG_inlined_subroutine` references and no out-of-line instance:

| Method / payload | Abstract entry offset | Inline debug entries |
| --- | --- | ---: |
| `Inbox::wait` / `ImuSample` | `0x2d0cc6` | 6 |
| `Inbox::wait` / `Bytes16` | `0x2d0cd2` | 6 |
| `Sender::try_send` / `Bytes16` | `0x2d0cfc` | 5 |
| `Sender::try_send` / `ImuSample` | `0x2d0d08` | 4 |
| `Sender::send` / `Bytes16` | `0x2d0d14` | 2 |
| `Sender::send` / `ImuSample` | `0x2d0d20` | 1 |

These counts describe debug entries, not runtime calls or samples. The `wait`
entries do not by themselves separate timeout and indefinite-wait uses. Endpoint
structure types are absent from this DWARF, so no numerical endpoint-size or
heap-usage finding is inferred.

To inspect the same evidence after extracting the artifact:

```sh
sha256sum target/p2-board/build/firmware/nuttx/nuttx
nm -C -S --defined-only target/p2-board/build/firmware/nuttx/nuttx > target/p2-board/symbols.txt
rg 'messaging::' target/p2-board/symbols.txt
readelf --debug-dump=info --wide target/p2-board/build/firmware/nuttx/nuttx > target/p2-board/dwarf-info.txt
rg -A6 'DW_AT_linkage_name:.*_ZN9messaging7channel(14Inbox|15Sender)' target/p2-board/dwarf-info.txt
rg -B1 'DW_AT_abstract_origin.*<0x2d0cc6>' target/p2-board/dwarf-info.txt
```

The symbol search has no matches, so its exit status 1 is expected. The final
command selects the six `ImuSample` wait instances; use the other offsets to
inspect their references. These commands inspect data and execute no firmware.

### Existing physical timing belongs to P0

[P0's corrected physical baseline](https://github.com/yongkyuns/nxrs/blob/634552533aeee23c1dbed5b7389c11612e98e15a/tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/README.md)
provides the following observations. Each row is one fresh boot with 1,000
retained samples, 100 excluded warmups and verified blocked receives. Keep the
rows separate:

| P0 case | Receive p50 / p99 / observed maximum, cycles | Send-call observed maximum, cycles |
| --- | --- | ---: |
| Legacy packing, tight | 1,866 / 1,873 / 3,581 | 5,901 |
| Digest candidate, tight 1 | 1,866 / 1,873 / 3,686 | 5,395 |
| Digest candidate, tight 2 | 1,866 / 1,873 / 3,511 | 5,227 |
| Digest candidate, tight 3 | 1,866 / 1,873 / 3,683 | 5,392 |
| Digest candidate, seeded delay 1 | 2,997 / 3,673 / 5,232 | 13,268 |

These are P0 raw-cycle observations. Independent clock calibration and actual
queue/stack placement remain open. The legacy and candidate are separate builds;
their differences do not establish a speedup. The seeded schedule changes setup
activity and does not establish IRQ phase coverage or identify the cause of its
different distribution. This sensitivity is a reason to preserve operating
conditions and individual boot results in P2.

P0's subsequent [thirteen-boot latency-control campaign](https://github.com/yongkyuns/nxrs/blob/4aeb64ceef1e2ea51ebe0fd7cb42a7ef42c12ec1/tests/std-channel-perf/results/2026-11-03-esp32s3-latency-controls/README.md)
uses a new executable and adds 13,000 retained samples. The older large seeded
alternation does not recur there: both new seed-1 runs have receive p50/p99/maximum
of 1,865/2,821/6,050 ticks. This executable difference is not evidence of a
transport improvement. A smaller 1,872-tick component repeats in four fixed-delay
runs, shifting retained parity when warmups change from 100 to 101. It tracks
warmup-adjusted transfer count modulo eight; internal slot, cache, scheduler and
IRQ causes remain unproved. Calibration, actual placement and IRQ phase coverage
remain open in that campaign too.

### Physical progress now constrains the choices

[P1's November 4 physical campaign](https://github.com/yongkyuns/nxrs/blob/8efbd59cd392678aaedf1f00addc80a30a1682a3/tests/channel-progress/results/2026-11-04-esp32s3/README.md)
records fifteen fresh boots:

| P1 observation | Consequence for P2 |
| --- | --- |
| Selected reserved-slot case stalls at owner/producer/waker FIFO 180/120/160 and needs recovery | Successful P2 samples cannot approve that general unequal-priority transport contract. |
| Six equal-FIFO Crossbeam cases finish without repair | This is a scheduling candidate for the specifically audited bounded-array and receive-selection paths. |
| Seven std mutex/condition-variable single-item cases finish without repair | These primitive observations do not qualify the separate bounded std channel used by the facade. |

The equal-FIFO contract requires one core, equal effective FIFO priorities,
admitted endpoint/lifecycle participants, no wait/yield/user code while owning an
incomplete reservation, bounded runnable work and bounded interference. It is
not a method-return latency bound. P2 does not silently change its priorities or
apply that Crossbeam source argument to std. A scheduling candidate would be a
separately named experiment with its own source review and matched evidence.

All fifteen P1 boots retain the legacy ROM warning and the physical qualification
flags remain false. Four original direct/reverse-send modes have no physical
execution in that campaign. The [P1 source audit](https://github.com/yongkyuns/nxrs/blob/8efbd59cd392678aaedf1f00addc80a30a1682a3/tests/channel-progress/FIFO-SCHEDULING-AUDIT.md)
defines the exact scope.

## First board campaign: eighteen captures

Use the authenticated candidate above, one board and one unchanged configuration.
The initial payload is `bytes16`, capacity eight. Retain native-counter values,
the declared single-core schedule and 16 KiB worker stacks.

| Three cases per family | Primary measurements | Question |
| --- | --- | --- |
| `ready-{std,messaging,crossbeam}-try` | `admission_call`, `ready_receive_call` | What does each ordinary queue call cost, including any residual facade cost? |
| `blocked-{std,messaging,crossbeam}-try` | `admission_to_receive_return`; separately `admission_call` | What does a verified blocked delivery cost through each path? |

Use 1,000 retained samples and 100 excluded warmups per capture, with three
fresh boots per case. Rotate transport order within each family:

| Pass | Transport order |
| --- | --- |
| 1 | std, messaging, Crossbeam |
| 2 | messaging, Crossbeam, std |
| 3 | Crossbeam, std, messaging |

This produces eighteen captures and six existing comparison reports: one
`ready-try` and one `blocked-try` report per pass. It is an initial screening
campaign. Three boots and 1,000 samples per boot do not establish a worst-case
bound. Order rotation does not establish IRQ phase coverage.

Retain sample order and inspect any repeating sequence-dependent component
before attributing a difference to a transport. P2's one-provider `sequence`
already includes warmup; its zero-based total-transfer index is `sequence - 1`.
That index is not an observed internal queue slot. If a P2 case shows such a
component, a same-image warmup-100/101 follow-up is an available conditional
diagnostic. Keep it separate from the primary comparisons, which require equal
warmup counts. The first eighteen captures remain unchanged.

Physical execution stays with the coordinated hardware workstream. Before
running, the operator must have exclusive board access, a fresh verified backup
and a restoration plan, and must verify the exact selected image. Sessions must
record the actual board, flash verification, core count, frequency, cache,
placement, power management and external load. Observed limitations belong in
the session; a configuration default is not a measured placement result.

P0's independent reference record must bind to the actual P2 image/build and
measurement session before absolute timing can be reviewed. Do not copy a P0
calibration claim onto P2. Without a reference, omit the calibration record and
retain the official unqualified result; raw-cycle characterization may still be
collected. Physical timing and progress acceptance remain separate reviews.

### Existing capture and comparison commands

Extract the verified artifact under `target/p2-board/build`. Place actual
per-boot session files under `target/p2-board/sessions`, using
`event-path-session-v1` and the fields described in the
[capture contract](README.md#evidence-and-pending-acceptance). P0's
`std-channel-session-v1` schema cannot be copied verbatim. Set `P2_PORT` to the
exclusively assigned device. From the repository root, the first capture is:

```sh
python3 tests/event-path-perf/capture.py serial \
  --build-directory target/p2-board/build \
  --artifact-root target/p2-board \
  --port "$P2_PORT" \
  --session target/p2-board/sessions/pass-1-ready-std-try.json \
  --case ready-std-try \
  --samples 1000 --warmup 100 --payload bytes16 \
  --output target/p2-board/runs/pass-1-ready-std-try
```

Capture opens serial and asks for a fresh reset. It sends one benchmark command;
it does not flash. Use its calculated serial deadline. Every attempt needs a
new output directory, including a failed boot. Preserve failures and stop to
investigate them; do not replace them with successful retries.

After completing the three ready cases in a pass:

```sh
python3 tests/event-path-perf/compare.py ready-try \
  target/p2-board/runs/pass-1-ready-std-try \
  target/p2-board/runs/pass-1-ready-messaging-try \
  target/p2-board/runs/pass-1-ready-crossbeam-try \
  --artifact-root target/p2-board \
  > target/p2-board/pass-1-ready-try.json
```

Repeat the existing `blocked-try` comparison for that pass. Comparison already
replays each member and checks matched build, payload, counts and declared
conditions. Keep every report and the complete artifact root. Report per-boot
p50, p95, p99, observed maximum, sample count and rejected/failed attempts.
Do not pool boots or subtract their percentiles.

### Add a case only for a specific decision

- To evaluate multiple admission classes, add `ready-select-one`,
  `ready-select-three`, `blocked-select-one` and `blocked-select-three` at the
  same counts and three passes. Reuse each pass's direct Crossbeam capture in
  `ready-selection` and `blocked-selection`: ten unique cases, thirty captures
  in total. The current unequal-priority scope still applies.
- To measure the instrumented provider-to-handler path with the real payload,
  add `timer-provider-messaging`, `imu16`, 128 samples and 16 warmups for three
  boots, then replay each with `replay.py`. The 50 ms arming interval is setup,
  not a sensor sampling requirement; no sensor is acquired.
- Add semaphore/park controls if blocked delivery is a material cost and a
  notification-path investigation is needed. They transfer different work from
  the channel cases and cannot be subtracted as a matched queue baseline.
- Add `send` only for a selected call site that permits waiting for capacity.
  The current `*-send` cases use nonfull queues. Add saturation/fan-in only to
  answer a concrete admission or retention-policy question.

## Interpret the intervals before choosing an optimization

| Observation | Next engineering action |
| --- | --- |
| Raw std and messaging are comparable across repeated ready captures | Keep the facade; focus on the shared implementation. |
| A reproducible facade-only difference exceeds run-to-run variation | Inspect the exact surrounding generated code and placement before changing the API. Inlining alone does not rule out every difference. |
| Blocked delivery is material relative to ready queue-call costs | [P4's physical traces](../event-path-trace/results/2026-11-04-esp32s3/README.md) show one ready/resume per measured transfer and material probe perturbation. Equal FIFO worsens delivery; keep production policy unchanged and narrow the probe before quantitative attribution. |
| Admission-return tails are large while delivery tails remain small | Check receiver preemption and work performed before the producer resumes. |
| Three-arm delivery adds a material cost | Decide whether separate admission classes are required by the product; preserve stop/priority semantics when considering a single inbox. |
| A progress failure occurs or the schedule is outside the accepted contract | Resolve the transport/scheduling contract before performance-driven production adoption. |

The [ready and blocked intervals](rust/src/ladder.rs) are not an additive
decomposition of scheduler cost. Ready delivery includes intermediate
admission-return and receive-entry counter reads and a same-thread receive.
Blocked delivery ends in another task after a verified wait. Subtracting their
p50 or p99 values does not isolate a wake or context switch.

At FIFO 120/180, caller-visible admission duration can include the higher-priority
receiver's execution. It is not exclusive queue CPU time. Single-arm
`select_biased!` expands to direct `recv`; the three-arm case also introduces two
connected idle queues. Its full difference includes that topology.

The [timer/provider intervals](rust/src/provider.rs) include instrumentation:
provider-to-admission includes state publication, a monotonic read, expiry and
cancellation checks, and payload construction. Receive-to-handler includes
fixture checks. Report them as the measured stage intervals; the planned timer
deadline is not an observed IRQ timestamp.

## Remaining execution boundary

The candidate is built and the first campaign is specified. This continuation
has no connected board interface and has executed no physical P2 capture.
Independent calibration, actual placement review and coordinated board access
remain with P0/hardware; progress-contract review remains with P1. The historical
[pre-shell ARM failure](README.md#preserved-arm-startup-failure) also remains
unresolved. No successful later capture can erase it.

The useful next result is the eighteen-capture table with these interpretations,
followed by one evidence-supported optimization or a decision to keep the
measured path. Additional benchmark families are conditional on that result.
