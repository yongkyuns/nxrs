# Standard primitive transfer qualification

This follow-up to the [Crossbeam source witnesses](SOURCE-AUDIT.md) evaluates
the existing single-item `Mutex` / `Condvar` transfer pattern. The repository
already uses that pattern in
[`service/detection/src/worker.rs`](../../service/detection/src/worker.rs):
the caller tries the mutex once, complete owned values move under its guard,
and the worker waits on a condition variable and performs its work after
releasing the guard. These witnesses are test code, not a new production queue
or a replacement parker.

The seven named cases run from one `std-mutex-suite` image with clean Crossbeam
sources and the established common Rust std preparation. They add no source
hooks to std. The original six Crossbeam cases retain their distinct meanings,
including their failed no-repair reservation contracts.

## Exact source basis

The ARM SDK is `nightly-2025-09-15`, whose compiler revision is
[`52618eb338609df44978b0ca4451ab7941fd1c7a`](https://github.com/rust-lang/rust/tree/52618eb338609df44978b0ca4451ab7941fd1c7a).
The ESP SDK is `esp-1.90.0.0`, whose compiler revision is
[`abf50ae2e46066e67e29fe856f9764c23aa9a3ca`](https://github.com/esp-rs/rust/tree/abf50ae2e46066e67e29fe856f9764c23aa9a3ca).
Both Unix target dispatches select pthread mutexes and condition variables for
NuttX. Their dispatch syntax and public API files differ, so they have separate
audited source maps rather than assuming one SDK's bytes fit the other.

`mutex_analyze.py::validate_std_sources(library, sdk)` requires the following
nine exact SHA-256 entries in the complete std library ledger. `build.py`
performs this check after common private SDK preparation and before compiling
the suite. `analyze.py` repeats it while validating the suite's retained source
receipts. Unknown SDKs, missing entries, changed bytes and a mixture of the two
SDK maps are rejected even if an outer ledger has been resealed consistently.
The gate applies only to `std-mutex-suite`; it does not alter the old channel
source-preparation contract.

These five implementation paths are byte-identical in the two audited SDKs.
Paths in the tables are relative to `library/`, as in the retained ledger:

| Rust std source | Enforced SHA-256 for both SDKs |
| --- | --- |
| [`std/src/sys/sync/mutex/pthread.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/mutex/pthread.rs) | `1aa80435780abe34a365179635e92fb5935894fb9b4f615eb45b336958c28035` |
| [`std/src/sys/pal/unix/sync/mutex.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/pal/unix/sync/mutex.rs) | `1df3c112f84ccfa7ff048aa88a604dc6029b405f59e0aa96d35381b589aa9b17` |
| [`std/src/sys/sync/condvar/pthread.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/condvar/pthread.rs) | `515dac39964e931abbcf56fa963254adab34cbd7a76916ccd03fd4d863acfd4f` |
| [`std/src/sys/pal/unix/sync/condvar.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/pal/unix/sync/condvar.rs) | `4b21456c5b928b032904232b0aab6dde205c3edbd8562c522a5274e23cd796dc` |
| [`std/src/sys/sync/once_box.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/once_box.rs) | `395ab1241ab919abedd060b790527a605763b5a94260e51e702ba2df5852ad6c` |

The SDK-specific dispatch and public API pins are:

| Rust std source | ARM `nightly-2025-09-15` SHA-256 | ESP `esp-1.90.0.0` SHA-256 |
| --- | --- | --- |
| [`std/src/sys/sync/mutex/mod.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/mutex/mod.rs) | `d7220a27794733fb47c515a991ecd2123b5ba4823d28674eb7324dfcfd66d567` | `ea2feae381d8e1be81a8ca4d8c13c5c3560e082d10fc9835f98dbede186bf5f6` |
| [`std/src/sys/sync/condvar/mod.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/condvar/mod.rs) | `34b21d5ee225213988cf772a4ceae69d20ecdb5102d259a2644575b041185497` | `207002bea900f8851de508c9aa4a2d1ac3b08a3464155d5d66291cd014824d51` |
| [`std/src/sync/poison/mutex.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sync/poison/mutex.rs) | `16ef24b3381c0a785889baee19744ade4346be56a42ea9778034e881af56900f` | `d8ac9dc942771d5767f3398d73eaa8c73d9f31c75a5b71f674b59f1921719e24` |
| [`std/src/sync/poison/condvar.rs`](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sync/poison/condvar.rs) | `b8202047cd8cf8bf5ff03336eefc4c03a0e546d18ea60f59deaca28be1be37e4` | `cb3bfbca24e41ff5f16157bc7206f1a2362513e5a65fb80cfb2cdcd6c8e8f305` |

The hashes were calculated from the exact upstream revisions above and checked
independently against the prepared SDK ledgers in the retained
[ARM artifact](https://github.com/yongkyuns/nxrs/actions/runs/41664483288/artifacts/13245827598)
and [ESP build artifact](https://github.com/yongkyuns/nxrs/actions/runs/41664483288/artifacts/13243774175).
All nine paths match every retained build of the corresponding SDK, with the
complete ledger and artifact hashes verified. That ESP comparison establishes
source identity only; its images were not executed on a physical board.
Common nxrs ABI preparation changes none of these nine files. The
[pthread parker](https://github.com/rust-lang/rust/blob/52618eb338609df44978b0ca4451ab7941fd1c7a/library/std/src/sys/sync/thread_parking/pthread.rs)
is checked separately by `tests/nuttx-std/prepare-std.py`, which requires its
original Git blob `14bc793c15de254594778b59ad045006b58fd845` and applies the
established NuttX initialization correction. That prepared file must not be
confused with its unpatched upstream bytes.

Actual firmware still requires its retained compiler identity, complete std
source ledger, resolved configuration and image hashes. These nine critical
source pins supplement that binding; this document cannot substitute for it
or for an independent target execution.

The kernel is pinned to NuttX
[`2b5c0de5e933281c209f954a63b99cf10c132346`](https://github.com/apache/nuttx/tree/2b5c0de5e933281c209f954a63b99cf10c132346).
The relevant native paths are:

| NuttX source | Git blob |
| --- | --- |
| [`libs/libc/pthread/pthread_mutexattr_init.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_mutexattr_init.c) | `5e65b09414b73f6dfa2697fae26e9e1441be462d` |
| [`libs/libc/pthread/pthread_mutex_init.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_mutex_init.c) | `9c6529ce87a469f159cee03b4bef716d2b2dc008` |
| [`libs/libc/pthread/pthread_mutex_trylock.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_mutex_trylock.c) | `c027f31923c70f918b2d78b2dd36a410439d92e5` |
| [`libs/libc/pthread/pthread_mutex.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_mutex.c) | `b040ec698a7b89f5b6b9e3b27514468203583ca8` |
| [`include/nuttx/mutex.h`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/include/nuttx/mutex.h) | `bd870c071d08a00cb431657721db0c162dac9650` |
| [`sched/semaphore/sem_trywait.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/semaphore/sem_trywait.c) | `710d0a6461ec80ce1366d86fa65b7616812c6f64` |
| [`libs/libc/pthread/pthread_condwait.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_condwait.c) | `840daa36ff105e225cfc3ab79a15ca84416a49a9` |
| [`libs/libc/pthread/pthread_condsignal.c`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_condsignal.c) | `b344c1e038e34b9a1371287ed65dd652b6240a9a` |

## Prewarming and a single attempt

The pthread-backed std mutex uses `OnceBox`. Even `try_lock` calls `get`, which
can allocate a pinned native mutex and initialize it on first use. The std
condition variable also uses `OnceBox`; a first notification can initialize
its native object. Therefore the witnesses initialize both objects before
releasing their workers. A cold `try_lock` name is not an allocation-free
operation guarantee.

After initialization, std's native `try_lock` calls `pthread_mutex_trylock`.
For a valid live mutex held by another thread, NuttX's path attempts the native
mutex and reports contention without waiting for the holder to release it.
The fixture verifies that case on the target: a priority-180 caller returns
from exactly one attempt while the priority-120 holder still owns the guard.
The caller retains an unaccepted input, or leaves the protected result intact,
instead of reporting contention as a delivered item. The released-guard cases
are matching successful-transfer controls.

This is a bounded-operation contract, not an instruction to retry in a loop.
Repeated high-priority calls without an independently justified blocking or
work budget could still starve the worker. The source audit and finite target
cases do not establish a numerical worst-case execution time.

## Condition predicates and notification

The condition variable is always paired with one mutex. The worker checks a
predicate under that mutex and waits in a loop when no item and no close state
is present. NuttX increments the condition's wait count before releasing the
mutex, waits on its semaphore, and reacquires the mutex before returning.
A complete item or the sticky close state is published under the same mutex.
Notification follows guard release; processing happens outside the guard.

| Case | Required observation |
| --- | --- |
| `mutex-admit-held` | One contended admission attempt returns before guard release; the original input remains owned and admission/delivery counts remain zero. |
| `mutex-admit-released` | Guard release precedes the attempt; one complete input is admitted and delivered. |
| `mutex-take-held` | One contended retrieval attempt returns before guard release; the protected complete value remains present and no delivery is claimed. |
| `mutex-take-released` | Guard release precedes the attempt; the complete value transfers once. |
| `condvar-wait-publish` | A native condition wait is observed before publication and notification; one complete value is delivered. |
| `condvar-publish-before-wait` | Publication precedes the consumer's predicate check; the consumer delivers the value with zero condition-wait calls. |
| `condvar-close-wait` | A native condition wait is observed before close and notification; the consumer sees the sticky close state and exits without a fabricated delivery. |

A notification is a wake hint, not stored ownership or the condition itself.
The predicate is what makes publication before a wait safe. A marker immediately
before `Condvar::wait` alone is insufficient evidence that the thread blocked;
the wait cases additionally require the native TCB wait observation. A condition
wait must reacquire its mutex before returning, so its timeout does not bound
that reacquisition delay.

## Scheduling limits

All native cases require actual FIFO policy readback, a single execution core,
caller priority 180, holder priority 120, observer priority 220 and watchdog
priority 240. Passing means all required operations and joins complete without
observer priority repair. Recovery or an expired watchdog cannot become a pass
by setting a success field.

Std initializes native mutex attributes and sets their type; it does not itself
request a priority-inheritance protocol. At the pinned NuttX revision,
`pthread_mutexattr_init` selects inheritance only when the relevant kernel
support and `CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT` are enabled. The retained
ARM and ESP baseline configurations use default `PRIO_NONE` with
`CONFIG_PRIORITY_INHERITANCE` disabled. Every new image must retain its own
resolved configuration rather than infer that property from an earlier run.

A blocking mutex acquisition allows its holder to run in the two-worker case,
but that does not prove a bound under intervening runnable priorities. The
single-attempt contention contract does not depend on priority donation.
Qualifying natural priority inheritance or a blocking transfer under a stated
interference budget would be a separate experiment, with effective priorities
and restoration observed directly.

## Parker and selection are separate contracts

The exact std pthread parker performs an atomic notification exchange. When
the target was already `PARKED`, `unpark` acquires and releases the target's
parker mutex before signalling its condition variable. That handshake prevents
a lost wake, but it means `unpark` is not an unconditional nonblocking wake
operation. These witnesses use the existing standard primitives and do not
replace that mechanism.

Likewise, Crossbeam `Select::ready` is not a publication certificate. In the
[pinned array source](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/flavors/array.rs),
`is_empty` compares head and tail, and the tail advances before payload
publication. A following `try_recv` can still encounter the reserved slot.
The [readiness implementation](https://github.com/crossbeam-rs/crossbeam/blob/2920c984290229ab4e0ca0452ef09e48a82063f3/crossbeam-channel/src/select.rs)
also uses internal mutex and parking machinery for watch/unwatch and wakeup.
Adding a readiness or retry loop does not qualify the three-class owner.

The `std-transfer-progress-v1` results therefore cover only these named
single-item primitive cases. A valid unbound transcript cannot establish the
executed image; source/build binding and external execution provenance remain
required. All production-transport, timing and hard-real-time qualification
claims stay false. Important-event retention, overload, fairness, the selected
three-class owner and physical replication remain separate qualification work.
