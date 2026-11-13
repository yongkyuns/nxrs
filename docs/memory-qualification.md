# Memory qualification of demos and acquisition

This is the **Rust allocation/lifecycle** slice, on host test builds. It is not a
new app, actor runtime, allocator policy, heap HAL, or RTOS benchmark. The separate
RTOS benchmarking work is untouched.

## Run

```sh
cargo test --locked -p nxrs-allocation-probe
cargo test --locked -p nxrs-std-demo --features probe --test memory_qualification
cargo test --locked -p nxrs-ao-stress --features probe --test memory_qualification
cargo test --locked -p nxrs-allocation-probe --features probe --test acquisition_lifecycle
# Repeat the three measurement commands with --release for optimized builds.
```

Each measurement command runs a dedicated test executable with `harness = false`.
The allocation-probe accounting unit tests use the ordinary test harness. Even with
one selected test and output capture disabled, libtest has a concurrent harness
thread whose allocations can contaminate process-global counts. The dedicated
process runs the same app sources and measurement assertions without libtest.
Only the `probe` feature opts into instrumentation. This is diagnostic
qualification, not normal `cargo run`.

`tests/allocation-probe` is a non-default workspace member and **dev-dependency**
of the two apps. Probe modules and their global-allocator registrations require
both `test` and `probe`. Normal binaries, including normal builds with that
feature selected, retain their usual allocator and do not depend on the probe.
Application `forbid(unsafe_code)` and all architecture boundary rules remain intact.
The unsafe allocator implementation and its direct-pointer accounting tests are
confined to the test-support crate. The acquisition witness is an integration
test in that same support crate, requires its `probe` feature, and uses
`nxrs-acquisition` only as a Unix dev-dependency. It exercises the normal
worker implementation without enabling acquisition's optional tracing feature.
No HAL manifest or production source needs allocation instrumentation. There are
no new third-party dependencies.

## Measured paths

`std-demo` measures six cases: dynamic Vec growth, filling a reservation then
growing beyond it, the existing private BoundedVec recipe, inline array reuse,
reserved HashMap keys and reusable String formatting. Setup, first use, warmed
operation and final reclamation are recorded separately where applicable.

Bounded Vec, reserved HashMap and String are initialized once and reused for 1,000
cycles. Those first-use and steady windows must have zero observed allocator calls
(including deallocation); final live bytes and block count must return exactly to
the pre-case baseline. Dynamic/growing Vec cases must instead demonstrate observed
reallocation. Static capacity assertions alone cannot satisfy these probes.

`ao-stress` separates three questions inside one isolated qualification test:

1. The calling test thread performs two timed receives on empty, newly created
   inboxes, dropping each inbox afterward. Rust 1.90's channel context uses a
   cached thread-local Arc; the calling thread remains alive after an owner is
   joined. Its first-use retention is measured explicitly, then the second
   operation must show no further net growth. There is no arbitrary byte allowance,
   subtraction of unexplained retention, or warm-until-the-check-passes loop.
2. A scoped owner moves one 256-byte Vec payload through the existing production
   Sender/Inbox APIs. Queue/thread setup and 16 warm-up exchanges precede
   1,024 measured round trips. The warmed window must show zero allocator calls;
   dropping the buffer, closing queues and joining the owner must return tracked
   live bytes/blocks to the pre-owner baseline. This is a focused transport and
   owned-buffer probe, **not a measurement of every full stress handler's steady
   path or blocked wakeup timing**. The caller's cached context stays in baseline;
   a terminated owner's context must not cause additional retained memory.
3. The actual unchanged `stress::run` executes all four scenarios with one-slot
   inboxes. Each scenario has exactly two warm-up lifecycles, followed by eight
   checked lifecycles. The returned Report is also dropped before every baseline
   comparison. First-run retention is reported separately; all eight warmed exits
   must return to the same live-byte and live-block baseline. Each run still
   executes the production accounting/order/progress/join checks.

All three measurement executables deliberately inject an allocation and retain its buffer
across a snapshot. The zero-call and reclaimed-memory assertions must both reject
it; releasing it must return to baseline. The observer itself separately tests
allocation/zeroing failure, failed realloc ownership, grow/shrink accounting,
alignment, byte preservation, concurrent owners and sticky counter errors.

## Acquisition lifetime witness

[`tests/allocation-probe/tests/acquisition_lifecycle.rs`](../tests/allocation-probe/tests/acquisition_lifecycle.rs)
exercises the real `acquisition::Worker` and `readiness::Cancel`, with one worker
and two simultaneously live workers. Each cardinality has one initial lifecycle
and exactly eight repetitions. There is no acquisition warm-up, adaptive retry,
byte allowance or unexplained baseline subtraction. Every lifecycle must return
tracked requested bytes and blocks to the same pre-construction baseline.

The fixture uses safe `std::io::pipe` descriptor ownership on Unix. Each worker
has one actual cancellation pipe and a separate test data pipe. Static atomics
coordinate the observation points; no fixture channel, heap-allocated barrier or
allocating sink is introduced. The first wait phase completes a real one-byte
data read and delivers one event before acknowledging completion. This proves
the operation completed; it does not assert that the scheduler actually parked
the thread in the kernel. Cancellation then uses the production `Cancel::wake`
path and a read from its retained reader before the worker exits.

| Report phase | Observation and requirement |
| --- | --- |
| `construction` | Create pipes, cancellation state and workers; all workers reach the initial gate. Observe positive Rust allocation activity. |
| `first-wait` | Release the initial gate, complete one data read and sink delivery per worker, then hold them at the second gate. Observe zero allocator calls, including deallocation. |
| `stop-join` | Release the second gate, request cancellation and explicitly join every worker. Verify completion and retain the session objects. Positive requested bytes and blocks remain above the lifecycle baseline. |
| `session-drop` | Drop the joined sessions and fixture data writers. Observe deallocation without new allocation/reallocation; return both requested bytes and block count exactly to baseline. |
| `reclaimed` | Report the complete pre-construction to post-drop window independently of the adjacent phase windows. |

All five phases are retained for all 18 lifecycles: **90 JSON records**. Snapshots
use fixed-size stack storage and printing happens after measurement. The Python
transcript checker independently requires the exact inventory, adjacent byte and
block endpoints, successful block accounting, the sum of phase counters and the
full-window result. A missing drop phase, a zero-work transcript, continued
retention or a repeated cycle with a shifted baseline fails qualification.

`join()` is a worker completion boundary. It consumes the join handle while the
session still owns `Arc<State>` and the cancellation object; the latter owns
`Arc<File>` and both original cancellation descriptors. These remaining session
allocations are expected ownership, not an arbitrary permitted leak. Dropping
the joined session is the next reclamation boundary. Dropping a running session
instead requests cancellation without joining; this witness does not reinterpret
that asynchronous path as quiescent cleanup.

### What remains outside canonical static storage

The following inventory comes from the actual IMU/GNSS acquisition paths. The
host worker witness measures only the shared Rust allocation part; it does not
instantiate the NuttX sensor backends.

| Storage or resource | Owner and release point | Accounting scope |
| --- | --- | --- |
| `Arc<State>` | Worker plus running closure; the joined session retains its reference until drop | Rust global allocator |
| `Arc<File>` for cancellation | Cancel plus worker reader clone; the original reference survives join until session drop | Rust global allocator |
| Cancellation descriptors and native pipe state | Cancel owns the pair; the worker also retains its reader until exit | OS resources, outside Rust requested-byte counters |
| Thread name, closure and Rust thread bookkeeping | Created by `thread::Builder`; worker completion and join retire thread-owned state | Rust allocations crossing the registered allocator are counted |
| OS thread state and stack | Native thread implementation | Excluded from these Rust counters; requested stack size is not measured usage |
| IMU driver, applied settings, readiness and lease/fault state | Typed `Mutex<Option<Device<S>>>` and fields in the canonical instance | Inline static slot storage, already included in linked object size |
| GNSS receiver/decoder and saved UART state | Typed canonical instance; the 64-byte saved-port buffer is inline | Inline static slot storage, not a separate heap allocation |
| IMU bus/GPIO and GNSS UART resources | Device-specific retained guard keeps the slot through terminal delivery, sink destruction and cleanup | Native descriptor/backend costs need separate target measurement |
| Transaction and decode scratch | Stack-local buffers in the drivers, including BMI270's 60-byte FIFO read and UBX's 128-byte read buffers | Examples of stack use, not a summed stack bound |
| Queues and sink-owned buffers | Chosen by the consumer; Worker does not create a queue | None in this witness; other sinks need their own accounting |
| Optional tracing observer | Enabled observer can own an `Arc`; ordinary `Trace` is zero-sized | Not enabled by this witness; qualify separately when selected |

Ownership sources: [worker](../hal/acquisition/src/lib.rs),
[cancellation](../hal/acquisition/src/readiness.rs),
[IMU slot and retained guard](../hal/imu/src/platform/nuttx/instance.rs),
[GNSS slot and UART cleanup](../hal/gnss/src/platform/nuttx/mod.rs), and
[tracing selection](../hal/acquisition/src/probe/mod.rs).
The [CoreS3 profile](../platform/cores3/profile.toml) requests 16,384 bytes for
each sensor worker. Those requests are separate from both linked static object
sizes and observed Rust allocations. The host witness requests 65,536 bytes per
worker so its debug and release test paths use the same declared stack budget;
neither request establishes the actual native allocation or stack high-water mark.

CI qualifies Linux x86-64 with pinned Rust 1.90.0, in debug and release. The test
requires Unix descriptor APIs and fails explicitly on unsupported hosts when
selected. The fixture's blocking pipes and two staged reads do not qualify
NuttX's nonblocking `pipe2`/`poll`, sensor I/O, IRQs, terminal backpressure or
startup-failure allocation paths. Existing lifecycle tests retain those separate
behavioral responsibilities. Host results do not establish Xtensa allocation
sizes, native heap reclamation, physical concurrency or total firmware RAM.

## What the counters mean

`alloc`, `zeroed`, `realloc` and `dealloc` are call counts at the registered Rust
GlobalAlloc boundary. `failed` counts returned null allocation/reallocation results.
Live bytes count requested layout sizes; live blocks count successfully allocated
blocks. A failed realloc leaves the old block/size live. A successful realloc
replaces its requested size without creating another live block.

Counters use pointer-width atomics, not AtomicU64. Hooks do not allocate, log,
lock a mutex, use thread-local storage or panic. Concurrent hooks may overlap.
An active-hook count and revision check allow snapshots to retry until they see
a coherent set of counters. Overflow/underflow is sticky-invalid, never a wrapped
zero that can pass. There are no counter resets that lose preexisting allocations.
The hooks add significant instrumentation overhead and are not timing probes.

`peak_process_requested_bytes` is the **process/probe lifetime** maximum of tracked
requested live bytes, not a per-phase peak. It includes test/runtime allocations
that cross this Rust boundary. It excludes allocator headers, alignment padding,
OS thread stacks, direct C allocation, direct System bypasses, and transient
internal realloc storage. Therefore it is neither resident RAM nor total NuttX heap.
All result printing occurs after the measured case's last snapshot.

Reclamation checks establish **net baseline stability in tracked live bytes and
block count**, not equality of pointer identities or a universal leak-freedom proof.
Unrelated allocation/deallocation can offset a leak; isolated execution and scoped
ownership reduce that ambiguity but do not replace allocation-identity tracing.

Every `MEMORY_RESULT` line is JSON. The independent Python checker requires the
exact app/case/phase inventory, numeric validity, allocation budgets, reclamation
balances and observer negative-control marker. Completion markers alone fail.
CI executes the real isolated tests in debug and release, checks out the exact
PR head (or push revision), records its commit/tree and
compiler, and verifies the probe is absent from normal/build dependency graphs.

## Explicit remaining scope

No NuttX heap/free-block or stack-high-water values are invented from these Rust
counters. OS heap usage, largest free block, physical-board stack measurements and
firmware allocator probes still need separate target instrumentation. This change
does not claim to solve fragmentation or prove a universal no-allocation property.
A passing warmed channel probe is not proof that every std channel use is allocation
free. Results apply to the exact compiler/build/platform and exercised paths.
The Rust optimizer may eliminate otherwise explicit allocations; the tests use
black_box and positive observer controls, but no unsafe assumption relies on an
allocation taking place.

## Primary API references

- https://doc.rust-lang.org/std/alloc/trait.GlobalAlloc.html
- https://doc.rust-lang.org/std/alloc/struct.System.html
- https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sync/mpmc/context.rs
