# Rust std channel performance on NuttX

The goal is low, predictable event-delivery cost through ordinary Rust `std`
threads and bounded channels. Performance near PX5 is a **qualification target**;
there is no matched physical result establishing it. This work adds research and
the isolated [std channel fixture](../tests/std-channel-perf), without changing
the production transport or replacing the RTOS.

## Keep the evidence cohorts separate

| Evidence | What it establishes | Boundary |
| --- | --- | --- |
| [October ESP32-S3 service comparison](rtos-comparison.md) | Physical native-API C/Rust/Zephyr/Embassy behavior, capacity, timing and footprint for its recorded workload | Its NuttX Rust uses native threads/queues, not `std::thread` or std channels |
| [Earlier std/NuttX qualification](nuttx-std.md) and [RTOS fixture](rtos-benchmarking.md) | Recorded ABI, lifecycle, implementation comparisons and QEMU behavior | Host/QEMU time cannot establish physical MCU latency; a ready message does not prove a parked receiver |
| [November 2 physical std-channel cohort](../tests/std-channel-perf/results/2026-11-02-esp32s3/README.md) | 3,000 independently verified blocked receives on ESP32-S3 N16R8; actual-std reserved-slot stall and published control | Counter data is physical; time conversion remains uncalibrated, with no cross-RTOS ranking |
| [Beningo report extraction](../tests/rtos-bench/report-reference.json) | Historical NuttX 12.13.0 results on an 80 MHz STM32L475, with documented source/configuration gaps | Percentages are normalized independently within each test row; they are neither absolute operations/s nor an overall RTOS score |
| Source audit below | Mechanisms and concrete optimization candidates | Source inspection establishes neither their fraction of runtime nor a measured speedup |
| `tests/std-channel-perf` | Reproducible cases and comparison contracts for the std path | Each claimed result still needs its own executable, environment and raw observations |

The report's message row records NuttX at 21% of its category best; its
synchronization row records 82.1%. These different denominators cannot be combined
into one slowdown. The reported failed cooperative check remains invalid. The
unresolved `-Ofast`/`-O2` discrepancy and missing author adapter remain in the
extraction. Do not infer absolute cycles or clock-scale a different MCU to match.

The pinned [Thread-Metric message loop][tm-message] sends and receives one
16-byte message on the same worker. Its successful steady state returns the
queue to empty each iteration. This hot path does not exercise a blocked receiver
or deep backlog: direct receiver handoff, ready-list scans caused by waking that
receiver, and queue-depth-dependent priority insertion cannot explain that row.
Measure those mechanisms with separate cases.

## Follow the actual std path

The current [nxrs messaging facade](../shared/messaging/src/channel.rs) delegates
its bounded inbox to `std::sync::mpsc::sync_channel`. The move into shared
infrastructure does not change the channel or its underlying parking mechanism.

For the audited Rust 1.90-era implementation, positive-capacity
`std::sync::mpsc::sync_channel` uses the [array channel][std-array]. Slot reservation
and publication use atomics; this is not a wrapper around `mq_send`/`mq_receive`.
When it must wait, its [channel context][std-context] parks the current thread.
NuttX selects the Unix [pthread parker][std-parker-selection], whose
[implementation][std-parker] uses an atomic notification token plus a pthread
mutex and condition variable. The existing nxrs SDK patch initializes that mutex
correctly; preserve it and the ABI gates.

An uncontended operation need not enter the kernel. Notification can also complete
through the parker's atomic fast path when the receiver is not parked. A genuine
blocked transfer can involve waiter registration, mutex synchronization, condition
signaling, NuttX semaphore wait/post, scheduling and context restoration. The
pthread parker releases its lock before signaling to avoid immediately making the
awakened receiver wait for that same lock again. Preserve its lost-wakeup and
acquire/release ordering when changing anything below it.

Fixed-capacity message storage does not establish allocation-free waiting. The
channel context is created lazily and cached per thread; waiter bookkeeping can
also allocate. Record setup, first use on fresh threads, steady ready operations,
repeated blocking/timeouts and teardown independently. Rust allocator counts omit
some libc/OS allocation and stack storage. See the
[allocation contract](concurrency-event-communication.md#allocation-policy-std-and-crossbeam-use-the-same-acceptance-test).

Rust 1.90.0 source links describe the audited algorithm. Each target build must
retain the actual SDK/compiler identity, selected Rust library source and private
patch ledger; the pinned ESP and nightly SDKs are not interchangeable with a
current desktop compiler. [Patch provenance](upstream-patchsets.md#rust-llvm-and-std)
is authoritative for the image being measured.

## NuttX costs: separate common mechanisms from mqueue-specific work

This audit uses nxrs's NuttX 13.1.0 pin
`2b5c0de5e933281c209f954a63b99cf10c132346`, separately from the report's 12.13.0.

| Mechanism at that pin | Cost and qualification question | Effect on std channels |
| --- | --- | --- |
| [Message allocation and send][nx-send] | Pop a global preallocated block when available; otherwise allocate from heap. `mq_maxmsg` limits depth but [does not reserve that many blocks][nx-alloc]. Allocation and payload copy precede the fullness check. | None directly; std owns different message storage |
| [Priority-ordered message insertion][nx-send] | Linear scan, including an occupied equal-priority queue; no tail shortcut in this source. Empty-queue insertion has no backlog scan. | None directly |
| [Buffered receive][nx-receive] | Remove queue entry, copy into caller, return/free block. Receive payload copy and free occur outside the queue critical section. | None directly |
| Public descriptor and cancellation paths | Public mq calls perform descriptor/reference handling and configured cancellation bookkeeping. Flat builds avoid user/kernel protection transitions for ordinary calls; disabled cancellation code compiles out. | Only paths actually called by std matter |
| [POSIX semaphore wait][nx-sem-wait] and [post][nx-sem-post] | Already have atomic uncontended fast paths. Optimize measured contended/wakeup costs without removing synchronization semantics. | Shared with blocking pthread machinery |
| [Pthread condition wait][nx-condwait] | Release mutex ownership, wait on the condition semaphore, then restore the mutex; count actual park/wake transitions and contention. | Directly relevant to the NuttX parker |
| [Priority-sorted scheduler lists][nx-sched] | Insertion cost grows with relevant ready/waiter population; choosing the already-sorted head is constant time. | Relevant when a std participant blocks or becomes runnable |

Therefore neither “POSIX always traps” nor “every mq send mallocs” explains the
observed comparisons. Pool pressure, payload size, occupied depth, build mode,
enabled features and whether another thread wakes determine the executed work.
`poll()` setup/refcount/teardown can matter to native wait-any service fixtures;
std's internal receiver wait does not use that descriptor-based selection path.

## Fixed-priority progress is a separate acceptance gate

The array algorithm reserves a slot by advancing the tail before writing the
value and publishing its stamp. A receiver that observes a reservation whose
stamp is not yet published retries internally. This can occur inside a method
named `try_recv`; the name does not establish a bounded instruction count.

On one core under strict fixed-priority scheduling, an independently awakened
higher-priority consumer can preempt a lower-priority producer between reservation
and publication. If the consumer spins waiting for that producer, the producer
cannot finish. This is a source-derived scheduling hazard to qualify, not a
claim that ordinary channel ping-pong has reproduced it. Normal channel
notification occurs after publication and may never expose this interleaving.
Same-priority round-robin or multicore runs can also conceal it.

The [retained actual-std witness](../tests/std-channel-perf/results/2026-11-02-qemu/README.md)
now reproduces this stall on NuttX/QEMU for the recorded single-core FIFO case;
the published-slot control completes without recovery. The same evidence cohort
verifies 64 blocked-receiver samples after a SysTick timestamp correction,
preserving the earlier rejected clock readings. These observations use a frozen
`c03ac02`-based worktree before integration with later main changes; the archive
records that boundary and the SysTick patch's later renumbering. Physical-target
builds in that archive remain build-only, and emulator timing establishes no MCU
speed ranking.

The separate [physical N16R8 cohort](../tests/std-channel-perf/results/2026-11-02-esp32s3/README.md)
reproduces the actual-std stall and successful published control on hardware,
with all workers joined. It also verifies 3,000 blocked receives across three
fresh boots using integrated CI firmware. Timing qualification remains withheld:
the cycle counter passed its plausibility probe, but independent CPU-clock
calibration was unavailable. The archive retains the initial host-banner rejection
and the target's unresolved ROM image-digest warning.

The progress witness must use an independent higher-priority wake source and
exercise the reserve-before-publish interval under controlled single-core FIFO
priorities. A synthetic protocol witness demonstrates the mechanism only; an
actual std claim additionally needs the pinned library path and target schedule.
An external watchdog bounds failed qualification. A timeout on another thread
of lower priority is insufficient if the test starves it. A host process timeout
is harness recovery, not a physical target watchdog.

Do not use std or Crossbeam channels from an ISR. Do not solve this risk by
silently weakening memory ordering, adding unbounded busy waits, or assuming
`yield` schedules lower-priority work. Keep production priority assignments within
qualified cases; use a separately qualified bounded transfer protocol when a
strict fixed-priority boundary requires it.

## Optimization sequence

1. **Measure the production abstraction.** Establish std ready-path throughput,
   verified parked-receiver latency, blocked-sender release, round-trip latency,
   overload and deadline behavior. Match payload/capacity and report cold and
   steady-state allocation separately. Keep the old C/native cohort intact.
2. **Find shared blocked-path cost.** Attribute waiter registration, pthread
   mutex/condition operations, semaphore wake, ready-list insertion and context
   switching with a separate diagnostic image. Confirm operation/wakeup counts;
   time the release image without per-event logging.
3. **Optimize that common path first.** Candidate work includes avoiding redundant
   lock/scheduler entry when nobody wakes, reducing duplicate bookkeeping,
   improving measured ready-list insertion, and target-specific context/code
   placement improvements. Existing semaphore fast paths already cover some
   uncontended cases. Every change must preserve wake ordering, timeout races,
   priority inheritance where enabled, cancellation and shutdown progress.
4. **Reduce unnecessary application wakeups.** Keep processing within its owner,
   use owned bounded buffers for large data and bounded draining where latency
   allows. Batch only with an explicit latency/fairness budget. Preserve isolated
   important/stop capacity and one logical wait point.
5. **Optimize mqueue users when evidence calls for it.** Pool sizing, a safe
   equal-priority tail append, per-queue reservation or direct delivery may help
   native mq callers. Benchmark depth and contention, preserve priority/FIFO
   ordering, and account for reserved RAM. These changes do not accelerate std's
   independent array storage. A kernel rewrite is not the starting task.

Replacing the NuttX parker with a new semaphore/futex-style backend is a later
candidate only if attribution justifies it. It needs a complete token/wait/timeout
and memory-ordering proof plus target regression evidence; a faster microbenchmark
does not establish a correct parker.

## What the other RTOSes actually demonstrate

Native [ThreadX 6.5.1 queue send][tx-send] uses a fixed-size ring with no
per-message allocation and copies directly into a waiting receiver's buffer.
Its native suspended queue waiters are FIFO unless explicitly prioritized; its
[ready scheduler][tx-ready] uses per-priority lists and bitmaps. The same version's
[POSIX send adapter][tx-posix-send] allocates/copies payload and searches priorities;
[receive][tx-posix-receive] also allocates temporary metadata, arranges messages and
releases storage. Native queue speed cannot establish POSIX or Rust std speed.

[Zephyr 4.5 `k_msgq`][z-msgq] also uses a preallocated fixed-size ring and direct
receiver delivery. Its [POSIX adapter][z-posix] ignores message priority and wraps
that fixed-size queue. A fixed-size/equal-priority benchmark compares a useful
common subset; it does not measure equivalent complete POSIX message semantics.

The [October 28 Zephyr 4.5 announcement][z-release] reports nearly 14% higher
message throughput and over 15% higher synchronization throughput than 4.4 on an
STM32F412. The Beningo study used 4.4.1, so keep these cohorts separate. Concrete
4.5 changes provide useful examples:

- [Update ring metadata before memcpy][z-ring] to avoid compiler aliasing reloads:
  the commit reports +7.95% Thread-Metric message throughput on AZ3166/M4/96 MHz,
  with much smaller gains for large payloads in a separate test.
- [Favor the successful receive branch][z-branch]: the commit reports +2.18% on
  that M4, with an increased cost for an empty receive.
- [Remove redundant Cortex-M IRQ-lock/unlock ISBs][z-isb], retaining the M7 erratum
  workaround and the required switch-point barrier: the commit reports semaphore
  give with no waiter falling from 74 to 68 cycles. NuttX's audited ARMv7-M
  BASEPRI helpers already lack those redundant barriers; ESP32-S3 uses different
  Xtensa synchronization instructions. This is not a transplantable NuttX patch.
- [Skip unused notification accounting][z-notify] in POSIX mq sends. The older
  [4.1 no-wakeup fast return][z-fast-return] similarly avoided needless scheduling.

These reported gains are upstream measurements, not nxrs results. Improve code
generated for the target while preserving the protocol; do not sum percentages
from different commits, baselines or configurations.

## Hardware acceptance and reproducibility

Freeze a numeric latency/throughput/memory budget before the qualification run.
“Near PX5” additionally requires a declared tolerance and matched physical
platform, workload, semantics and timing boundaries. Otherwise retain it as a
goal without assigning a pass or a numerical gap.

| Required case | Boundary and evidence |
| --- | --- |
| Hot send/receive | Same-thread ready path; record send+receive pair throughput, occupied depth, bytes and full/empty outcomes |
| Blocked receiver | Independently prove the receiver parked; time publication/wakeup through receipt/handler entry, explicitly stating each timestamp boundary |
| Blocked sender | Fill capacity, prove sender blocked, release one slot, verify one completion and retained ownership on rejection |
| Ping-pong | Whole round trip with exact sequences/digests; never divide by two and call it a one-way context switch |
| Contention and priority | Multiple producers, same/different priorities, one core and selected SMP cases; independently awakened high consumer, overload, timeout and shutdown |
| Service load | Sustained/burst traffic plus long CPU work; report publication lateness, queue residence, handler time, loss and deadline misses separately |

Retain board/CPU revision, actual frequency, timer/counter resolution, cache and
flash/PSRAM placement, core affinity/count, actual scheduling policy/priorities,
FIFO versus round-robin/timeslice, compiler and full flags, LTO/panic settings,
Rust/std/libc/dependency identities, kernel configuration and patch ledger,
payload/capacity/producer count, warmup, sampling boundaries and executable hashes.
Use matched fresh boots and repeated counterbalanced runs; reject configuration
mismatches and invalid delivery before comparing speed.

Report p50, p99, maximum observed latency and throughput with sample count and
duration. Label histogram upper bounds as bounds, and observed maxima as observed;
neither is WCET. Also report setup/steady-state/retained allocation, stack
reservation and high-water, linked flash/static RAM and pool capacity. Measure
idle/load power or energy per delivered event on hardware when claiming an energy
benefit. Unmeasured power remains unmeasured. No spinning-for-throughput change
passes solely because its mean latency improves.

## Pinned mechanism sources

[tm-message]: https://github.com/eclipse-threadx/threadx/blob/730b61874bc5cf40768987605ae5187fde0fa1e2/utility/benchmarks/thread_metric/tm_message_processing_test.c
[std-array]: https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sync/mpmc/array.rs
[std-context]: https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sync/mpmc/context.rs
[std-parker-selection]: https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sys/sync/thread_parking/mod.rs
[std-parker]: https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sys/sync/thread_parking/pthread.rs
[nx-send]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/mqueue/mq_send.c
[nx-alloc]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/mqueue/mq_msgqalloc.c
[nx-receive]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/mqueue/mq_receive.c
[nx-sem-wait]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/semaphore/sem_wait.c
[nx-sem-post]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/semaphore/sem_post.c
[nx-condwait]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/libs/libc/pthread/pthread_condwait.c
[nx-sched]: https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/sched/sched/sched.h
[tx-send]: https://github.com/eclipse-threadx/threadx/blob/v6.5.1.202602a_rel/common/src/tx_queue_send.c
[tx-ready]: https://github.com/eclipse-threadx/threadx/blob/v6.5.1.202602a_rel/common/src/tx_thread_system_resume.c
[tx-posix-send]: https://github.com/eclipse-threadx/threadx/blob/v6.5.1.202602a_rel/utility/rtos_compatibility_layers/posix/px_mq_send.c
[tx-posix-receive]: https://github.com/eclipse-threadx/threadx/blob/v6.5.1.202602a_rel/utility/rtos_compatibility_layers/posix/px_mq_receive.c
[z-msgq]: https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0/kernel/msg_q.c
[z-posix]: https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0/subsys/portability/posix/options/mqueue.c
[z-release]: https://zephyrproject.org/zephyr-rtos-4-5-now-available-two-new-architectures-multimedia-pipelines-sboms-and-more/
[z-ring]: https://github.com/zephyrproject-rtos/zephyr/commit/f32e042fa5d22b7731d3a173a9e92ba5c12893e7
[z-branch]: https://github.com/zephyrproject-rtos/zephyr/commit/a73dce81a7cf0beec6ff96e1db49f2931c750aa6
[z-isb]: https://github.com/zephyrproject-rtos/zephyr/commit/a811b0f1cd51f34a0f86c4a0efb60ae69addbf05
[z-notify]: https://github.com/zephyrproject-rtos/zephyr/commit/ac8a6f976ea3f95ec2baa54916c903d1ed20003b
[z-fast-return]: https://github.com/zephyrproject-rtos/zephyr/commit/48939cb44b615516e04f57d7706f37ce0afc6b99
