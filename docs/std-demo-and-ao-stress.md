# Standard-library recipes and active-object stress qualification

Two independent ordinary `main()` binaries. Each is a complete selected firmware
app, not a plugin in another app, a platform launcher, or a new actor framework.
Neither app depends on a HAL provider. `ao-stress` uses the existing
`nxrs-messaging` bounded inbox unchanged. No new third-party dependencies.

## Run on a host

```sh
cargo run --locked -p nxrs-std-demo --bin std-demo
cargo run --locked -p nxrs-std-demo --bin std-demo -- --list
cargo run --locked -p nxrs-std-demo --bin std-demo -- --case channels
cargo run --locked -p nxrs-ao-stress --bin ao-stress
cargo run --locked --release -p nxrs-ao-stress --bin ao-stress -- \
  --scenario burst --duration-ms 2000 --producers 4 --workers 4 --capacity 32
cargo run --locked --release -p nxrs-ao-stress --bin ao-stress -- \
  --scenario slow-consumer --capacity 1 --rounds 10
```

These are `std`/threaded native or NuttX apps. They are **not** advertised as
single-threaded `wasm32-unknown-unknown` browser apps. Nxrs's separate browser
thread qualification is not changed here. Do not run these blocking entry points
on a browser UI thread.

## Build exactly one firmware app with the existing Cargo interface

```sh
cargo nxrs apps
cargo nxrs build --app std-demo --profile pico2-mock
cargo nxrs build --app ao-stress --profile pico2-mock
cargo nxrs build --app std-demo --profile esp32s3-qemu-mock
cargo nxrs build --app ao-stress --profile esp32s3-qemu-mock
cargo nxrs build --app std-demo --profile mps2-an521-mock
cargo nxrs build --app ao-stress --profile mps2-an521-mock
```

Use the repository's existing pinned NuttX/toolchain prerequisite setup. Images
remain under `target/firmware/<app>/<platform>/`. NSH commands are `std_demo` and
`ao_stress`, respectively. Existing platform profiles happen to select mock HAL
packages for their other apps; these two app dependency graphs acquire none of
them. A mock platform name is not an assertion of sensor or radio qualification.

## The curated recipes

| Case | Recipe | Embedded lesson |
| --- | --- | --- |
| `vec` | Dynamic Vec growth; with_capacity; fallible reservation; app-local BoundedVec; 100 reuse cycles | Reservation is not a maximum. The wrapper enforces a logical limit and returns rejected values. |
| `fixed` | Inline array and occupied slice | Truly fixed inline storage with no backing allocation; not an inline Vec supplied by std. |
| `maps` | HashMap with static keys, reserve and entry; BTreeMap | Reject new keys at the limit but allow updates; BTreeMap orders keys and allocates nodes. |
| `queues` | Rolling VecDeque; BinaryHeap of Reverse deadlines | Reuse storage and pop before push; earliest deadline first, no unnecessary new scheduler. |
| `bytes` | Cursor over fixed bytes; little-endian encoding; String reuse; Cow | Bound serialization, avoid temporary format! allocations, make borrow-to-own transitions explicit. |
| `ownership` | Box, Arc, OnceLock, named/joined threads | Distinguish unique ownership, immutable sharing, and one-time publication. |
| `channels` | Bounded and zero-capacity sync_channel; full/disconnect; moved buffer | Choose overflow policy, retain rejected ownership, and avoid per-message reply channels. |
| `synchronization` | Mutex/Condvar predicate; AtomicBool release/acquire | Predicate loops handle spurious wakeups. No 64-bit atomic requirement on 32-bit MCUs. |
| `deadlines` | Instant/Duration and recv_timeout | Use remaining time from an absolute deadline, then drain/disconnect cleanly. |

`Vec::with_capacity(N)` can grow past N. An allocator may also provide a capacity
larger than requested. `BoundedVec` is a tiny private example of an application
policy around a heap-backed Vec, not a general-purpose collection library. For a
production inline growable collection, evaluate an established fixed-capacity
container separately; std's array example deliberately adds no dependency.

Capacity/pointer reuse checks prove that those Vec buffers did not grow. They do
**not** establish that the entire process, std channel implementation, formatting,
element constructors/destructors, allocator, or OS performs no allocations.
`clear()` drops elements; it retains the Vec backing storage. HashMap's default
hasher needs platform randomness. The NuttX std backend already configures the
randomness device; runtime qualification must still exercise that path.

The overflow recipe requests an impossible `u32` Vec capacity to exercise a
checked capacity error. It does not deliberately exhaust physical memory. Other
infallible allocations and thread creation still have their platform limitations.

## Stress topology and policy

```text
P paced/bursty producer threads
  -> W worker active objects, each with its own bounded inbox and private state
  -> one collector active object with another bounded inbox and private state
```

Default: two producers, two workers, one collector, 16 slots per inbox, 300 ms
per scenario. The four scenarios are paced steady traffic, unpaced bursts,
a collector with a deliberate 1 ms blocking-I/O surrogate, and CPU-heavy worker
handlers (at least 4096 iterations). `--work` sets the ordinary handler iterations;
CPU-load uses `max(work, 4096)`. Effective settings and platform are printed.

Workers use the existing `Inbox::wait` as their single event/deadline wait
point. They check a 5 ms owner timer before every receive, including when their
inbox never empties. Missed timer periods are skipped rather than replayed without
a bound. Only setup/cancellation is shared; no global mutable application-state
mutex, async executor, universal event bus, or production HAL is introduced.

Both ingress and egress use **try_send/drop-newest**, explicitly counted at their
respective boundaries. Worker handlers never block waiting for another owner's
queue capacity. Thus overload is a result to measure, not automatically a failed
run. A deterministic full-queue/disconnect probe also executes before scenarios,
so transport edge coverage does not depend on a particular host schedule.

## What constitutes success

For each run, the app checks:

- attempted = accepted + ingress-full; accepted = handled;
- handled = forwarded + egress-full; forwarded = collector-received;
- accepted/handled and forwarded/received checksum digests agree;
- payload checksums and strictly increasing per-(worker, producer) sequences;
- exactly one report per configured owner, no unexpected disconnections, and
  nonzero collector progress.

Sequence gaps are valid when explicit drops occurred. There is no claimed global
ordering across independent producers/workers. Per-producer and per-worker counts
are printed to expose skew or starvation; PASS is **not** a fairness guarantee.
The synthetic checksum catches damage to the message identifiers; this is not a
camera/DMA payload or hardware-sensor integrity test.

When producers finish, their last input senders disappear; workers drain admitted
input and drop output senders; the collector drains and exits. All threads are
joined before the app prints `AO_RESULT` and ultimately `AO_STRESS PASS`.
Startup failure, timeout and early return set a cooperative cancellation flag,
unblock the startup gate, and join started threads. Repeated rounds start fresh
owners instead of leaving detached workers behind.

`--shutdown-ms` is a completion deadline, not a force-kill primitive or a proven
worst-case shutdown bound: joining cooperative threads still depends on scheduler
progress. Host/QEMU test harnesses add an external process timeout. Firmware
products requiring a hard bound need their platform watchdog policy.

## Metrics and their limitations

Each `AO_RESULT` is a JSON object following that prefix. It contains throughput,
ingress/egress losses, received-message latency, deadline misses, timer wakeup
lateness, shutdown time, configured queue slots and requested thread stack bytes.
`AO_CONFIG`, `AO_PRODUCER`, and `AO_WORKER` give settings and per-owner progress.
No stdout logging happens in the measured handlers.

Latencies are **upper bounds from fixed logarithmic histogram buckets**, explicitly
named `p50_upper_us`, `p95_upper_us`, and `p99_upper_us`; `max_latency_us` is the
observed maximum rounded up to microseconds. Samples are rounded up before
bucketing so a fractional microsecond cannot understate an upper bound. They measure enqueue-attempt to
collector receipt, excluding the collector's subsequent artificial sleep.
Dropped messages have no end-to-end latency sample: read loss and latency together.
`--deadline-us` counts misses; it is not an implicit platform-independent pass/fail
threshold. Throughput is received messages divided by elapsed time including drain.

`payload_bound_bytes` accounts for configured work-queue slots plus one in-flight
message per producer/worker/collector. It is a logical message-storage budget,
**not measured total RAM, heap high-water, exact queue occupancy, or allocation
instrumentation**. There are also bounded completion-report queues, handles,
channel/allocator metadata, stacks and runtime resources. Each spawned thread
requests 32 KiB; main requests 64 KiB in firmware metadata. Large allowed host
configurations can exceed MCU RAM and are not promised to fit. Start with defaults.

These measurements characterize this synthetic workload on the reported platform.
Host throughput is not MCU throughput; QEMU timing is not real-silicon timing.
No hard-real-time, zero-allocation, fragmentation, comparative speedup, physical
Pico/ESP32 performance, or reliability claim follows merely from passing.

## Qualification

```sh
cargo test --locked -p nxrs-std-demo -p nxrs-ao-stress
cargo build --locked -p nxrs-std-demo -p nxrs-ao-stress
python3 -m unittest discover -s tests/host -p test_std_apps.py -v
cargo clippy --locked -p nxrs-std-demo -p nxrs-ao-stress --all-targets -- -D warnings
```

Tests cover collection limits/ownership, channel semantics, histograms, accounting
faults, all scenarios, one-slot queues, repeated restart and partial-startup cleanup.
The process tests reject false PASS markers and invalid CLI configuration.
Qualification results belong to exact commit CI logs; a listed command is not a
claim that its firmware was built, booted or measured on physical hardware.

## API references

- Rust Vec guarantees: https://doc.rust-lang.org/std/vec/struct.Vec.html#guarantees
- Rust sync_channel: https://doc.rust-lang.org/std/sync/mpsc/fn.sync_channel.html
- Rust HashMap: https://doc.rust-lang.org/std/collections/struct.HashMap.html
