# Services and concurrency

The event-driven model is about explicit ownership, not replacing Rust with an actor language. Ordinary functions remain useful. An active owner is introduced when a component needs independent execution or a wait boundary.

## One owner, one wait point

Keep mutable state and owned resources together. Producers submit typed messages to a bounded inbox. The owner waits, dispatches one event, completes its handler and waits again. Different event sources can feed the same owner rather than forcing it to poll several queues in a busy loop.

Use Rust threads and blocking synchronization. An async executor is not required. `HealthService` in the navigation demo remains synchronous; not every reusable component needs its own thread.

## Separate acquisition from processing

An IMU HAL owns readiness, FIFO draining, decoding and device cleanup. A GNSS HAL owns UART reads, framing and parsing. Services receive normalized events and own processing policy. The app composes the services.

In the navigation example, pause/resume controls processing. HAL acquisition continues draining, and suppressed measurements are counted as drops. Pausing processing does not reconfigure the sensor’s output rate.

A configured interrupt failure must remain a failure. Do not silently replace it with periodic polling. Explicit mock or unwired-device timer paths have their own documented scope.

## Design bounded delivery

Choose capacities from the expected burst and consumer behavior, then measure them. Decide what a full queue means before adding producers. Possible policies include rejecting the new value, retaining a pending value, or coalescing a state update, but they are not interchangeable.

Account separately for attempted, admitted, rejected, handled and discarded messages. Keep sequence gaps visible. Do not allow a slow consumer to turn a bounded design into an unbounded retry list elsewhere.

A terminal acquisition fault must not disappear because the ordinary data queue is full. The existing sensor implementation retains fault state and exposes cleanup outcomes; follow the owning contract rather than inventing a generic success fallback.

## Keep interrupt work small

An ISR or interrupt-context callback is not a service thread. It must not invoke service handlers, allocate, wait for queue capacity or call ordinary std/Crossbeam channels. Use the qualified bounded native notification path and defer parsing and I/O to task context.

The exact context and lifetime of a native callback must be documented. A callback registration cannot outlive the Rust state it references.

## Stop, cancel and join deliberately

Create inboxes before producers start. On shutdown, stop new production, wake blocked waits, complete or cancel admitted work according to policy, join acquisition and verify resource cleanup. Retain cleanup failures rather than reporting resources as released prematurely.

Explicit stop may drain admitted work; dropping a handle can request cancellation without joining. Read the actual service contract. A shutdown timeout alone does not prove that a worker stopped accessing memory or a device.

## Review scheduling separately

A bounded queue is a memory bound, not automatically a method-time bound. A function named `try_recv` does not prove that every internal path is safe under fixed-priority preemption. Producer/consumer priority, queue reservations, lock ownership, IRQ load and all endpoint holders matter.

Do not adopt a scheduler policy from a benchmark without its participant and lifecycle restrictions. Read the [communication reference](../../docs/concurrency-event-communication.md), [navigation demo](../../docs/event-driven-demo.md) and [channel performance analysis](../../docs/std-channel-performance.md) together.
