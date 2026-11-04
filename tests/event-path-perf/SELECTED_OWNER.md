# P3: measure the selected owner, then accept a performance change

This fixture moves the #252 focus from timer anomalies to useful service work.
It is stacked on P2 (#256); it does not replace that transport ladder or P1's
progress requirements, modify production messaging, or change P0 evidence.

`owner.rs` implements the chosen three-class policy with real bounded Crossbeam
queues: one lifecycle-only stop slot, eight important slots, eight ordinary
slots, at most eight important dispatches before giving ready ordinary work a
turn, one blocking selection point, and deadline checks before and after waiting.
Disconnected data arms are disabled. An already-selected message is retained
across deadline work. Stop cancels queued work, with explicit discard accounting.
It follows the Wi-Fi fixture's policy; it is not the Wi-Fi controller itself.

Two strategies execute the same owner policy and handler:

- `select`: call the biased selector for each data dispatch.
- `ready`: first try the ordered queues directly, respecting the same stop,
  fairness and deadline checks; use the unchanged selector when none is ready.

This is a concrete candidate for reducing generic selector overhead under ready
work, NOT a promise that try-receive is cheaper on every target. It adds ready
probes before an empty blocking wait, so idle/wakeup cost can regress. Report both
workloads. Ready probes are bounded in number, not necessarily in execution time:
Crossbeam's reserved-slot dependency remains. There is no custom notifier,
check-then-sleep protocol, ISR channel call, actor framework or busy retry loop.

## Workloads and useful-work accounting

All cases execute inside the existing `event-path-perf` binary. Parameters are
`CASE COUNT 0 bytes16`, with COUNT 1..256 and **zero excluded warmups**. The initial
comparison is cold-inclusive and finite; it must not be pooled with P2/P0 warmed
latencies. A separate process or fresh boot is required per case.

| Case suffix | Behavior |
| --- | --- |
| `backlog` | COUNT rounds of eight important + eight ordinary events, all prefilled before draining. Exactly the same checked work; no rejection or discard is allowed. `drain_ticks` sums whole drain windows including handler and observation overhead. |
| `burst-1`, `burst-4` | One/four real providers each attempt COUNT events. Each performs at most sixteen consecutive attempts before a blocking sleep. |
| `periodic-1`, `periodic-4` | One/four real providers use per-provider absolute one-millisecond release plans; actual scheduling may make releases late. This is not externally driven or calibrated offered load. |

Prefix each suffix with `owner-select-` or `owner-ready-`. For concurrent cases,
every ninth attempted event is important, the rest ordinary. The two queues have
separate capacity. Each provider retains Full's original value; Closed is an
unexpected failure. The owner validates the 16-byte payload and does fixed bounded
handler work. After all producers join, the coordinator sends dedicated stop.
Every admitted event must be handled or explicitly discarded at stop:

```
attempted = admitted + rejected
admitted = handled + discarded
```

The raw record of every attempt includes admission stamps and, only for handled
events, handler-entry/completion stamps. Reports preserve rejected/discarded work,
so a fast result obtained by dropping more work is visible. Only the deterministic
backlog is a matched-work dispatch comparison; concurrent admission counts can
differ. Do not compare throughput without those counts. No percentile subtraction.

The ready-first backlog control must execute zero selector API calls versus
sixteen per round for baseline, with identical output and work count. These are
**API invocation counts**, not wakeups or context switches. CPU time, actual
switches, allocation/stack placement, power and idle utilization are unmeasured.
The total elapsed observation includes setup/cleanup; use named drain windows to
study ready-owner dispatch. All output formatting and cross-checking follow joins.

## Progress and lifecycle

On NuttX, all channel data actors use equal effective FIFO priority 180 on the
P2 single-core profile. A finite coordinator uses 220 and the existing watchdog
240. The source obeys P1's reservation-order constraints (no external code while
holding an incomplete reservation), but these fixtures do **not** establish full
production progress or method bounds. P1's unequal-priority failures stay failed.
Finite provider bursts and owner queues eventually block; fairness among queues
alone must never be mistaken for fairness among FIFO threads.

Stop is checked before each dispatch, including retained selections. A stop
arriving just after that check can leave one event in flight. Stop tests cover
full queues, but this initial timing campaign posts stop only after producers
join. It is not yet a worst-case stop-under-live-load or deadline-budget test.
Partial startup releases every spawned worker; cancellation and the independent
watchdog bound failure containment. Original P2 transport/capture gates are unchanged.

## Commands and evidence

```sh
cargo +1.90.0 test --locked -p nxrs-event-path-perf
CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_LTO=false \
  cargo +1.90.0 build --locked --release -p nxrs-event-path-perf
python3 tests/event-path-perf/owner_campaign.py \
  --binary target/release/event-path-perf --samples 128 --repeats 2 \
  --output target/owner-campaign
```

The host campaign uses one hash-checked executable, reverses case order on the
second round, validates actual transcripts, and retains raw output plus separate
reports. It never promotes host results to physical timing or progress acceptance.
Existing P2 ARM/ESP builds compile the same owner code. This increment does not
add owner-specific board flashing/capture orchestration; P2's existing capture
parser intentionally rejects owner cases rather than misclassifying their schema.
An on-board command is `event_path_perf owner-ready-periodic-4 128 0 bytes16`;
new target execution and matched workload evidence remain required.

## Acceptance and next decision

First require functional equality: stop priority, capacity isolation, fairness,
deadline recheck/retention, closure and payload accounting. Then compare matched
backlog dispatch and live provider distributions on one executable. Keep the
baseline strategy available. Adopt ready-first only if a measured benefit in the
intended workload survives the idle/wakeup tradeoff and P1 contract review.
Independent absolute calibration remains separate; no new P0 timer investigation
is a prerequisite for these functional and relative-cost experiments.
