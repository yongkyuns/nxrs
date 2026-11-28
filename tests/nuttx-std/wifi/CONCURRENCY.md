# Wi-Fi scan owner concurrency contract

This contract describes the existing scan fixture's
[`OwnerLoop`](src/owner.rs), its [`main`](src/main.rs), and the ordinary
[`wifi::Controller`](../../../hal/wifi/src/controller.rs). It gives maintainers
a concrete ownership and progress inventory for [issue #443](https://github.com/yongkyuns/nxrs/issues/443).
It is source evidence and a qualification plan, not a product timing guarantee.
The station/UDP path in `station.rs` has a separate loop and is outside this
contract.

## Execution and ownership

| Context | Owns and executes | Waiting and scheduling |
| --- | --- | --- |
| Scan entry thread | `OwnerLoop`, `Controller`, operation deadline, and `Observer`; changes controller state through exclusive mutable borrows | One logical blocking `select_ready` over the three inboxes and the current deadline. The [documented NuttX build](README.md#nuttx-builds) requests entry priority 100 and an 8 KiB stack. |
| Wi-Fi provider worker | Native driver, scan parsing, delivery retries, and interface lease | One existing `std::thread`; [NuttX acquisition](../../../hal/wifi/src/platform/nuttx/mod.rs) requests an 8 KiB stack. The [worker builder](../../../hal/wifi/src/worker/mod.rs) sets no scheduling policy or priority. Record the effective target values before making a priority claim. |
| Command and event callers | Owned inputs until successful admission | `try_submit`, `try_emit`, and `request_stop` execute on their callers. The real scan observer submits commands and stop on the entry thread; the provider calls the event sink on its worker. No relay thread or inline controller callback is introduced. |
| Native radio/OS execution | NuttX/vendor radio work, interrupts, and internal resources | Separate from the Rust worker. Their priorities, locks, and interference are not established by this fixture's Rust topology. No ISR calls the owner endpoints. |

The observer runs synchronously on the owner. The supplied implementation only
counts scan results, records completion, and attempts command/stop admission;
it performs no console I/O, device wait, or join. Another observer must preserve
that contract. The callback type does not prove bounded execution.

The synthetic [selected-owner qualification](../../owner-qualification/README.md)
imports this exact loop and controller, but supplies a fake provider and source
threads. Its owner, coordinator, source workers, and endpoint callers all run at
FIFO priority 180 on the admitted single-core target. It provisions 16 KiB
stacks and disables priority inheritance. Those settings describe that witness,
not the real scan worker or an application scheduling policy.

## Bounded storage and delivery

The scan entry calls `channels(4, 4)` before opening the provider.

| Storage | Capacity in this composition | Contents and overflow |
| --- | --- | --- |
| Stop inbox | 1 token | Lifecycle-owner-only, non-clonable handle. A duplicate returns `AlreadyPending`; receiver retirement returns `OwnerClosed`. Data cannot consume this slot. |
| Important inbox | 4 messages | Commands, provider status, and link changes. Full/closed admission returns the original command or event. |
| Ordinary inbox | 4 events | Scan results and their completion share FIFO order so completion cannot overtake results from the provider. Full/closed admission returns the event. |
| Selected input | At most 1 `pending_ready` | A selected input survives an error during the pre-dispatch deadline/report work; it is not silently lost on resume. It is additional to queue capacity. |
| Provider request state | 1 request/active operation | `pending` and `busy` serialize native work. A busy or contended submission returns the original command with `Busy`. |
| Provider rejected delivery | At most 1 retained event | The provider retries a full destination after a 5 ms park; stop or closed delivery ends that attempt. There is no finite delivery-time bound while the consumer remains full. |
| Native scan buffer | 4,096 bytes | Allocated by [`Native::open`](../../../hal/wifi/src/platform/nuttx/native.rs) and reused. Native result truncation remains explicit. |

The synthetic cases deliberately use other capacities: 2/2 for independent
inputs, 1/1 for overload/recovery, and 16/4 for fairness/deadline ordering.
Queue capacity excludes active handler values, producer-owned inputs, runtime
bookkeeping, stacks, and native radio memory. These source capacities do not
establish a measured heap peak, minimum stack, or allocation-free dispatch path.

The current owner transport is pinned Crossbeam 0.5.17. Its `try_` operations
avoid waiting for queue capacity; that is not a guarantee of bounded internal
contention on a fixed-priority target. The NuttX std bounded-channel patch does
not change Crossbeam's implementation. Transport follow-up is tracked separately
in [issue #446](https://github.com/yongkyuns/nxrs/issues/446); this contract adds no
second mailbox or selector.

## Wait dependencies and shared resources

The owner depends on the provider for native completion, but waits through its
normal selection point. Provider submission never waits for a native operation.
The worker can depend on the owner draining an event queue; stop remains
independent of that queue and interrupts its retry park. Neither side may turn
that relationship into a synchronous request/reply cycle.

| Resource or operation | Current dependency and boundary |
| --- | --- |
| Controller state | Exclusive to the owner; no shared controller mutex. |
| Provider `pending` mutex | Owner submission uses `try_lock`; worker takes blocking locks for request extraction, busy-state changes, and final cleanup. Native scan/connect work runs outside it. Submission currently calls `unpark` while holding this mutex; final terminal notification also occurs under it. Include these paths in contention analysis. |
| Provider status and first fault | Atomic snapshots read by `reconcile`; a terminal fault survives rejected notification and later stop. Snapshot visibility alone is not a wakeup. |
| Native initialization mutex | Setup-only initialization in the NuttX acquisition path. Its protected native work is not part of the normal owner dispatch loop. |
| Native calls and delivery | The worker owns scan/connection waits. Full-queue retry delays later native work and idle link observation. Kernel, driver, allocator, and channel locks remain part of the target's dependency inventory. |
| Worker join | `Controller::finish` after the owner has left dispatch. Joining while a producer still needs queue drainage would create a wait cycle. |

There is no measured upper bound for these lock holds or scheduler delays in
this contract. Enabling kernel priority inheritance alone does not establish
the protocol of each pthread/std mutex, nor does mutex inheritance donate
priority across an event queue.

## Fairness and deadline semantics

Stop has first selection preference. After eight consecutive important inputs,
the owner gives a ready ordinary input preference over important input; handling
an ordinary input resets the streak. This bounds dispatch preference when both
classes are ready. It does not preempt a running handler or guarantee elapsed
latency if an observer, transport operation, or scheduler does not make progress.
Closed important/ordinary receivers are replaced by `never()` to avoid a
disconnection spin. Closing the stop endpoint requests stop.

Each loop checks `advance` and `reconcile` before waiting and checks both again
after selection, before dispatching the selected input. A stream of ready input
therefore cannot suppress deadline checks merely by preventing a receive
timeout. The supplied clock must be monotonic; the controller rejects a
backwards clock.

The scan command requests a 10,000 ms operation timeout. That interval starts
when the owner calls `Controller::submit`, not when the command first enters
the important inbox. It excludes command queue residence and is not an
end-to-end latency bound. A timeout reports the outcome once; the native
operation keeps its slot until completion or stop, preventing overlapping retry.
An eventual completion then releases the slot without reporting a second
outcome. The synthetic deadline case checks this ordering with logical time;
it does not measure deadline response latency.

## Shutdown and failure boundaries

Create all inboxes and install the event sink before provider acquisition.
Normal completion requests stop through its reserved inbox. The owner continues
dispatch and reconciles sticky provider state while shutdown progresses. On
`Stopped`, it retires all three receivers and any selected input; queued values
are discarded explicitly by that retirement, not processed after shutdown.
Old endpoints then reject with closure.

An observer or stop error returns ownership to the caller; it is not successful
shutdown. The real entry requests stop, resumes the same loop with a draining
observer, preserves the original failure, and calls `finish` after the loop
returns. `finish` joins the worker; `Drop` only signals abandonment/stop and
does not hide a blocking join.

The current worker drops native resources, publishes the `Stopped` snapshot,
then attempts its final status notification before returning. Callers must keep
sink-owned resources alive through `finish`; the snapshot alone is not evidence
that this final Rust callback has returned. This ordering needs reconciliation
with the stronger callback-quiescence wording in the public status contract.
The synthetic qualification joins its source workers and explicitly does not
use a status label as proof that real SDK callbacks are finished.

Stop admission does not preempt an in-flight native ioctl. The NuttX scan-read
retry has a six-second deadline between calls, which cannot interrupt a blocking
call. If native cleanup fails, a live owner can retry stop; an abandoned worker
retains its resources and retries with backoff from 100 ms, capped at 30 seconds.
That caps each delay, not total teardown time. Permanent cleanup failure is not
reported as successful stop.

## Evidence and remaining admission work

From the repository root, the [owner tests](src/tests.rs) and HAL worker controls
exercise the contract with synthetic sources:

```sh
cargo +1.90.0 test --locked -p nxrs-wifi
cargo +1.90.0 test --locked -p nxrs-std-wifi --lib
```

The [target qualification](../../owner-qualification/README.md) adds observed
native wait states, selected schedules, full queues, finite fairness, failure
recovery, endpoint closure, and counted joins. Its watchdog and gate deadlines
bound a failed qualification invocation; they are not product response budgets.
The [radio procedure](README.md) separately owns device behavior and historical
physical evidence.

Before adopting this composition for a deadline-sensitive product, supply its
actual input rates/bursts, maximum handler and observer work, acceptable data
age, effective priorities and mutex protocols, interference, and stop/recovery
budget. Measure queue occupancy, allocation paths, stack use, blocking duration,
and deadline response with that workload. None of those missing values should
be filled from the synthetic fixture's capacities or priorities. Mixed-priority
Crossbeam progress and the final-notification boundary remain separate open
checks; the present contract does not mark them qualified.
