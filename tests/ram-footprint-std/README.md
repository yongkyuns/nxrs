# Ordinary Rust RAM scaling controls

This fixture measures ordinary `std::thread::Builder`, the actual
`nxrs-messaging` bounded inbox, and the pinned Crossbeam multi-queue candidate.
It complements the historical native-API C/Rust service comparison. It does
not substitute POSIX mqueues or a private pthread wrapper for the Rust paths.
There is no async executor.

The fixture is a separate Cargo workspace with normal `fn main()` entry points.
Its four binaries let a final linked-image analysis distinguish an instrumented
startup baseline, threads, std inboxes and Crossbeam. Linker elimination must be
checked in each final ELF; compiling a dependency is not proof that its storage
is retained. The baseline deliberately includes the same allocation/reporting
instrumentation. It is not a claim about the minimum empty Rust image.

| Binary | Case | Resource contract |
| --- | --- | --- |
| `ram-baseline` | No arguments | Ordinary startup and the measurement harness; no workers or channels |
| `ram-threads` | `threads COUNT STACK` | Only ordinary std workers parked at the diagnostic gate |
| `ram-std-inbox` | `queues COUNT STACK` | Three actual Nxrs bounded inboxes per owner, eight 64-byte slots each; no workers |
| `ram-crossbeam-select` | `queues COUNT STACK` | Three pinned Crossbeam bounded channels per owner, eight 64-byte slots each; no workers |
| `ram-std-inbox` | `services COUNT STACK` | One 24-slot Nxrs inbox and one std worker per owner |
| `ram-crossbeam-select` | `services COUNT STACK` | Three independently bounded eight-slot channels and one fixed three-arm `select_biased!` per std worker |

`COUNT` is one of `0`, `1`, `5`, `10`, or `20`; `STACK` is an explicit byte
request. At twenty owners, both capacity layouts contain 480 events, or
**30,720 bytes of event contents**. They do not offer identical admission
isolation: one 24-slot FIFO can use every slot for one class, while three
eight-slot FIFOs preserve independent capacity. Std `Inbox` has no multi-queue
selector. The fixture does not pretend otherwise or implement a replacement.

## What each invocation verifies

Every process starts without previous channel/worker warm-up. Output setup and
the main thread's `thread::current()` bookkeeping are initialized explicitly
before its baseline. Every sample is retained in a fixed-size recorder; no
report is printed between the measured phases.

Queue-only controls record `baseline`, `empty`, `full`, `drained`, and
`released`. They admit all slots, reject one excess event per queue with the
original value intact, verify every payload word and per-lane order, verify
empty and disconnected states, and require exact Rust requested-byte/block
reclamation.

Service controls additionally record endpoint construction, join-handle storage,
parked workers, the first completed blocking timeout, eight repeated completed
timeouts, joined workers, and final release. A mutex/condition-variable gate
holds every worker out of the queue drain while capacity is filled. The
coordinator acquires the same gate mutex after each worker releases it into
its wait, so the snapshots are taken with workers quiescent at a known point.
Every admitted event is consumed, all nine empty waits must time out, all
required closures must be observed, and every worker must join.

The timeout phases exercise the real std receive / Crossbeam selection and
timer-wakeup path, including first-use allocation. They measure retained
allocation **after** the completed wait; they do not claim to snapshot the
maximum simultaneous blocked-waiter footprint. The allocator's process-lifetime
peak can expose larger transient demand but is not reset at each phase and can
already include startup/argument parsing. This is not a latency measurement or
a sender-to-receiver latency/wakeup qualification.

The pure-thread case uses the same diagnostic gate. Gate and coordinator
effects can therefore be assessed against that control. The report identifies
the Rust struct size of the fixed gate/recorder and exact requested bytes for
endpoint vectors and the join-handle vector. Endpoint-vector bytes are fixture
wiring, not channel internals. These are explanatory subsets of the measured
memory; **do not add them again**. The recorder struct size is not a compiler
stack-frame analysis.

## Accounting boundaries

| Field | Meaning | Must not be interpreted as |
| --- | --- | --- |
| `rust_live`, `rust_blocks` | Live requests observed by `tests/allocation-probe` | Allocator usable size, OS heap, RSS, or thread-stack allocation |
| `rust_peak_process` | Process-lifetime high water of requested Rust allocation bytes | A resettable peak for each phase |
| `native_heap_used/free/arena` | NuttX `mallinfo()` through target headers | A Rust-only measurement or complete firmware RAM |
| `stack_request_*` | Explicit `thread::Builder::stack_size` requests | Measured OS allocation or stack use |
| Worker `stack_usable` | NuttX `tcb_s::adj_stack_size` | Gross malloc block size including every native header/TLS reservation |
| Worker `stack_observed` | Stack-coloration high water observed in the worker after channel cleanup | A proven worst-case stack bound |
| `reporting_stack` | Main stack observed after the substantial reporting path | A static proof that any smaller main stack is safe |

On NuttX the native heap includes allocations behind the Rust allocator as well
as native allocations such as thread resources. **Do not sum Rust live bytes
and native heap used.** Add only disjoint final-ELF regions/reservations when
constructing a full-image ledger; account for any heap arena only once. Platform
fixed storage, OS stacks, RAM code/vectors, and retained feature costs remain
separate from per-worker/per-queue slopes.

Native snapshots include activity outside the fixture if other NuttX tasks
allocate concurrently. Use a controlled board profile, repeated runs, and the
same background configuration. The observations from different fields are
sequential, not one atomic whole-system transaction. The native stack helper
itself contributes to observed stack use. Worker sampling precedes the final
std/pthread teardown, so it cannot prove teardown stack depth. Stack margins
and exceptional paths still require separate physical qualification.

Host records use `domain="host-functional"`, expose requested Rust bytes, and
emit `null` for all native heap/stack measurements. Host pthread reservations,
guard pages, allocation headers, kernel data and resident memory are outside
the allocator hook. Neither the host request nor host numbers are MCU RAM.

## Run the host controls

From the repository root, using the pinned host toolchain:

```sh
python3 tests/ram-footprint-std/run.py --out target/ram-footprint-std-host
```

The output directory must be new. The runner builds locked release binaries and
runs 26 fresh processes: the startup baseline plus five counts for each of the
five nonbaseline arms. `--repeats 3` repeats this matrix without reusing a
process. `--stack-bytes 65536` changes the explicitly labelled host request.
The runner retains logs, exact source and executable hashes, Rust/Cargo versions,
and a structured `report.json`. The hashes identify the tested source even when
it contains uncommitted changes; a recorded Git HEAD alone is not that identity.

The captured host evidence under `results/` is a functional and accounting
witness. It does not replace the missing physical matrix.

## Pinned compiler and allocator instrumentation

The target build exercises the shared diagnostic allocator as real generated
code. The pinned Espressif Rust `1.90.0.0` compiler fails optimized atomic
`fetch_max`/`fetch_min` lowering with `Undefined temporary symbol`: the emitted
Xtensa assembly branches to missing basic-block labels. A minimal `no_std`
atomic reproducer confirms that this is independent of the fixture, NuttX
startup, and debug information. The original `fetch_max` calls in the
allocator's peak update exposed that defect in all four target images.

`tests/allocation-probe` therefore expresses the peak update with an explicit
`compare_exchange_weak` retry loop. It keeps a sequentially consistent
read-modify-write even when the old peak already exceeds the candidate. It
preserves the high-water measurement and concurrent ordering contract without
changing the production allocator, compiler pin, or optimization policy. The
allocator tests cover concurrent live allocations and peak retention after
release; target CI must compile and link all four ordinary Rust images.

## Build and capture the NuttX controls

Use the canonical frontend with the dedicated single-core, no-PSRAM profile.
The observer is the only target C helper; it has no thread/queue behavior.
The profile must enable flat mode and stack coloration, or the helper refuses
to compile.

```sh
cargo nxrs build --manifest-path tests/ram-footprint-std/Cargo.toml \
  --profile esp32s3-ram-footprint --bin ram-std-inbox \
  --command ramprobe --stack-size 16384 --abi-profile minimal \
  --target-c-source tests/ram-footprint-std/ffi/observe.c \
  --out target/ram-footprint-std-nuttx-inbox
```

Build each other binary into a separate output directory with the same profile,
main-task stack, command name, SDK and optimization policy. Preserve its final
ELF, map, resolved configuration, source/SDK provenance and firmware hash before
flashing. The profile retains NSH for reproducible invocation; its absolute
total is not directly comparable with the October shell-free image.
Use the supported `minimal` ABI profile consistently because the startup-only
binary need not retain `pthread_create`/`pthread_join`. Target-header/layout ABI
checks still run; this changes which API calls each binary must reference, not
the target ABI. The canonical frontend's build policy is recorded independently
of the standalone host Cargo profile; do not assume their LTO settings match.

On the corresponding image, capture one invocation per log, for example:

```text
ramprobe services 20 4096
```

Run all five counts. `ram-threads` takes `threads COUNT STACK`; the two channel
images take `queues COUNT STACK` or `services COUNT STACK`; `ram-baseline`
takes no arguments. Reboot between invocations for true fresh-process/kernel
first-use controls: an NSH task exit alone does not reset OS/runtime caches.
Treat 4,096-byte workers and the 16,384-byte main stack as initial provisions,
not certified minima. Compare multiple worker requests only after the current
one completes safely; retain the observed watermark and an explicit margin.

Validate and normalize a captured invocation:

```sh
python3 tests/ram-footprint-std/report.py --input target/inbox-20.log \
  --domain nuttx-target --out target/inbox-20.json
```

The parser rejects missing phases/completion, host/native mixing, altered
capacity/topology, missing worker results, invalid stack observations and
unreclaimed queue-only allocations. Target-domain output alone does not prove
physical execution: retain the board/run provenance and exact flashed-image
identity with the evidence. This fixture's ELF/native build and physical runs
must be reported independently of the host witness.
