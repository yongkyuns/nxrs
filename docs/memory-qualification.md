# Memory qualification of the existing demos

This is the **Rust allocation/lifecycle** slice, on host test builds. It is not a
new app, actor runtime, allocator policy, heap HAL, or RTOS benchmark. The separate
RTOS benchmarking work is untouched.

## Run

```sh
cargo test --locked -p nxrs-allocation-probe
cargo test --locked -p nxrs-std-demo --features probe memory_qualification -- --ignored --test-threads=1 --nocapture
cargo test --locked -p nxrs-ao-stress --features probe memory_qualification -- --ignored --test-threads=1 --nocapture
# Repeat both app commands with --release to qualify the optimized build too.
```

Each command runs one explicitly ignored test in its own executable. The test
checks its invocation for the exact filter, ignored mode, one test thread and no
output capture, because unrelated libtest allocation activity would contaminate
process-global counts. This is diagnostic qualification, not normal `cargo run`.

`tests/allocation-probe` is a non-default workspace member and **dev-dependency**
of the two apps. Probe modules and their global-allocator registrations require
both `test` and `probe`. Normal binaries, including normal builds with that
feature selected, retain their usual allocator and do not depend on the probe.
Application `forbid(unsafe_code)` and all architecture boundary rules remain intact.
The unsafe allocator implementation and its direct-pointer accounting tests are
confined to the test-support crate. There are no new third-party dependencies.

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
   EventSender/EventInbox APIs. Queue/thread setup and 16 warm-up exchanges precede
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

Both test executables deliberately inject an allocation and retain its buffer
across a snapshot. The zero-call and reclaimed-memory assertions must both reject
it; releasing it must return to baseline. The observer itself separately tests
allocation/zeroing failure, failed realloc ownership, grow/shrink accounting,
alignment, byte preservation, concurrent owners and sticky counter errors.

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
CI executes the real isolated tests in debug and release, records the head and
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
