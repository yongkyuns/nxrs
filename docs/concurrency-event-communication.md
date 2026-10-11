# Concurrency and event communication architecture

> **Status: architecture baseline proposed for review; implementation qualification
> is pending.** The ownership and queue directions below replace the earlier
> single-physical-inbox default. Public helper APIs can evolve through a small
> qualification fixture. This document alone changes no production behavior.

## Architectural decisions

Keep tightly coupled application processing together. Messages cross meaningful
ownership boundaries, not every processing stage. Normalized hardware delivery
is a legitimate asynchronous boundary even when measurements are periodic.

| Concern | Direction |
| --- | --- |
| Service state | Ordinary owning Rust structs with synchronous helper modules |
| Independent execution | Qualified `std` threads; no mandatory actor trait |
| Device acquisition, waiting, parsing and normalization | HAL/provider, never the service event loop |
| Important traffic | Dedicated bounded queue capacity, independent of ordinary measurements |
| Multiple queues | One receiver owner and one logical blocking selection point |
| Multi-queue implementation | Pinned Crossbeam bounded channels as the preferred qualification candidate |
| Simple single-queue cases | Existing std bounded channels remain valid; no automatic migration |
| Data inside an owner | Direct calls and borrows |
| Bulk data across a justified boundary | Explicit bounded buffers/rings with a tested notification contract |
| Acceptance | Functionality, allocations, latency, linked binary size and RAM all matter |

This is an in-process Rust application model. Neither availability of `std` nor
an upstream crate's desktop support qualifies a NuttX or browser target.

## Ownership and the HAL boundary

The service owns application state, processing policy and HAL session lifecycle.
The provider owns device resources and acquisition/parsing state. Owning a GNSS
session does not mean reading a UART on the service thread.

~~~text
GNSS device -> HAL wait/read/parse/normalize -> typed delivery endpoint
                                                     |
commands / peer events ------------------------------+-> service queues
                                                          |
                                                  one selection point
                                                          |
                                       local calibration / fusion / health
~~~

GNSS HAL hides NMEA versus UBX, serial framing, checksums, partial input,
receiver-specific configuration/recovery and coherent measurement assembly.
Reusable protocol/parser crates may implement that work behind the HAL facade.
The service receives device-independent data, not raw packets or descriptors.

The capability contract defines units, coordinates, measurement versus arrival
time, validity, optional fields, epochs, sequence/gap reporting and source
lifecycle. Missing receiver data must not be fabricated. Product decisions,
such as entering degraded navigation, remain service policy.

A blocking provider may own a worker thread. Existing driver workers, shared
provider loops or callbacks are also valid. There is no mandatory thread per
device or parser, and no additional service whose only job is relaying HAL data.
Provider-internal device waits and cancellation remain behind HAL/target support.
No std/Crossbeam channel operation is assumed ISR-safe; use a qualified deferred
path before invoking a service delivery endpoint.

Application processing remains ordinary `&mut self` methods. Do not default to
`Arc<Mutex<Service>>`; move state to the one execution owner. Thread creation
requires the appropriate `Send + 'static` bounds, not that all private state be
`Sync`. Ordinary `main()` remains the composition root. [Thread builder][thread]

## Typed endpoints without a global event graph

Services expose command/semantic-event enums. HAL capabilities expose normalized
data/status enums. A destination combines only the types it consumes, potentially
in separate enums for separate admission classes. There is no global message enum,
`Any` registry, subscription system or serialized internal wire format.

Inject a narrow source-typed sink into each producer. Application/service wiring
maps, for example, a GNSS solution to the ordinary queue and a terminal provider
fault to an important queue. The producer need not know the destination's private
enums. Classification is explicit policy, not an arbitrary priority number
supplied by every sender.

The sink runs on the producer and only maps/admit events. It never calls the
receiving service's handler, starts a forwarding thread, or adds an intermediate
HAL-output queue. Rejected events remain owned by the caller. Admission means
queued, not processed, acknowledged or durably stored. [Channel operations][cb]

Contract types may remain `no_std`; std-backed delivery support belongs in a
neutral support crate or provider/facade adapter, not a dependency from HAL up
into a service implementation. Multi-instance identity uses local variants or
bounded tags when needed. Explicit fan-out needs a partial-admission policy; a
sender clone is not a broadcast subscription.

## Dedicated capacity, one logical wait point

The baseline for a service needing importance isolation is independently bounded
important and ordinary queues. A dedicated lifecycle-stop queue can additionally
isolate shutdown from an important-event burst. This replaces the earlier rule
of one physical queue for every source.

~~~text
lifecycle owner -> stop queue (reserved for stop) --+
critical commands / terminal faults -> important -+-> one select -> one owner
HAL solutions / ordinary peer events -> ordinary -+
~~~

All senders for a class fan into its queue. Separate capacity is not a reason to
create one queue per producer, device, enum variant or processing module.

Use existing Crossbeam selection instead of writing a selector. Fixed-arm
`select_biased!` gives the earliest ready arm preference. It does not preempt a
running handler, make arbitrary source order deterministic, or guarantee normal
traffic progress during an unbounded important-event stream. [Biased selection][biased]

Illustrative dispatch shape (not the final helper API):

~~~rust,ignore
loop {
    // Check absolute deadlines between dispatches, not only after a timeout.
    service.run_due_work();
    let remaining = service.time_until_next_deadline();
    crossbeam_channel::select_biased! {
        recv(stop_rx) -> request => { handle_stop_request(request); break; },
        recv(important_rx) -> event => handle_important_or_closed(event),
        recv(ordinary_rx) -> event => handle_ordinary_or_closed(event),
        default(remaining) => {},
    }
}
~~~

There is one blocking selection, not sequential blocking receives. Required
channel closure causes explicit failure/teardown; optional closed channels are
disabled. Disconnected channels are ready, so ignoring errors can spin.
A source can fail while other senders keep its class queue alive; source status
must be explicit rather than inferred from whole-channel disconnection. [Selection][select]

Handlers perform bounded work and do not block waiting for another service,
queue capacity, a device response or a worker join. Service-specific deadline and
fairness policies must be tested under sustained traffic. Strict priority is
acceptable only with a justified important-traffic bound or an explicit starvation
policy. Selection preference is not a real-time scheduling guarantee.

Simple services with only one queue may use `recv`/`recv_timeout`. Existing
standard receivers cannot be passed to Crossbeam's selector. Neither path
requires Rust async. There is no generic service-side descriptor reactor.

## Admission and lifecycle contracts

Independent queues prevent ordinary traffic consuming important-event slots;
they do not make a finite important queue incapable of overflowing.

- Ordinary admission returns accepted, full-with-original-event or closed-with-
  original-event. No implicit allocation, unbounded retry or silent discard.
- Each measurement consumer declares its loss/coalescing policy. A display's
  latest-value semantics must not silently become an estimator's loss policy.
- Important full/disconnected outcomes must be retained/retried or escalated by a
  bounded source-specific protocol. Terminal state must remain observable even
  when its event cannot be admitted; do not report overflow only by sending
  another event into an already full queue.
- Shutdown has isolated admission. The initial fixture uses a capacity-one,
  lifecycle-owner-only stop queue. A pending duplicate stop may be treated as
  already requested; normal traffic can never occupy that slot. Disconnect and
  completion remain distinct from successful admission.

Source rates, burst bounds, capacities, permitted retry storage and overflow
visibility are part of the resource contract. HAL acquisition must not block
indefinitely on the consumer and become unable to drain hardware or cancel.

Lifecycle meanings are separate:

| Milestone | Meaning |
| --- | --- |
| Start accepted | Session exists and initialization has begun |
| Ready | Provider configured and capable of acquisition; not necessarily a valid GNSS fix |
| Operation accepted | Request admitted; later completion is explicit where needed |
| Stop requested | Cancellation initiated without blocking normal service dispatch |
| Provider stopped | Acquisition/callbacks quiescent; no further sink invocations |
| Joined | Worker execution has terminated and its resources are reclaimed |

Create queues and install sinks before enabling production. Roll back partial
starts. Provider stop must wake a blocked device wait. Quiescence does not remove
already queued events: the session contract defines draining/discard and, when
restart is supported, how old-session data is rejected.

Do not join a producer that still needs the joining service to drain a full
queue. The single-wait rule applies to normal dispatch; explicit joins belong to
teardown after progress dependencies are resolved. Do not hide a blocking join
in `Drop`. A retained HAL session whose worker retains a sender cannot rely on
automatic inbox closure to start shutdown. [Join semantics][join]

## Allocation policy: std and Crossbeam use the same acceptance test

Use positive-capacity bounded channels with a fixed set of participants and
selection arms. Do not create channels/selectors/threads during normal dispatch.
Prefer fixed-arm macros to constructing a dynamic `Select` repeatedly.

Neither `std::sync::mpsc::sync_channel` nor Crossbeam's bounded implementation
promises that all blocking bookkeeping is allocated at construction. Both have
an internal blocking context and waiter bookkeeping; the std implementation
was derived from Crossbeam. Fixed message storage does not prove allocation-free
waiting. Conversely, bookkeeping that is allocated once and reused is not the
same as allocation per message. Audit the pinned sources and measure both.
[Std implementation][std-implementation], [Crossbeam source][cb-source]

Record construction, first blocking use, repeated ready-path operations,
repeated blocking/timeouts, overload and teardown separately. First-blocking
cases run on fresh threads/processes so earlier tests cannot hide lazy setup.
Initialization and every participant's prescribed first-use setup must be
explicit. Target acceptance is documented bounded initialization and no
unbudgeted steady-state allocator activity; measured exceptions must be reported,
not normalized away by calling arbitrary extra warm-up iterations.

Reuse `tests/allocation-probe`. Rust allocator hooks do not account for all
libc/OS heap usage or task stacks, and instrumented timings are not performance
measurements. Keep allocation, timing and linked-footprint artifacts separate.
Crossbeam is preferred for its required selection functionality, not yet claimed
qualified or superior on a particular target. Do not write a replacement mailbox
unless qualification identifies a concrete unmet requirement.

## Binary footprint is a first-class acceptance criterion

Measure the final linked executable/firmware, not `.rlib` sizes or the sum of
crate archives. Adding a dependency and using a dependency are different cases;
dead-code elimination, LTO, panic handling and shared runtime paths affect the
marginal linked cost. [Cargo profiles][profiles]

Required comparison ladder:

| Variant | Purpose |
| --- | --- |
| Minimal C with matched kernel configuration | Existing OS/application baseline |
| Minimal Rust core/no_std with comparable entry, where buildable | Separates Rust core/entry from std startup effects |
| Minimal ordinary Rust std | Whole-image incremental std/entry/runtime cost |
| Std plus one thread/join workload | Incremental threading use |
| Std bounded channel workload | Standard-channel baseline |
| Crossbeam bounded channel with the same workload | Dependency substitution delta, not a different workload |
| Crossbeam separate queues plus selection | Incremental selection/queue-isolation cost |
| Std channel and Crossbeam used together | Measures coexistence/possible duplicated implementations during migration |
| Future thin nxrs helper with the same workload | Measures framework overhead after extraction, not before it exists |

Use the same target, source workload/payload, compiler/SDK and library versions,
resolved NuttX settings, stack configuration, optimization, LTO, panic strategy,
linker/GC options, instrumentation state and enabled features for matched pairs.
Only explicitly enumerated application-selection differences may be ignored in
kernel-config comparisons. Reject mismatches rather than print misleading deltas.

Report bytes and incremental deltas for text/read-only code/data, initialized
data, BSS, `text + data` (flash-like proxy), `data + BSS` (static-RAM proxy), and
actual loadable/flash image size where available. Keep full ELF file size separate
from those metrics: debug/symbol metadata is not device flash. Account separately
for alignment/gaps, stacks, heap peaks and reserved pools. Keep maps/section tables,
symbol evidence, build commands, resolved configs, lockfile/feature identities and
artifact hashes. A Rust no_std baseline must demonstrate absence of std runtime
symbols; a label alone is not evidence.

C versus minimal std is a whole-image deployment delta, not a language-intrinsic
constant. No_std versus std includes their entry/panic choices unless separately
controlled. Host dynamically linked binaries are diagnostics, not MCU flash
estimates. Firmware on the actual target is the adoption gate. Reuse the existing
matched C/std footprint pipeline under `tests/rtos-bench` rather than replace it.

Record dependency features and transitive dependencies. Repeat the relevant
matched rows when adding a runtime dependency; inspect coexistence before claiming
that replacing std channels necessarily reduces size. Size, memory and latency
tradeoffs go into the review, not an unsupported claim of zero-cost abstraction.

## Bulk data remains a separate concern

Keep local samples/frames borrowed within one service. Across a justified
boundary use an explicitly sized pool/ring or preallocated owning buffers.
Moving a `Box<[u8]>` does not deep-copy bytes; immutable `Arc` is for actual
simultaneous readers, not unrestricted mutation. DMA/cache/completion constraints
remain provider responsibilities.

Generic pool/notification machinery is not part of the first shared crate.
Path-specific implementations must test rejected availability notifications,
partial draining, re-notification and stop/restart. Pending data cannot become
permanently invisible after a full inbox rejects its wake event. All notification
handling remains integrated into the service's one selection point.

## Implementation sequence and qualification boundaries

1. Qualify raw std versus pinned Crossbeam with a synthetic HAL producer and
   separate stop/important/ordinary queues; use existing allocation instrumentation.
2. Add matched linked-size variants and comparison guards, then run the selected
   native and NuttX builds. Keep each result tied to exact source/toolchain/config.
3. Extract only repeated endpoint, dispatch and lifecycle machinery into a small
   neutral crate after the fixture passes. No actor runtime, custom queue algorithm,
   serializer, global enum, service graph or service-side device poller.
4. Integrate one event-producing HAL and coarse processing service; qualify provider
   cancellation and data normalization before broader migration.

The current `hal/gnss/src/api.rs` still has synchronous `Gnss::fix`; the event-producing
session described here is not yet implemented. The existing event-demo topology
remains portability/stress evidence, not the required product decomposition.
See [architecture.md](architecture.md) and [event-driven-demo.md](event-driven-demo.md).

Tests must cover typed multi-source delivery, isolated capacity, important-full
outcomes, biased selection and non-preemption, deadlines under sustained traffic,
source failure while peers remain alive, full-queue stop, partial-start rollback,
blocked device-read cancellation, callback quiescence, allocation phases and
mismatch-rejecting footprint reports. A fixture's synthetic cancellation is not
qualification of a real UART/driver cancellation implementation.

Crate names, sink-wrapper syntax and concrete GNSS fields are implementation
choices. Target cancellation support, fairness limits and measured resource
budgets are acceptance work. The architecture need not be reopened to choose them.

## References

References describe mechanisms, not nxrs target qualification. Pin dependencies
and toolchains in the executable qualification change.

[thread]: https://doc.rust-lang.org/std/thread/struct.Builder.html
[join]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html
[cb]: https://docs.rs/crossbeam-channel/0.5.17/crossbeam_channel/
[biased]: https://docs.rs/crossbeam-channel/0.5.17/crossbeam_channel/macro.select_biased.html
[select]: https://docs.rs/crossbeam-channel/0.5.17/crossbeam_channel/macro.select.html
[cb-source]: https://docs.rs/crossbeam-channel/0.5.17/src/crossbeam_channel/lib.rs.html
[std-implementation]: https://doc.rust-lang.org/src/std/sync/mpmc/mod.rs.html
[profiles]: https://doc.rust-lang.org/cargo/reference/profiles.html
