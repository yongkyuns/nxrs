# Participant admission for the selected owner

This is the P1-CLOSE contract handed from
[#255](https://github.com/yongkyuns/nxrs/pull/255) to the actual selected-owner
work in [#265](https://github.com/yongkyuns/nxrs/issues/265), under the
[#252 closeout plan](https://github.com/yongkyuns/nxrs/pull/252#issuecomment-6633035786).
It assembles the existing source audits into participant, operation and lifetime
requirements. **Admission of a concrete owner configuration remains pending**
until its complete census and evidence satisfy those requirements. Host owner
work can proceed independently; target and production acceptance must meet this
contract and the remaining physical gates.

## Available conclusions

| Evidence | Supported conclusion | Acceptance limit |
| --- | --- | --- |
| [Original Crossbeam witnesses](SOURCE-AUDIT.md) | A higher-priority caller can stall on a lower-priority participant's reserved, unpublished or unreclaimed slot. The three released-slot controls distinguish the hazard. | The tested unequal-priority contracts fail. A reproduced stall is a valid diagnosis; priority recovery is never a no-repair pass. |
| [Equal-FIFO source audit and six cases](FIFO-SCHEDULING-AUDIT.md) | On the pinned single-core scheduler, under all restrictions below, the existing reservation holder releases its slot before a newly ready equal-priority peer enters the conflicting operation. | This excludes the audited incomplete-slot overlap. Full method return, application progress and shutdown need additional arguments. |
| [Seven std primitive cases](STD-TRANSFER-AUDIT.md) | A prewarmed, one-attempt mutex transfer preserves ownership on contention; separate cases establish protected publication, predicate waiting and sticky close observations. | These are single-item primitive contracts. They establish neither a selected owner nor a general `std::sync::mpsc` guarantee. |

The audits retain the exact Crossbeam, Rust std, NuttX and configuration pins.
Use those maps when reviewing a consumer's compiled sources. Neither candidate
contract establishes a numerical method-return bound, WCET, physical timing or
general production transport qualification. P2's FIFO 120/180 characterization
does not approve that scheduling relationship for this owner.

## Complete participant census

Create one census entry for **each actual execution context** that can touch an
endpoint, including synchronous callbacks and exceptional paths. Record every
endpoint instance, its clones or shared references, who may receive it, and who
finally destroys it. One producer/owner pair or a count of Rust handles cannot
stand in for this census.

For each entry retain: native task/thread identity; endpoint and queue class;
operations and reachable callbacks; first and last possible use; native policy
and effective-priority evidence; locks held across calls; bounded runnable work
and the justified blocking point; shutdown obligations; and review disposition.
Record unknowns as pending admission. Do not silently change priorities to make
a witness or workload pass.

The following rows map the existing
[`OwnerLoop`](../nuttx-std/wifi/src/owner.rs),
[`main` composition](../nuttx-std/wifi/src/main.rs) and
[`Wi-Fi worker`](../../hal/wifi/src/worker.rs). A changed consumer revision must
update this map before relying on it.

| Participant / phase | Operations to account for | Required admission evidence |
| --- | --- | --- |
| Constructor and startup coordinator | Create the positive-capacity channels, distribute or clone endpoints, initialize native objects, start providers. | Prove the scheduling invariant before the first possible channel callback. The provider can call its sink before `wifi::open` returns; a readback taken only when `OwnerLoop::run` starts is too late. |
| Every command caller | `CommandSender::try_submit`, important-channel `try_send`, rejected-command retention/retry, clone/drop. | Enumerate every clone's actual callers and lifetime, including synchronous owner callbacks and handles moved to other tasks. |
| Every provider/event caller | `WifiEventSink::try_emit` on important and ordinary channels, delivery retry, sink clone/drop. | Admit every provider worker and any foreign/native callback context. Both endpoints and their final notification belong in the census. Prove task context; ISR or asynchronous signal use is excluded. |
| Every stop caller | `StopHandle::request_stop`, stop-channel `try_send`, repeated requests and final drop. | Capacity one isolates stop storage from data queues. It gives no scheduling exemption. The non-`Clone` handle can still be shared by reference or `Arc`; identify every caller. |
| Owner task | Receive-only selection, deadline checks, controller reconciliation, dispatch, retained `Ready` values and receiver retirement. | Admit the actual caller of `run`, its effective scheduling, all reachable callbacks and work between real scheduling opportunities. |
| Observer, clock and provider calls made by the owner | Report handling, clock reads, command/stop admission, controller/provider operations. | These execute synchronously in the owner context unless a separate worker is explicitly involved. The current observer sends commands on `Ready` and requests stop on completion/rejection. Inventory additional locks, channel calls and dependencies introduced by generic closures. |
| Readiness or status observers | Channel readiness queries, `Select::ready`, watch/unwatch, inspection followed by receive. | Account for every context that enters channel machinery. A high-priority observer is external interference only if it performs no channel operation; bound that interference for any latency claim. |
| Endpoint and payload lifecycle callers | Late clone/drop, last-sender or receiver destruction, disconnect, `never()` replacement, clearing retained values, rollback and TLS teardown. | Keep concurrent shutdown in the admitted lifetime and provide its separate lock/destructor argument, or prove quiescence before moving it outside the active phase. The six FIFO cases keep endpoints live. |
| Recovery and reclamation callers | Stop after observer failure, provider `Drop`, final sink calls, cleanup retries, explicit `finish`/join. | Show that final callbacks and endpoint destruction complete and workers are reclaimed. Recovery priority changes, watchdog expiry or incomplete joins cannot establish no-repair success. |
| Scheduling controls and other runnable work | Policy/priority changes, inheritance/protection, scheduler operations, higher-priority tasks and interrupts. | Explain how the effective-priority and single-core invariant survives every relevant lock and phase. A priority change or additional actor requires review before use. |

Any additional forwarding channel brings its own participants and contract.
For example, report-forwarding helpers in the
[owner's host tests](../nuttx-std/wifi/src/tests.rs) use a std synchronous channel;
the Crossbeam argument does not admit that channel.

## Crossbeam operation and scheduling admission

The source argument is conditional on all admitted participants executing on
**one core at the same effective `SCHED_FIFO` priority** while their channel
operations can overlap. Successful array reservations must reach publication
or reclamation without a wait, yield, user callback or unfinished token escaping
that interval. The pinned scheduler preserves the preempted holder ahead of a
subsequently readied equal-priority peer.

Retain native policy/effective-priority readback for every participant, the
resolved kernel configuration and evidence that the invariant holds throughout
the admitted lifetime. Startup readback alone cannot justify later inheritance,
protection, reprioritization or policy changes. The witness actors use FIFO 180,
observer 220 and watchdog 240; those are the tested fixture values, not an
automatically approved assignment for another workload. A global round-robin
interval does not change an explicitly FIFO task into RR, but an RR participant
is outside this contract.

| Operation or condition | Disposition under the existing audit |
| --- | --- |
| Positive-capacity bounded-array `try_send` / `try_recv` | Their successful reservation-to-release intervals are eligible under the complete FIFO restrictions. Payload moves complete before release; user `Clone`/`Drop` is not called in that interval. The `try_*` name alone does not bound the entire method. |
| Immediate receive-only `select_biased!` | Eligible for the same narrow ordering argument: wakers are unregistered before acceptance, the normal cached context returns without blocking, and the selected receive completes before the arm body. The native timeout case establishes ordering, not a deadline bound. |
| Direct blocking `send` / `recv` | The source audit describes immediate completion after successful reservation. Waiting, wakeup, retries and whole-method progress require separate admission; the six FIFO cases do not qualify blocking producer ingress. |
| `Select::ready`, readiness snapshots, or a readiness/receive retry loop | Readiness is not proof of payload publication or reclaimed capacity. Watch/unwatch and notification have internal synchronization. A ready-first dispatch candidate needs its own operation review and #265 acceptance evidence. |
| Manual/retained `SelectedOperation`; selected-send expressions | Outside this receive-only contract unless separately audited. A token can retain a reservation across arbitrary code; a selected-send expression is evaluated after reservation. Direct-send arguments are evaluated before method entry. |
| Notification, allocation, cold initialization and destruction | `SyncWaker` and pthread `unpark` can take mutexes after array slot release. These paths, payload destruction and endpoint shutdown need their own lifetime/blocking discipline before claiming a complete-call bound. |
| Zero-capacity, unbounded, std MPSC or another queue implementation | Different protocols or source paths. They receive no implicit admission from the positive-capacity Crossbeam array argument. |
| SMP, RR, unequal/changing effective priorities, priority inheritance/protection that changes the invariant | Outside this scheduling contract. Supply a separately reviewed argument and evidence for the proposed configuration. Base priority or an API name is insufficient. |
| ISR/asynchronous signal reentry, TLS teardown or reentrant context fallback | Outside the audited call path. Same-thread asynchronous reentry can overlap its own unfinished reservation even at the same numeric priority. |

The actual owner stores an already completed receive in `pending_ready` as an
owned `Ready` value. Retaining that value across observer failure is different
from retaining a `SelectedOperation`; the reservation has already completed.
Its later destruction still belongs in the lifetime review.

## Separate std primitive admission

The [std audit](STD-TRANSFER-AUDIT.md) covers the following conditions together:

- Prewarm the same mutex and condition variable objects before releasing their
  users. Cold `OnceBox` access may allocate or initialize native objects.
- Make exactly one `try_lock` attempt. On Busy, retain the original input or
  protected value and count zero admitted/delivered items. Repeated high-priority
  polling needs an independent work and blocking argument.
- Move complete owned values under the guard. Pair a condition variable with
  one mutex; check the item/close predicate under that mutex in a loop, publish
  sticky close under the same guard, notify after release, and process outside
  the guard.
- Establish actual native waiting where required. A pre-wait marker or timeout
  does not prove that a task blocked or bound subsequent mutex reacquisition.
- Retain the new image's mutex attributes and kernel configuration. The retained
  baselines use default `PRIO_NONE` with inheritance disabled; Rust `Mutex`
  itself does not establish a priority-inheritance contract.

These conditions do not automatically describe the current Wi-Fi provider.
Its `submit` holds the pending mutex while calling `Thread::unpark`; stop and
`Drop` also unpark; its final event callback runs while holding the pending
mutex. Review those dependencies separately. In particular, the std condition
variable recipe above does not certify that provider's parker or entire method.
Blocking mutexes, intermediate-priority interference and a proposed inheritance
configuration also need separate evidence.

## Owner liveness and lifetime obligations

Every participant needs a bounded runnable burst and a justified blocking point
so another equal-priority task can run. A continuously ready owner, unbounded
handler, repeated failed admission or retry loop can starve peers even when
incomplete-slot overlap is excluded. Calling `park`, `park_timeout`, a wait or
selection is insufficient evidence by itself: a token or ready input can make
it return immediately. Establish the actual scheduling opportunity and its
dependencies. The owner's eight-important-event turn changes queue order; it
does not schedule another task.

Stop precedence applies at selection points. Controller reconciliation,
deadlines, callbacks and a previously received `pending_ready` value can precede
the next selection. Document the resulting shutdown behavior rather than infer
preemptive cancellation or a stop deadline. Any latency bound additionally needs
bounded higher-priority/interrupt interference and the notification, handler
and lock costs omitted by the slot-order argument. Leave missing product budgets
unknown until the consumer supplies them.

The lifetime review must cover startup, steady state, overload, stop, errors and
reclamation. In the current worker, `Stopped` is stored **before** its final
`Status::Stopped` sink call; callback completion, guard/sink destruction and
thread exit still follow. `Stopped` alone is insufficient proof of quiescence.
The owner can replace receivers with `never()` and destroy a retained payload
while shutdown is in progress. Track those operations explicitly. The existing
composition calls `controller.finish()` to join after driving shutdown; that
structure does not establish a bounded join.

Track D's workload evidence must reconcile offered, attempted, admitted,
rejected and handled work, with ownership retained on rejection. It must exercise
stop admission with both data queues full, declared queue fairness, deadlines
under continuous readiness, closure/re-arm, cancellation, retries and bounded
reclamation. Pin any baseline/ready-first alternative and compare matched work;
the source argument alone does not select a faster dispatch policy.

## Consumer review packet and remaining gate

Attach this small review packet to the concrete #265 candidate; no new witness
or runtime configuration API is required to publish the contract.

| Required item | Evidence and decision to retain |
| --- | --- |
| Source and image identity | Consumer revision, compiler/std/Crossbeam/kernel identities, resolved configuration, build receipt and executed image; identify every departure from the linked audit pins. |
| Complete participant census | The entries above expanded to actual tasks, endpoint instances/clones, callbacks, locks, lifecycle operations and additional transports; disposition of every unknown or excluded operation. |
| Scheduling and work budgets | Native readbacks for all participants from first possible use, effective-priority invariants, finite work-to-block arguments, interference assumptions and any unqualified timing requirement. |
| Lifetime and owner semantics | Startup admission, overload/ownership accounting, stop and error handling, final callback/destructor completion, re-arm and explicit worker reclamation. |
| Scoped acceptance | State separately which source conditions hold, which target observations pass without repair, which physical gates remain, and what production/timing claims are still unqualified. |

The canonical hardware handoff is the
[fixed 19-case campaign](../channel-progress-physical/README.md) and its
[frozen manifest](../channel-progress-physical/candidate-manifest.json), using
the eight already published builds in artifact
[13323366472](https://github.com/yongkyuns/nxrs/actions/runs/41827567717/artifacts/13323366472).
Those clean-image fresh boots remain pending. The
[older 15-case physical campaign](results/2026-11-04-esp32s3/README.md) and
ARM/QEMU results retain their original scopes; neither fills the new campaign.
Operator labels and build receipts do not authenticate physical execution, and
`physical_execution_verified` remains false under the existing offline replay.
Calibration is unnecessary for the ordering/progress question; absolute-time
qualification remains separate. Publishing this contract closes the software
handoff, while P1's physical closure and the consumer's actual admission remain
open.
