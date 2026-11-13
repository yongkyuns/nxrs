# Standard-library APIs

Rust's standard library is the starting point for application code in NXRS. NuttX provides the underlying operating-system facilities; the Rust target integration connects those facilities to `std`.

Use this chapter after [Your first application](first-application.md). The commands run from the NXRS checkout and use its pinned host toolchain. They demonstrate API behavior; the [NuttX walkthrough](firmware.md) covers target builds.

## Choose the interface for the operation

| Operation | Rust interface | Configuration to consider |
| --- | --- | --- |
| Worker execution | `std::thread`, `JoinHandle` | Stack allocation and NuttX scheduling |
| Message delivery | `std::sync::mpsc` | Queue capacity, backpressure, and shutdown |
| Shared state | `Mutex`, `Condvar`, atomics | Ownership and lock ordering |
| Time and deadlines | `Instant`, `Duration` | Clock resolution and timeout handling |
| Files and byte streams | `std::fs`, `std::io` | Mounted filesystem or registered device |
| IP communication | `std::net` | Network interface, addresses, and socket support |
| Owned data | `Vec`, `String`, `Box`, collections | Capacity and allocation budget |

API availability follows the selected target and NuttX configuration. The [Rust integration reference](../../docs/nuttx-std.md) lists the integration details and tests. Rust's [standard-library documentation](https://doc.rust-lang.org/std/) describes the APIs themselves.

## Threads and events

Use `thread::Builder` when you need a name or explicit stack allocation. Keep the returned handle and join the worker during shutdown. Use a bounded channel when queued work needs a fixed capacity.

[Services and concurrency](services.md) contains a complete, small example. The existing `std-demo` recipes also cover full and disconnected queues, ownership transfer, condition-variable waits, and deadline-based timeouts:

```sh
cargo run --locked -p nxrs-std-demo -- --case channels
cargo run --locked -p nxrs-std-demo -- --case synchronization
cargo run --locked -p nxrs-std-demo -- --case deadlines
```

Each invocation reports `STD_DEMO case=<name> status=pass` and ends with `STD_DEMO PASS cases=1`. The [recipe source](../../app/std-demo/src/concurrency.rs) shows the assertions and cleanup behind the result.

The entry-task stack configured in firmware metadata and a worker's `thread::Builder::stack_size` are separate allocations. Keep both in the memory budget. A thread name helps diagnostics; `std::thread` itself does not provide a portable priority-setting API. Target scheduling support belongs below reusable behavior, and its effect needs target measurements.

### Deadlines and waiting

Use `Instant` for elapsed time and deadlines. A timed receive waits for either work or its timeout; it does not guarantee that the thread will be scheduled at the exact expiry instant. On wakeup, compare the current time with the original deadline. Repeatedly restarting a full relative timeout can otherwise extend a request indefinitely.

Use condition variables with a predicate checked under the same mutex. Decide how poisoned locks, disconnected channels and worker panics reach the owner. The synchronization and deadline recipes demonstrate these Rust mechanisms; the [event communication reference](../../docs/concurrency-event-communication.md) discusses complete owner loops.

## Memory and collections

Select storage from the workload: fixed arrays for fixed-size data, preallocated collections for bounded variable data, and dynamic allocation where the lifetime and budget allow it. A channel's capacity bounds its slots; payload allocations and retained buffers belong in the same memory budget.

```sh
cargo run --locked -p nxrs-std-demo -- --case fixed
cargo run --locked -p nxrs-std-demo -- --case vec
cargo run --locked -p nxrs-std-demo -- --case maps
```

[Performance and memory](performance.md) describes image size, thread stacks, heap use, and communication measurements.

`Vec::with_capacity` reserves capacity; it does not prevent later growth. Check a collection's length against your application limit before inserting. Include data owned by each element: a queue of `Vec<u8>` values bounds message count but can still retain a large amount of payload memory.

Use an owned value when work crosses a thread boundary. For a large payload, decide whether to move an existing allocation or use a bounded, reusable buffer pool. Small copied messages often need no additional abstraction. The `ownership` and `queues` recipes provide further exercises:

```sh
cargo run --locked -p nxrs-std-demo -- --case ownership
cargo run --locked -p nxrs-std-demo -- --case queues
```

## Files and networking

Use ordinary Rust file and socket operations for the data path. Peripheral configuration and radio control can require additional interfaces: opening a serial byte stream and configuring its baud rate are separate operations, as are sending an IP packet and joining a Wi-Fi network.

See [Networking and storage](io.md) for application structure and [Drivers and HALs](hardware.md) for peripheral access.

`Read::read` and `Write::write` may transfer fewer bytes than requested. Select a framing and retry policy, use the appropriate complete-transfer helper when needed, and retain the underlying error. A successful socket send and a successful file write provide different guarantees; neither should be renamed as an application-level acknowledgement without implementing that protocol.

## Using third-party crates

Check the crate's target support and dependencies. Portable Rust algorithms often need little target-specific work. Crates that use native libraries, OS-specific syscalls, large thread pools, or background services require corresponding integration and resource planning.

Assess a dependency in this order:

1. Read its selected Cargo features and transitive dependencies. Enable the functionality the application needs.
2. Identify OS assumptions: processes, signals, dynamic loading, native libraries, filesystem paths, entropy sources or background threads.
3. Exercise its portable logic and failure cases on the host with the same feature set.
4. Build through the selected NXRS target profile and check required OS resources and native ABI support.
5. Run the relevant workload on the target, including memory use, timeouts and shutdown.

Keep dependencies in Cargo manifests. `std` provides a common API; the selected profile determines the OS services and resources available to the application. The [Rust std integration](../../docs/nuttx-std.md) and [upstream patchsets](../../docs/upstream-patchsets.md) references describe the maintained target work and known boundaries.

## Decide when another interface helps

Stay with a standard API when its contract matches the operation. Use a capability when acquisition, hardware control, delivery or lifecycle needs a stronger subsystem contract. Keep a pure parser, transformation or data structure independent of either.

Continue with [Choose an interface](interfaces.md), [Services and concurrency](services.md), or [Networking and storage](io.md), depending on the next operation in your application. The [Rust API reference](../api.md) covers NXRS capability, messaging, service, and chip-driver APIs alongside this use of `std`.
