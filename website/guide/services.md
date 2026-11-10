# Services and concurrency

Services use ordinary Rust functions and explicit resource ownership. A component gets a worker thread when it needs independent execution or a blocking wait.

## One owner, one wait point

Keep mutable state and resources with their owner. Producers submit typed messages to a bounded inbox. The owner waits for an event, runs its handler to completion and waits again. Multiple event sources can feed the same inbox.

Concurrency uses Rust threads and blocking synchronization. Synchronous components such as `HealthService` in the navigation example run without their own thread.

## Separate acquisition from processing

An IMU HAL owns readiness, FIFO draining, decoding and device cleanup. A GNSS HAL owns UART reads, framing and parsing. Services receive normalized events and implement processing policy. The application connects the services.

In the navigation example, pause/resume controls processing. HAL acquisition continues, and suppressed measurements are counted as drops. Sensor output rates are configured separately.

Report a configured interrupt failure rather than silently switching to polling. Timer-based acquisition and mock providers are selected explicitly.

## Configure bounded delivery

Choose queue capacities for the expected bursts and consumer behavior, then measure them under load. Define how each queue handles overload: reject a new value, retain a pending value, or coalesce a state update.

Track attempted, admitted, rejected, handled and discarded messages. Keep sequence gaps visible and bound any retry storage used by producers.

Retain terminal acquisition faults separately from the ordinary data queue so they remain available when the queue is full. Report cleanup errors through the owning component's interface.

## Keep interrupt work small

ISRs and interrupt-context callbacks use the bounded native notification path. Parsing and I/O run in task context. Do not invoke service handlers, allocate, wait for queue capacity or call ordinary std/Crossbeam channels from interrupt context.

Document the execution context and lifetime of each native callback. Keep its Rust state alive until the callback has been unregistered and any in-flight work has completed.

## Stop, cancel and join

Create inboxes before starting producers. During shutdown, stop new production, wake blocked waits, drain or cancel pending work according to policy, join acquisition and release resources after cleanup completes.

Explicit stop may drain admitted work; dropping a handle can request cancellation without joining. Follow the component's shutdown contract and retain cleanup failures. After a timeout, resources must remain owned until the worker has stopped using them.

## Review scheduling

Queue capacity limits storage. Execution time also depends on producer and consumer priorities, queue reservations, locking, interrupt load and endpoint lifetimes. Review internal progress behavior before using a transport under fixed-priority scheduling, including nonblocking methods such as `try_recv`.

The [communication reference](../../docs/concurrency-event-communication.md), [navigation example](../../docs/event-driven-demo.md) and [channel performance analysis](../../docs/std-channel-performance.md) describe the scheduling requirements and measurements.
