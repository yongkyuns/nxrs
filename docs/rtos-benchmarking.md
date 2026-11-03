# NuttX, Rust/std and active-object characterization

This is an isolated qualification fixture under `tests/rtos-bench`, not a new
production runtime or HAL. The ordinary Rust `main()` is a non-default workspace
member because it intentionally measures OS APIs. Production apps, services,
platform profiles and the existing event transport are unchanged.

The follow-up [std channel performance plan](std-channel-performance.md) and
[`tests/std-channel-perf`](../tests/std-channel-perf) focus on the actual bounded
std channel and pthread parking path. They extend this evidence without replacing
the original Thread-Metric loops, the native physical service comparison or their
recorded measurement boundaries.

## Three independent questions

1. **C/NuttX:** pinned, unmodified Thread-Metric loops and our own NuttX adapter;
   also a standalone C executable for the matched primitive experiments.
2. **Matched implementations:** C caller loop -> Rust caller of the same C/POSIX
   shim -> raw Rust `sync_channel` -> existing `Sender`/`Inbox`.
3. **Report correspondence:** factual reference data and unresolved prerequisites,
   never a computed cross-architecture score.

The C/POSIX and Rust/POSIX loops share explicitly non-inlined C shim calls.
They isolate caller-loop/bindings differences **including the shared shim cost**,
not an ideal bare syscall. The standalone C firmware contains no Rust runtime.
A `c-posix` run *inside* the Rust image is a further same-image control, not the
independent baseline. The queue comparison changes implementation (POSIX mqueue
vs std channel); it is not a pure language-overhead measurement.

## Initial executable matrix

| Case | Independent C / C-in-Rust / Rust-POSIX | Raw std / Rust AO |
| --- | --- | --- |
| semaphore-hot | sem_trywait + sem_post | Not applicable |
| heap128 | malloc/free, observable first/last-byte accesses | Not applicable |
| queue-hot | Nonblocking send/receive, 16 bytes, 8 slots | Same payload/capacity/admission semantics |
| yield-alone | sched_yield call cost; no switch guarantee | Not applicable |
| semaphore-handoff | Real two-thread semaphore request/response; one round trip per iteration | Rust caller uses the same C/POSIX object and step function |
| ping-pong | Not directly equivalent | One Rust echo owner, two bounded channels, lossless/blocking |

`queue-hot` and the original Thread-Metric message loop each return an otherwise
empty queue to empty on the same worker. Their message score does not measure a
blocked receiver, direct handoff or deep priority insertion. Keep those cases
separate when attributing a speed difference. The report's normalized percentages
also have a different denominator for every test row; they are not an aggregate
RTOS score. Native ThreadX queue results are not its POSIX adapter's results.

Hot cases have 100 warmup iterations, then two clock reads around the requested
operation count. Setup and teardown are outside that interval. They are new,
fixed-count experiments: **not replacements for the original Thread-Metric
30-second counting loops**. The clock is CLOCK_MONOTONIC read through the same C
shim; nominal clock resolution and actual caller scheduling policy/priority are
reported. A zero elapsed interval fails instead of becoming infinite throughput.

`semaphore-handoff` is the scheduler/wakeup baseline that `yield-alone` cannot
provide: a C-created worker blocks on a request semaphore, the caller posts it,
the worker posts a response, and the caller blocks waiting for that response.
The C caller and Rust/POSIX caller use the same opaque object and same non-inlined
step function. This is a two-wakeup/two-block round trip, not a one-way context
switch latency, but it directly answers whether the current NuttX blocking/
wakeup path is expensive at application scale.

The ping-pong fixture instantiates exactly the same generic owner loop with raw
channels or the existing AO inbox. The echo owner requests 32 KiB of stack; this
is not measured stack consumption. Warmup precedes sampling; the measured total
includes round-trip timestamps and histogram bookkeeping. Percentile fields are
inclusive logarithmic **upper bounds**, not exact quantiles. Threads are joined
and message counts checked before success. Cleanup is cooperative; the outer
host/QEMU timeout is not a physical hardware watchdog guarantee.

On NuttX, every matched result now snapshots `mallinfo()` before setup, after
setup, after the active measurement, and after teardown. Results retain current
heap usage, allocator high-water and largest-free-block observations. The most
useful overhead quantities are setup delta and retained-after-teardown delta;
global peak values are interpreted only with boot/history provenance. Raw std
versus AO therefore has a direct dynamic-memory comparison in addition to CPU
timing.

Each firmware build also records GNU `size` text/data/BSS for the complete
linked image. A matched-config C image versus Rust image is a practical
whole-firmware footprint delta (OS + benchmark app + linked runtime), not a
claim that every byte of the difference is intrinsic to the Rust language.
If that delta is material, a minimal-app decomposition can follow.

**Readiness does not prove a blocked receiver.** Ping-pong is a round-trip
measurement with `receiver_parked_verified=false`, not isolated wakeup latency,
not a pure context switch, and not one-way latency obtained by dividing by two.
Actual parked-state verification requires a scheduler witness on a controlled
single-core priority setup and remains a separate qualification step.

## Local Linux qualification

```sh
python3 -m unittest discover -s tests/rtos-bench -p test_bench.py -v
python3 tests/rtos-bench/build.py c-host
cargo test --locked -p nxrs-rt-bench
CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_LTO=false \
  cargo run --locked --release -p nxrs-rt-bench -- rust-ao ping-pong 10000
```

Backends are `c-posix`, `rust-posix`, `rust-std`, `rust-ao`. Run each backend/case
in a fresh process, then repeat in counterbalanced order; do not cherry-pick the
best run. The inherited repository release default is size optimization (`z`).
Set and retain the benchmark's explicit speed-profile environment as above rather
than changing the production default. The C shim is always GCC -O2, no LTO.
The initial host is Linux: no emulated POSIX mqueue on macOS or browser fallback.

## Standalone C / real NuttX builds

Initialize the existing pinned submodules and install the same ARM toolchain,
Kconfig and QEMU prerequisites as the repository's std qualification.

```sh
python3 tests/rtos-bench/build.py fetch
python3 tests/rtos-bench/build.py c-firmware --suite thread-metric --case message
python3 tests/rtos-bench/build.py c-firmware --suite posix
python3 tests/rtos-bench/build.py rust-firmware
```

The helper prints its artifact directory. By default each C build archives the
repository's pinned NuttX/apps revisions into a new isolated build directory. It
retains the resolved Kconfig, compiler identity, final ELF hash, source hashes and
symbol inventory. C builds do not invoke Cargo.

Raw C profiles `raw-o2` and `raw-ofast`
select periodic 1 kHz, no round-robin time slicing, no SMP and disabled debug /
stack coloration / priority inheritance. They retain the selected board's FPU
and other platform settings: **they do not become STM32L475 report reproductions**.
The Rust build uses the unchanged selected nxrs kernel profile and explicit
Rust optimization level 2, not the raw comparison profile.

For a matched independent C image, supply the Rust build's resolved config:

```sh
python3 tests/rtos-bench/build.py c-firmware --suite posix --profile matched \
  --matched-config PATH_TO_RUST_BUILD/resolved.config
```

The configuration check permits only the two application-selection flags to
differ; scheduling, time, libc, TLS and diagnostics may not silently diverge.
The matched POSIX C application's flags are explicitly -O2/no-LTO, like the shim.
Full compiler commands must accompany hardware results. Matching configuration
alone does not establish identical linked code placement or memory/cache behavior.

Six original Thread-Metric tests are buildable: basic, cooperative, preemptive,
memory, message and synchronization. The default duration is the original 30
seconds; one-second windows are **smoke tests only**. Threads use explicit FIFO
priorities `220 - tm_priority`; the reporter is higher priority than all workers.
All resources exist before workers start. Queues use 8 slots of 16 bytes; the
original report's exact queue capacity/adapter is not supplied. Resume/suspend
uses per-thread semaphores; allocation uses malloc, not a fixed-block pool.
The original reporter checks and loops are not patched. Each image runs one test
until the external harness captures two windows and resets it. Do not restart an
infinite original test in the same boot or assume it joined its worker threads.

The optimized upstream Basic source has a separate validity problem: its worker
increments `tm_basic_processing_counter` in a tight loop with no function call,
but the shared counter is not `volatile` or atomic. In the observed ARM build,
the compiler kept that counter in a register, so the reporting thread read zero
even while the volatile 1024-element work array continued to change. The
unmodified `basic` case remains authoritative for source fidelity and must stay
invalid when its original check fails. An auxiliary `basic-observable` case is
also available; it copies the pinned source and changes exactly that one counter
declaration to `volatile` in the isolated build directory. This is a diagnostic
visibility repair, not an exact Thread-Metric/report reproduction and not a claim
of C11 data-race correctness. Its provenance records
`thread_metric_source_variant=basic-counter-volatile`.

Both upstream interrupt test sources are pinned and downloaded, but deliberately
**not offered as runnable cases**. Their reference SVC macro cannot replace
NuttX's own SVC handler. No POSIX signal, timer thread or direct C call is accepted
as a substitute for a real hardware interrupt. GPIO/timer/IRQ marker adapters and
physical scope captures are outstanding work.

## Output and report comparison

`analyze.py` validates complete transcripts, counts, identities, timing boundaries
and failures. Original counter errors remain `valid=false`; a fast invalid
cooperative run is never a usable throughput result. A truncated/missing window
is an error, not an empty success. Example:

```sh
python3 tests/rtos-bench/analyze.py thread-metric console.log
```

`analyze.py compare left.json right.json` accepts environment/result envelopes
only with matching physical board, clock, kernel revision/configuration, build
profile and measurement-session identity. It rejects host/QEMU comparisons and
mismatched scheduling/instrumentation. Provenance is supplied evidence, not
self-authenticating merely because two JSON strings agree. Retain toolchain,
clock verification, binary hashes, repeated runs and actual compiler commands.
No hardware result or speedup is checked into this change.

`tests/rtos-bench/report-reference.json` records selected factual values from the
user-supplied Beningo PDF, its SHA-256, and exact PDF page references (printed page
numbers differ). The PDF itself is not redistributed. Headline Section 1.3 says
uniform -Ofast, while Appendix L lists -O2; the discrepancy is unresolved. The
exact author's adapter/build artifacts and complete absolute throughput counts
are missing. Percentages of category-best do not yield absolute operations/sec.
The report's failed NuttX cooperative check is retained explicitly. The pinned
upstream revision is our chosen reproducible source, not asserted to be his.

## Flat NuttX built-in restart caveat

The production architecture runs one firmware application `main()` for the life
of the MCU boot. Repeatedly invoking the same Rust program as an NSH built-in in
one flat NuttX image exercises a different lifecycle.

Pinned Rust 1.90's Unix TLS implementation stores each lazily-created
`pthread_key_t` in a process-global static `LazyKey`. NuttX, however, allocates
pthread keys and their destructor table from the current **task group**.
`pthread_setspecific()` accepts an in-range numeric key without proving it was
created for that new group, while `tls_destruct()` invokes a destructor only
when the current group's `ta_tlsdtor[key]` is populated.

Therefore, after one built-in task group exits, a later invocation can reuse
Rust's cached numeric key in a different task group without recreating the
corresponding NuttX destructor registration. This makes repeated Rust built-in
launches unsuitable as the primary performance/memory model and plausibly
explains the observed restart-only hard fault and target-side lifecycle drift.

The benchmark therefore uses **one command per fresh boot** for implementation
performance comparisons. Same-boot/restart tests are retained as diagnostics;
they do not gate the one-main-per-boot firmware characterization. Supporting
reentrant Rust built-ins would require a separate task-group-aware TLS/runtime
design rather than relaxing benchmark checks.

## Qualification remaining after this foundation

The [November 2 ESP32-S3 std-channel cohort](../tests/std-channel-perf/results/2026-11-02-esp32s3/README.md)
records verified blocked receivers and the reserved/published priority witnesses
on hardware; independent clock calibration remains outstanding. The
[focused next-step plan](event-path-benchmarks.md) prioritizes correcting the
terminal send-timing boundary, qualifying progress, and measuring the actual
messaging/three-class selection owner, provider wakeups and scheduling. Complete
Thread-Metric coverage, other boards and external RTOS ports are not prerequisites
for that work. Physical IRQ-to-provider measurements are added where they explain
the chosen acquisition path, rather than as an RTOS-wide interrupt ranking.
The [October native-API physical cohort](rtos-comparison.md)
already exists; it does not establish those std measurements. Runtime heap/stack
and image-size measurements quantify Rust/std/AO overhead; fragmentation, queue
occupancy, fairness and WCET remain separate. The existing
`ao-stress` app remains the system-load suite; its workload/percentile/loss limits
remain unchanged. Extend those measurements separately after validating the
primitive baseline, without introducing another AO framework.
