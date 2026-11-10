# Standard-library APIs

Rust's standard library is the starting point for application code in NXRS. NuttX provides the underlying operating-system facilities; the Rust target integration connects those facilities to `std`.

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

## Memory and collections

Select storage from the workload: fixed arrays for fixed-size data, preallocated collections for bounded variable data, and dynamic allocation where the lifetime and budget allow it. A channel's capacity bounds its slots; payload allocations and retained buffers belong in the same memory budget.

```sh
cargo run --locked -p nxrs-std-demo -- --case fixed
cargo run --locked -p nxrs-std-demo -- --case vec
cargo run --locked -p nxrs-std-demo -- --case maps
```

[Performance and memory](performance.md) describes image size, thread stacks, heap use, and communication measurements.

## Files and networking

Use ordinary Rust file and socket operations for the data path. Peripheral configuration and radio control can require additional interfaces: opening a serial byte stream and configuring its baud rate are separate operations, as are sending an IP packet and joining a Wi-Fi network.

See [Networking and storage](io.md) for application structure and [Drivers and HALs](hardware.md) for peripheral access.

## Using third-party crates

Check the crate's target support and dependencies. Portable Rust algorithms often need little target-specific work. Crates that use native libraries, OS-specific syscalls, large thread pools, or background services require corresponding integration and resource planning.

Keep dependencies in Cargo manifests, and run their tests on the host before adding target integration tests. `std` provides a common API; the selected profile determines the OS services and resources available to the application.
