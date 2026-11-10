# Services and concurrency

Services use ordinary Rust functions and explicit resource ownership. A component gets a worker thread when it needs independent execution or a blocking wait.

## One owner, one wait point

Keep mutable state and owned resources together. Producers can clone a sender to submit typed messages to the same bounded inbox. One owner holds the receiver and uses `recv` or `recv_timeout` to sleep until there is work or a deadline expires. It completes one handler before taking the next event. This serializes changes to the service's state. When the order of events from different producers matters, encode it with timestamps or sequence numbers.

Use direct synchronous calls within an owner. `HealthService` in the navigation demo runs this way. When another active service must do work, send a request and handle the completion as a later event. Waiting for a peer from inside a handler can stall the inbox or create a cycle in which both owners wait for each other.

Some services need independent capacity for stop, important and ordinary events. They still use one logical wait point across those queues. This requires a selection mechanism and an explicit policy for priority and fairness. [Event communication reference](../../docs/concurrency-event-communication.md#dedicated-capacity-one-logical-wait-point)

## Separate acquisition from processing

An IMU HAL owns readiness, FIFO draining, decoding and device cleanup. A GNSS HAL owns UART reads, framing and parsing. Services receive normalized events and implement processing policy. The application connects the services.

In the navigation example, pause/resume controls processing. HAL acquisition continues, and suppressed measurements are counted as drops. Sensor output rates are configured separately.

Report a configured interrupt failure rather than silently switching to polling. Timer-based acquisition and mock providers are selected explicitly.

## Configure bounded delivery

The standard inbox uses `sync_channel::<T>(n)` with a positive capacity. The ring has room for `n` values of `T`, and sending and receiving reuse that storage. The ring needs no per-message allocation. On the NXRS ESP32-S3 NuttX build, the camera's eight Wi-Fi event slots are 64 bytes each, for 512 bytes of ring storage. Channel metadata, stacks and any heap buffers owned by messages count separately. Allocate reusable payload buffers during setup when dispatch needs to run without heap allocation. [Camera inbox](../../app/home-cam/src/runtime.rs) · [Channel memory analysis](../../docs/std-channel-performance.md#where-the-memory-goes)

An owner can block on an empty inbox. For producer paths that need to keep progressing, `try_send` either admits the event or returns it to the caller when the queue is full or disconnected. A blocking `send` waits for capacity. Reserve it for paths with a known progress guarantee, such as control code outside the service's dispatch loop.

Choose queue capacities for the expected bursts and consumer behavior, then measure them under load. Define how each queue handles overload: reject a new value, retain a pending value, or coalesce a state update.

Track attempted, admitted, rejected, handled and discarded messages. Keep sequence gaps visible and bound any retry storage used by producers.

Retain terminal acquisition faults separately from the ordinary data queue so they remain available when the queue is full. Report cleanup errors through the owning component's interface.

## Blocking across std platforms

The capacity and ownership semantics are the same on NuttX, Linux and macOS. A successful send transfers ownership of the value. Safe Rust requires values sent across threads to be `Send`, and the owner processes them through its single receiver.

NXRS's patched NuttX std keeps bounded-channel waiters on the calling thread's stack. It unlinks the waiter under the queue lock before publishing completion, then uses a retained thread handle to issue the wakeup after releasing the lock. This lets the blocked call return without leaving a dangling reference to its waiter. The first blocking call can still initialize thread state. Desktop Rust 1.90 uses a cached thread-local context and vectors for waiters, so a first wait or a larger set of blocked senders can allocate. [NuttX implementation](../../upstream/rust-std/README.md#native-nuttx-channel-and-parking) · [Rust waiter storage](https://github.com/rust-lang/rust/blob/1.90.0/library/std/src/sync/mpmc/waker.rs)

## Keep interrupt work small

ISRs and interrupt-context callbacks use the bounded native notification path. Parsing and I/O run in task context. Do not invoke service handlers, allocate, wait for queue capacity or call ordinary std/Crossbeam channels from interrupt context.

Document the execution context and lifetime of each native callback. Keep its Rust state alive until the callback has been unregistered and any in-flight work has completed.

## Stop, cancel and join

Create inboxes before starting producers. During shutdown, stop new production, wake blocked waits, drain or cancel pending work according to policy, join acquisition and release resources after cleanup completes.

Explicit stop may drain admitted work; dropping a handle can request cancellation without joining. Follow the component's shutdown contract and retain cleanup failures. After a timeout, resources must remain owned until the worker has stopped using them.

## Review scheduling

Queue capacity bounds queued values. Include any pending messages held by blocked senders or handlers when budgeting memory. Audit startup, first blocking use, steady transfers, timeouts, overload and teardown separately. The reported NuttX channel campaign measured live heap snapshots. Allocation call counts across all those phases still need target-specific measurement.

A try operation avoids waiting for queue capacity or data. Its execution time still depends on internal synchronization, priorities, IRQ load and the target configuration. Measure wake latency and handler deadlines under load, including fairness when separate queues give some events preference. Qualify SMP and any multi-queue selector on the intended target. [Navigation demo](../../docs/event-driven-demo.md) · [Channel performance analysis](../../docs/std-channel-performance.md) · [Performance and memory](performance.md)
