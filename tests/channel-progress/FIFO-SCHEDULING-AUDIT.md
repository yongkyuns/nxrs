# Ordered FIFO channel progress

This experiment evaluates an explicit scheduling restriction for the existing
bounded Crossbeam transport: all channel participants run on one core at the
same effective `SCHED_FIFO` priority, and a successful array reservation is
completed before its holder can block, yield or execute user code. A finite
higher-priority observer makes a conflicting peer ready while the holder is
preempted. The required observation is that the holder resumes and releases
the actual slot before that peer enters the conflicting operation.

The argument concerns exclusion of incomplete-slot overlap. It does not make
Crossbeam generally wait-free, bound application work or qualify a production
transport. The original unequal-priority reservation witnesses remain valid
demonstrations of failed progress contracts; their recovery cannot become a
progress pass. The standard primitive suite remains a separate qualification.
No queue, parker, production owner or provider is replaced by this experiment.

## Exact source and image binding

Crossbeam uses the authenticated `crossbeam-channel 0.5.17` and
`crossbeam-utils 0.8.23` archives described in [SOURCE-AUDIT.md](SOURCE-AUDIT.md).
Both identify upstream commit
[`2920c984290229ab4e0ca0452ef09e48a82063f3`](https://github.com/crossbeam-rs/crossbeam/tree/2920c984290229ab4e0ca0452ef09e48a82063f3).
`prepare-crossbeam.py` authenticates the original complete archives and
reconstructs the selected complete source trees. The new `fifo-ordered` recipe
has image hook stage 5. It preserves the existing clean and four single-boundary
recipes, including their serialized receipts, byte-for-byte for identical input
archives and output paths.

The shared runtime build mode is `fifo-channel-suite`; its records use
`fifo-channel-progress-v1` and name contract `equal-fifo-run-to-block-v1`. Each
run selects one named case from that one image. Actual channel participants run
at FIFO priority 180, the finite observer at 220 and the watchdog at 240.

The kernel is NuttX
[`2b5c0de5e933281c209f954a63b99cf10c132346`](https://github.com/apache/nuttx/tree/2b5c0de5e933281c209f954a63b99cf10c132346).
`fifo_sources.py` requires the following SHA-256 values for actual source files.
No current nxrs kernel patch changes these paths.

| Kernel source | Enforced SHA-256 |
| --- | --- |
| [`sched/sched/sched.h`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched.h) | `57b4cc2d873a57f9a2410598d953b7ab0bf0cb21eb11d019d29b29407885b21a` |
| [`sched/sched/sched_addreadytorun.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_addreadytorun.c) | `9969e9395eb1cb91127229bd6b65e2f12b7078f41b420067bf0ae2d82f579fa0` |
| [`sched/sched/sched_removereadytorun.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_removereadytorun.c) | `03bb1aa9b17867c524c729440343ba6ded69978f40baaee137fa1de5d5aa7d0a` |
| [`sched/sched/sched_mergepending.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_mergepending.c) | `dbe356d1068dd0ee3e69f3cf782da6619ccda1e0cdcfd61dafa7e576e5983e02` |
| [`sched/sched/sched_unlock.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_unlock.c) | `d2aabeb580c2762fc8b859b3bb3d45a8265b5b274830b055b059bab7eeb5b07e` |
| [`sched/sched/sched_processtick.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_processtick.c) | `e2f60009edfac6998d905a16ea37a01e5fc3058cfe0881c25f5af5811bec03d4` |
| [`sched/sched/sched_processtickless.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched_processtickless.c) | `8b147e08d18974fd56e508227262b2f6f8d5b724a1abd13d706e6ec0c1af550e` |
| [`sched/semaphore/sem_post.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/semaphore/sem_post.c) | `ade456e5fd87fb2472e8c84fb3a528125b854bc3c7cf9e86ce8660d94727d7ee` |
| [`sched/semaphore/sem_wait.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/semaphore/sem_wait.c) | `a4f4bb20bf81460ba025f38758f9500a7dec45e6c1bd02a58b4f257ea78f84e6` |
| [`sched/semaphore/sem_waitirq.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/semaphore/sem_waitirq.c) | `f51a033e67f4e2d65102ddff934e400841aea0d1360e646603b24aa6f2f96657` |

`verify_kernel` reads and hashes the actual selected files. `prepare_kernel`
authenticates every input before creating a new retained directory and writes
independent regular copies under their original relative paths. The builder
includes that map in its identity and rechecks the compiled kernel directory
after the firmware build. Replay requires the same pinned revision, exact map,
and complete retained copy inventory. Missing or changed bytes, extra entries,
symlinks, hardlinks and a consistently resealed but unaudited map are rejected.
These copies supplement the existing compiler, configuration, source, ABI and
firmware binding; copies alone are not proof of execution.

The FIFO suite also requires the nine SDK-specific std mutex/condition-variable
pins in [STD-TRANSFER-AUDIT.md](STD-TRANSFER-AUDIT.md). Its two additional parker
pins use paths relative to the complete retained `library/` ledger:

| Prepared std source | ARM `nightly-2025-09-15` | ESP `esp-1.90.0.0` |
| --- | --- | --- |
| `std/src/sys/sync/thread_parking/mod.rs` | `4aea2e6fd87d71bd6572a232af6c56bcc5615baacfcc64e2b27d88839d767328` | `64bfa00edd937fafb302a6655434718acecb736dbb3996d73904d4ef978291c5` |
| `std/src/sys/sync/thread_parking/pthread.rs` | `8779b59ac72e52511db47fa44c4f1bdbc9840046e9e4c98c6a63f6acb5b920f5` | `8779b59ac72e52511db47fa44c4f1bdbc9840046e9e4c98c6a63f6acb5b920f5` |

The [ARM dispatch](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/thread_parking/mod.rs)
and [ESP dispatch](https://github.com/esp-rs/rust/blob/abf50ae2e46066e67e29fe856f9764c23aa9a3ca/library/std/src/sys/sync/thread_parking/mod.rs)
both select the Unix pthread parker for NuttX. The common prepared parker hash
includes the established NuttX mutex-initialization correction from
`tests/nuttx-std/prepare-std.py`; it is not the unpatched upstream hash. Both
upstream pthread files have Git blob `14bc793c15de254594778b59ad045006b58fd845`.
The additional pins were independently reconstructed from those exact upstream
bytes and the common preparation function, then compared with retained ARM and
ESP source ledgers. Unknown SDKs and mixed dispatch maps are rejected.

## Why equal FIFO priority can exclude the dangerous overlap

In the pinned non-SMP scheduler, the running task remains the head of the ready
list. A higher-priority arrival is inserted before it, leaving the preempted
task in its existing position. `nxsched_add_prioritized` scans past existing
equal priorities, so a subsequently readied equal-priority peer goes behind the
original holder. Removing the observer when it blocks selects the existing next
task. Pending-list merging preserves the same relationship. These are the
specific source properties exercised by the witness, rather than an assumption
based only on a `SCHED_FIFO` label.

The tick, tickless and scheduler-unlock paths apply round-robin processing only
when the task policy flags identify `SCHED_RR`. A global nonzero round-robin
interval does not time-slice an explicitly FIFO worker. The fixture therefore
requires actual native policy and effective-priority readback, the resolved
single-core configuration, and native ready/wait observations.

In Crossbeam's [array source](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/flavors/array.rs),
successful `start_send` advances the tail and prepares a token. Direct `send`
and `try_send` immediately call `write`, which moves the payload and publishes
the release stamp. The symmetric receive path advances the head, moves the
payload out and publishes the reclaimed stamp. None of these successful
reservation-to-release intervals allocates, calls user code, waits or explicitly
yields. Payload movement invokes neither `Clone` nor `Drop`. Backoff and native
waiting on unsuccessful attempts occur before that attempt owns a reservation.

The receive-only selection path preserves this property. In
[`run_select`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/select.rs),
all wakers are unregistered before `accept` attempts a receive reservation. The
normal cached [`Context::with`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/context.rs)
return restores the thread-local context without a blocking call. The
[`select_biased!` receive completion](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/select_macro.rs)
immediately calls `SelectedOperation::recv` before evaluating the arm body.
The existing Wi-Fi owner uses that receive-only form.

Starting from complete slots, a participant can consequently finish its
successful interval before an equal-priority peer can run. Finite higher-priority
interference may delay that interval, but does not reorder a newly ready peer
ahead of the preempted holder. This argument depends on all the restrictions
below and on the exact source/configuration binding. It provides no numerical
worst-case execution bound for atomic retries or external interference.

## Source markers and the forced native observation

The isolated FIFO recipe uses one existing hook ABI, with these values:

| Marker | Exact location |
| --- | --- |
| `0x301` | Successful send reservation, after tail CAS and before token preparation |
| `0x302` | Immediately after the publication release-store, before receiver notification |
| `0x303` | Successful receive reservation, after head CAS and before token preparation |
| `0x304` | Immediately after the reclamation release-store, before sender notification |
| `0x100 + registered_count` | Before `Context::wait_until` in `run_select`, using the actual registered count |
| `0x200` | Immediately after that `Context::wait_until` returns, before unregistering wakers |

All calls are enabled only for the NuttX witness. The runtime selects the holder
and trigger boundary; one image supports three reserved/released pairs. A hook
may record atomics and post the prestarted native observer. It must never wait
on a gate, sleep, yield, reprioritize a participant or invoke another channel.
The existing native gates use `SEM_PRIO_NONE`, avoiding priority donation.

| Named reserved/released pair | Required reserved-case order |
| --- | --- |
| `fifo-try-recv-reserved` / `fifo-try-recv-published` | Send reservation; observer sees both workers ready; holder resumes; actual publication; peer enters `try_recv` |
| `fifo-select-reserved` / `fifo-select-published` | Three actual registrations and native wait; send reservation; independent native timeout makes owner ready; holder resumes and publishes; selector wait returns |
| `fifo-try-send-reserved` / `fifo-try-send-reclaimed` | Full ring; receive reservation; observer sees both workers ready; holder resumes; actual reclamation; peer enters `try_send` |

The published/reclaimed controls start the observer after the corresponding
release store. A reserved case must show that store had not happened at its
observation checkpoint. Successful delivery without the required marker order
cannot establish this contract. All cases require completed joins, a bounded
independently runnable watchdog and no recovery. Source markers are not native
state observations: the selector cases require both the actual three-registration
marker and the target TCB wait evidence.

## A native timeout does not require the select default arm

The observer can hold the CPU for a bounded interval while the real selector
deadline expires. The NuttX timed semaphore wake makes the selector ready through
`nxsched_add_readytorun`. It does not execute the selector's Rust code. The
observer must remain runnable until that ready state is observed; sleeping or
yielding while waiting would let the holder continue before the intended wake.
It then blocks, allowing the original holder to resume according to the audited
FIFO order.

`Context::wait_until` changes a still-waiting selection to `Aborted` only when
the owner itself executes its deadline check. A producer that resumes first can
publish and select the operation while the timed-out owner is ready but has not
run. `run_select` also retries operations before returning its timeout result.
Therefore this case requires the native ready observation and publication before
the wait-return marker; it does not require the default arm to execute. The
timeout is an independent wake mechanism, not a bound on channel spinning.

The pinned [pthread timed condition wait](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_condclockwait.c)
releases its mutex before waiting and reacquires it before returning. A peer
that has become ready but has not run has not performed that reacquisition.
Its timeout does not bound the later mutex reacquisition.

## Admission restrictions and remaining limits

- All task-context senders, receivers, selectors, readiness observers and
  endpoint-lifecycle participants must belong to the admitted scheduling
  contract. Checking one producer and one owner does not admit every clone of
  the command, event or stop endpoint.
- Single-core FIFO and equal effective priorities must remain true during the
  operation. SMP, RR, priority changes and priority-inheritance/protection
  effects require a separate argument. The witness applies no priority repair.
- No incomplete reservation may survive a wait, yield or user callback. Manual
  `SelectedOperation` use can hold a reservation across arbitrary code. A
  selected-send macro evaluates its message expression after reservation, so
  that expression is outside this receive-only selection contract unless
  separately constrained. Direct send arguments are evaluated before the
  channel method begins. Thread-local teardown or reentrant context fallbacks
  are outside the normal live-worker argument.
- ISRs and asynchronous signal handlers must not call the channels. A signal
  handler can reenter the same thread while its interrupted frame owns a slot;
  identical numeric priority does not prevent that overlap. Native callbacks
  need explicit task-context and scheduling admission. This follows the
  repository's existing ISR/channel boundary, not a new provider API.
- Only positive-capacity bounded-array channels are covered. Zero-capacity and
  unbounded channel flavors use other protocols. Readiness alone is not a
  publication certificate, since tail/head reservation changes readiness
  before the corresponding stamp is released.
- Every participant needs a bounded runnable burst and a justified blocking
  point. Repeated `try_send` failures, continually ready I/O or unbounded owner
  callbacks can starve equal-priority peers without any incomplete-slot hazard.
  The owner's eight-important-event fairness turn orders queue dispatch; it
  does not by itself schedule another task. Higher-priority tasks and interrupts
  also need bounded interference before any end-to-end latency claim.
- Crossbeam's [`SyncWaker`](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/waker.rs)
  takes a std mutex, and the prepared pthread parker may take the target parker
  mutex during `unpark`. Those calls follow the slot release in the array path,
  but can delay method completion. Allocation, first-use initialization,
  notification locks, payload destruction and endpoint shutdown require their
  own lifetime and blocking discipline. Neither the API name `try_send` nor
  this source proof guarantees an unconditional bounded method return.

The named native cases exercise the specific ordering claim. Their artifacts
must retain the actual source/configuration/image binding and execution
provenance. QEMU observations remain emulated execution; ESP build receipts
alone establish no physical run. A passing result does not certify the complete
three-class provider workload, production transport, physical timing or a hard
real-time bound. Functional owner, overload, retention, fairness and shutdown
work remains with its existing fixture and ownership.
