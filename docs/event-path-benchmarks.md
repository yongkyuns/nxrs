# Event-path benchmark next steps

This is the execution plan after PR #252's [November 2 ESP32-S3
observations](../tests/std-channel-perf/results/2026-11-02-esp32s3/README.md).
It qualifies the chosen nxrs architecture: typed, bounded event delivery from
providers to one execution owner, one logical wait over its admission classes,
and predictable scheduling, overload and shutdown. The work below is **planned**;
the archived physical runs do not execute these proposed changes.

A complete Thread-Metric or multi-RTOS campaign is not a prerequisite. Use a
matched Zephyr/PX5 control later only when it answers a specific mechanism or
product-performance question. Existing historical cohorts remain separate.

## What the physical result changes

The [raw cohort and summary](../tests/std-channel-perf/results/2026-11-02-esp32s3/summary.json)
contain three fresh boots, each with 1,000 retained samples after 100 warmups.
The channel carries 16 bytes with capacity eight; sender/receiver FIFO priorities
are 120/180 on one ESP32-S3 core. All 3,000 receives have an independent native
blocked-state observation, valid payloads and completed joins.

| Observation | Consequence for the next work |
| --- | --- |
| Send entry to receive return: pooled p50 1,864, p99 1,871, maximum 11,958 CCOUNT ticks | A physical blocked-handoff baseline exists. These are end-to-end elapsed intervals, including scheduling, not pure queue or context-switch costs. |
| Send call: p50 3,577, p99 3,584, maximum 215,296 ticks | This includes execution that preempts the sender. The maximum needs a fixture-boundary correction before it can describe steady-state send behavior. |
| Reserved-slot consumer stalls until observer priority recovery; published control completes | Fixed-priority progress is now a demonstrated physical concern for this actual std interleaving. Progress qualification precedes transport adoption or speed claims. |
| Independent CPU-frequency calibration is absent | Keep raw ticks primary and timing qualification false. At the configured 240 MHz, handoff p50/p99/max would be 7.767/7.796/49.825 microseconds; those conversions remain conditional. |
| Common internal/PSRAM heap; exact allocation placement unobserved | Do not attribute the result to SRAM or PSRAM. Record or control placement in the next cohort. |

### Two findings from the retained raw samples

In **all three boots**, the 215,296-tick send maximum is sample **999**, while
receive latency at that sample is the normal 1,864 ticks. In
[latency.rs](../tests/std-channel-perf/rust/src/latency.rs), the higher-priority
receiver normally posts completion and blocks at the next iteration's arm gate.
On the final iteration it instead exits. Its cleanup can therefore execute
before the sender records its return timestamp. This strongly implicates terminal
lifecycle work; it does not isolate a particular destructor or pthread operation.
The conditional 897.067-microsecond maximum must not be described as an intrinsic
channel-send tail. Keep that observation in the immutable historical archive.

All three handoff maxima occur at sample **81**, with the coarse monotonic guard
crossing a 10 ms boundary. Each retained 1,000-sample interval spans only about
8.126 million ticks, conditionally **33.86 ms** at 240 MHz. Thus sample count alone
overstates timing-phase coverage. A tick/IRQ contribution is a hypothesis, not an
attributed cause. The regular eight-slot pattern in ordinary samples also warrants
retaining sample order and placement information rather than only quantiles.

These findings are reproducible by parsing the `sample` records in each
`runs/latency-*/console.log`, calculating unsigned 32-bit differences from
`send_start_ticks`, and retaining the sample indices. The official captures
remain timing-rejected; their separate analyses establish sample validity only.

## Benchmark the architecture that is actually selected

There are two related paths, with different qualification status:

| Path | Actual implementation and measurement purpose |
| --- | --- |
| Current single inbox | [shared/messaging](../shared/messaging/src/channel.rs) wraps positive-capacity std MPSC. Providers clone senders and fan into one owner; `wait` uses `recv`/`recv_timeout`. Measure the actual typed sink's `try_send` as well as the existing raw `send` baseline. |
| Chosen separate admission classes | The [architecture](concurrency-event-communication.md) chooses stop, important and ordinary bounded queues with one fixed-arm Crossbeam selection point. This is a qualification candidate, not a completed production transport migration. |
| Existing executable selection contract | The [Wi-Fi qualification owner](../tests/nuttx-std/wifi/src/owner.rs) already has three-arm `select_biased!`, a stop slot, an eight-important-event fairness turn, deadline rechecks and closed-arm disabling. Reuse this contract with deterministic synthetic providers; its functional evidence is not timing evidence. |
| Native device readiness | UART/FIFO/IRQ waiting stays inside the HAL provider. Service selection is not a generic file-descriptor `poll` reactor. Do not introduce a forwarding thread or one queue per provider merely to benchmark it. |

Provider count and selection-arm count are independent: four providers can share
one ordinary queue. `tests/service-footprint` also uses different host and NuttX
backends; its NuttX mq/poll results do not measure Crossbeam on the target.

Keep tightly coupled computation local. Measure one meaningful ownership transfer;
add a second hop only as a topology diagnostic, not as a requirement for every
processing stage. No std/Crossbeam channel is called from interrupt context.

## P0 — correct and qualify the physical baseline

Extend the existing `tests/std-channel-perf` fixture before adding a new campaign:

1. **Make terminal lifecycle consistent.** After the last completion, have the
   receiver wait on an explicit final gate. Release it only after the sender has
   recorded its last return and completed measurement. Include partial-failure
   and cancellation cleanup, bounded joins and watchdog recovery. Measure teardown
   separately. Do not discard the last sample or subtract the historical outlier.
2. **Make qualification outcomes explicit.** Emit structural/sample validity,
   physical-execution identity, clock qualification and progress outcome separately.
   A calibration failure must retain valid behavioral data and the failed timing
   gate in one report, without a second ad hoc analysis or relabeling failed timing
   qualification as a pass.
3. **Require a calibration artifact.** Record reference source/accuracy, raw
   bracketing counter/reference observations, duration, measured ratio, uncertainty,
   fixed-frequency/power state and the build/session association. A suitable
   independent hardware timer can establish a ratio if its own frequency basis is
   known; another frequency derived from the same CPU configuration is insufficient.
   The current broad 20 ms plausibility probe is not calibration, and free-form
   operator text alone must not qualify timing. Preserve rejection of missing,
   stale, inconsistent and unsupported calibration.
4. **Resolve the boot-image warning.** Investigate the ROM digest/header/image
   construction path, reproduce the expected digest, and rerun a clean boot.
   Flash-byte verification and the ROM embedded-digest check are different checks.
   Preserve the existing warning and source/build identity records.
5. **Measure the timer and placement controls.** Record counter-read overhead and
   resolution; retain wrap/monotonic checks and fixed core/frequency. Identify or
   constrain channel/shared-state and stack memory placement. Keep the current
   checking-enabled default and any tuned configuration separately named, with
   exact settings. Do not silently disable checks or change PSRAM/cache policy.
6. **Add phase coverage.** Keep the tight-loop baseline, then vary release phase
   outside the timed handoff using a recorded schedule/seed. Preserve index, raw
   timestamps and per-boot distributions. Use a longer duration spanning many
   tick periods; increasing only the number of back-to-back samples is insufficient.

Acceptance: final-sample lifecycle has the same wait boundary as ordinary samples;
failure cleanup terminates; the counter has auditable calibration; image identity
and clean boot agree; phase-varied captures satisfy delivery and clock checks.
An outlier may remain after the correction and must be retained and investigated.
Add focused negative controls for these failure modes; do not relax old gates.

## P1 — qualify progress for the intended scheduling contract

Keep the actual std reserved-slot witness and published-slot control. Their test
can pass by reproducing a known hazard; that is **not** a production progress pass.
The observed recovery interval is a test observation window, not a bounded channel
stall duration. Accept a production configuration only when it completes without
observer priority repair under its stated contract.

Before choosing Crossbeam for multi-queue owners:

- Pin and inspect the actual target Crossbeam source, then add reservation/publication
  witnesses for direct receive and three-arm selection. Drive an independent wake
  through another arm or deadline when reproducing the relevant interleaving.
  The std result motivates this check; it neither proves nor disproves Crossbeam
  progress on the target.
- Audit the corresponding sender/reclamation path and test it if it also waits
  behind a preempted participant. Use source hooks only in witness images.
- Read back actual scheduling policy, priorities and one-core execution; test
  the intended producer/owner relationships. Global round-robin configuration
  does not establish that explicitly FIFO workers time-slice.
- Keep an independently runnable watchdog and record ready/blocked states,
  publication, completion, recovery and joins. A lower-priority timeout thread
  cannot recover a higher-priority spinner that prevents it from running.

If the selected scheduling contract fails, choose and qualify a remedy before
adoption: a justified priority contract or a bounded transfer protocol preserving
typed ownership, overload, wakeup and shutdown semantics. Equal priorities,
`yield`, retries, another RTOS, or a different channel name are not proofs of a fix.
Do not write a replacement queue or parker based only on this diagnostic.

## P2 — a small timing ladder that isolates useful costs

Use one canonical experiment contract with named cases; reuse the #252 build,
capture, provenance and clock checks. A separate event-path fixture may host
the multi-provider owner without changing the meaning of the archived std modes.
No benchmark-only transport or selection policy enters production modules.

| Case family | Minimal controls | What it answers |
| --- | --- | --- |
| Ready transfer | Raw std, actual messaging wrapper, pinned Crossbeam; same-thread enqueue/dequeue plus explicit full rejection | Queue/payload/admission and wrapper cost without a receiver wake |
| Blocked handoff | Verified semaphore notification, `std::thread::park/unpark`, raw channel, messaging wrapper; same worker policy/stack | Where parking, wakeup and channel work enter the end-to-end cost |
| Fixed-peer handoff | Two runnable equal-priority FIFO peers; explicit yield-to-peer and confirmed switch identities | Task-to-task handoff with a known peer, separate from higher-priority wakeup |
| Selected wait | Crossbeam direct receive, one-arm selection, chosen three-arm selection | Incremental selection, registration/unregistration and re-arm work |
| Provider fan-in | One and four task-context synthetic providers; normal and burst/saturation schedules | Contention, useful delivery rate, queueing, fairness and deadline behavior |
| Provider-to-handler | Qualified native IRQ/timer notification to provider task, typed sink and owner handler | Acquisition wake, admission, owner dispatch and extra execution hops |

Start with the same 16-byte/capacity-eight baseline. Add one real normalized
sensor/event type with target `size_of`/alignment recorded and one product-relevant
capacity. The current fusion input capacity of 16 is a useful reference, not a
global requirement. Test the intended FIFO relationships rather than averaging
them together. Nonblocking sink admission is the primary production case;
blocked-producer release is a targeted control for call sites that permit blocking.

Compare raw std and its wrapper with identical calls and parameters. Keep `send`
and `try_send` results distinct. A native semaphore has no message payload and is
a scheduling control, not a semantically equivalent transport. Adding independent
queue capacity changes admission semantics; do not label the entire one-inbox to
three-queue difference as selector overhead. Compare one-arm selection and direct
receive on the same queue to isolate that narrower question.

Every timed loop must produce observable checked work, with retained sequence or
checksum validation so optimization cannot eliminate the transfer. The park/unpark
control needs a sequence/predicate protocol covering early notification, stale
tokens, spurious returns and lost-wakeup races; one unpark is not assumed to equal
one completed wake. Keep fixed-peer setup gates outside its handoff interval.

Do not run the full Cartesian product. Zero-capacity rendezvous, unbounded queues,
large arm-count sweeps, SMP, every allocator and every RTOS wait until a chosen
product path or an observed bottleneck requires them.

## P3 — make the single selection point the central acceptance test

Reuse the three-class owner behavior from the Wi-Fi fixture, with fixed participants
and no radio work. Measure both all-ready dispatch and independently verified
all-empty blocking; a readiness announcement alone does not prove a parked selector.
Rotate the active arm, then make arms ready simultaneously. Retain both the number
of actual wakes/switches and useful events so repeated wakeups cannot hide in a
throughput total.

The correctness corpus must cover:

- Arrival between readiness check and wait registration; arrival during re-arm;
  another arm winning while an unconsumed event remains available.
- Empty wait followed by message, deadline, stop and optional-source closure.
  Disable closed receivers so an idle owner cannot spin on a disconnected arm.
- Full ordinary capacity with an important event and a stop request; admission
  isolation must survive overload without blocking the producer.
- Sustained important traffic and the declared ordinary fairness turn. Check
  absolute deadlines while queues stay continuously ready and again after wake.
- Bounded drain/batching with a time/work budget, remaining-work visibility and
  prompt stop checks. Selection preference cannot preempt a running handler.
- A single provider failing while other senders keep the class queue connected;
  source faults must remain explicit and terminal faults independently observable.
- Cancellation, partial-start rollback, provider quiescence and joins after
  dispatch exits, including a full queue. No callback touches released resources.

Reuse [existing owner tests](../tests/nuttx-std/wifi/src/tests.rs) for behavioral
expectations. Deterministic race hooks belong in functional/witness images; normal
latency images keep channel/selector source uninstrumented.

Report wait/selection cost, idle CPU, wakes per useful event, actual switches,
deadline lateness, important/ordinary response distributions, full/closed outcomes
and bounded stop completion. A fixed-arm selector is accepted by these observations,
not by assuming a method called `try_recv` is bounded or allocation-free.

## P4 — attribute wakeup and switching before optimizing the kernel

The [physical P4 report](../tests/event-path-trace/results/2026-11-04-esp32s3/README.md)
retains 36 matched fresh-boot observations. It finds one ready/resume per
admission-to-receive interval and material instrumentation perturbation; equal
FIFO increases delivery latency. No production priority/transport change is admitted.

Keep release timing and diagnostic attribution in separate images. Preallocate
bounded trace storage, detect overflow, avoid per-event printing/allocation and
measure instrumentation perturbation with the same workload.

| Timestamp boundary | Interpretation |
| --- | --- |
| ISR/native-notification marker to provider task first marker | Provider notification, wake and scheduling delay |
| Admission entry to return | Caller-visible elapsed cost; may include preempting receiver work |
| Admission entry to receive/select return | End-to-end transfer, wake and scheduling latency |
| Receive/select return to handler start/end | Owner dispatch policy and bounded handler work |
| Source release to handler completion | Application response, including publisher lateness and queueing |
| Ready transition to first resumed application marker | Runnable delay plus restoration/return work, with markers explicitly scoped |

NuttX's `CONFIG_SCHED_INSTRUMENTATION_SWITCH` suspend/resume notes can identify
actual task switches. Their hook spacing does not enclose the entire Xtensa
register save/restore: both are called inside the scheduler's context-switch
function before restoration. Use them for identities/counts and a timeline; add
a narrowly scoped ready-transition marker if that attribution is needed. Only
claim pure architectural switch cost with markers that actually bound it.

Explicitly set/read back worker policy and prove the intended wait for native
controls too. The older `yield-alone` case need not switch tasks; semaphore
ping-pong is a complete notification round trip. Do not divide it by two and
call the result context-switch latency. CCOUNT intervals include IRQs/preemption
and are not per-thread CPU consumption. Do not subtract unrelated p99s or sum
medians to invent a stage decomposition; use correlated per-event traces.
Idle/worker CPU claims need validated idle residency or per-thread runtime
accounting; elapsed CCOUNT alone is insufficient. Planned source release to
native notification is a separate lateness interval when the workload includes it.

Optimize only the stage shown to dominate: extra owner hops/wakeups, bounded
batching, selector registration, common pthread/condition/semaphore paths,
ready-list work or code/data placement. NuttX mqueue changes apply to native mq
users and do not automatically improve std's array channel. A new parker requires
its own token, memory-ordering, lost-wakeup, timeout and shutdown proof.

## P5 — representative load and resource acceptance

Use absolute offered-release schedules, with one provider then four. Exercise
normal sensor-like traffic, a defined burst/near-saturation case, bounded owner
work and one long-handler sensitivity case. Preserve release lateness and planned
but unattempted events; admitted-only timing must not hide overload or coordinated
omission. Reconcile offered, attempted, admitted, rejected, handled and explicitly
discarded events. A faster run that loses required work cannot pass.

For the initial baseline/candidate pair, plan five fresh boots with counterbalanced
order and a duration covering at least 1,000 system-tick periods in the sustained
load case (10 seconds with the current 10 ms tick). This is an initial sampling
budget, not a proof of tail convergence or WCET. Tight handoff captures can be
shorter but need phase variation. Bound storage with checked blocks/histograms
and output only outside timed blocks; do not simply raise #252's 4,096-sample
limit and retain an unbounded raw vector. Preserve per-boot results and exact
histogram semantics when raw per-event storage is impractical.

Measure allocation and footprint separately from timing, reusing
[allocation-probe](../tests/allocation-probe) and the
[existing memory qualification phases](../app/ao-stress/src/memory_qualification.rs):
construction, fresh-thread first block/select, steady ready/block/wake, repeated
timeout, closure and teardown. Record Rust allocation activity plus native heap,
stack reservation/high-water and actual placement. Compare linked std-only,
Crossbeam-only, three-arm selection and coexistence images with matched kernel
settings. Bounded payload storage does not prove allocation-free waiter machinery.

Record numeric product targets for response/deadlines, loss/overload policy,
minimum useful throughput, stop latency and RAM/flash before accepting a change.
If targets are not yet set, publish characterization without an invented pass
threshold. Observed maxima remain observed, and energy claims require power data.

## Work allocation and completion criteria

| Work | Primary locations | Completion evidence |
| --- | --- | --- |
| P0: corrected baseline | `tests/std-channel-perf/rust/src/latency.rs`, `analyze.py`, `capture.py`, their tests | Final-gate/error controls; structured qualification; calibrated, clean-boot phase-varied captures |
| P1: progress gate | Existing std witness plus isolated pinned Crossbeam witnesses | Exact-path reserved/published cases; admitted scheduling contract completes without recovery |
| P2/P3: transport and selection | Actual `shared/messaging` caller; synthetic fixture reusing Wi-Fi owner semantics | Matched ready/blocked/selected cases; race, fairness, deadline, closure and stop gates |
| P4: mechanism attribution | Fixture-only NuttX trace/native glue | Correlated ready/switch/IRQ/application events, no trace overflow, perturbation quantified |
| P5: service/resources | Synthetic provider workload, allocation-probe and matched ELF/heap accounting | Reconciled load results, per-boot distributions, bounded resources and explicit product budgets |

Complete P0 and P1 first; the owner functional corpus can proceed in parallel.
Then capture the small P2/P3 baseline, use P4 only to resolve concrete attribution
questions, and qualify candidate changes under P5. Preserve #252's original
artifacts and failed qualifications unchanged. Extend the suite when a result
or production requirement justifies it, rather than making external RTOS parity
the critical path to improving nxrs.
