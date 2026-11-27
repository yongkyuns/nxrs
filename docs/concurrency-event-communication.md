# Concurrency and event communication architecture

> **Status:** the acquisition/ownership principles are requirements. Platform
> implementations still require target ABI and physical qualification; channel
> allocation, timing and footprint claims require measurements.

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
| Latest observations | Fixed exclusive publishers/readers; explicit coalescing, freshness and retained faults |
| Ordered capture | Preserve shared admission, overflow identity and drain/close semantics before changing storage |
| Data inside an owner | Direct calls and borrows |
| Nested shared task locks | Optional std-backed `shared/lock-order` with a declared strict rank order |
| Bulk data across a justified boundary | Explicit bounded buffers/rings with a tested notification contract |
| Acceptance | Functionality, allocations, latency, linked binary size and RAM all matter |

This is an in-process Rust application model. Neither availability of `std` nor
an upstream crate's desktop support qualifies a NuttX or browser target.

The [Wi-Fi scan owner contract](../tests/nuttx-std/wifi/CONCURRENCY.md) applies
these rules to an existing owner: execution contexts, queue capacities, wait
dependencies, deadline meaning, and shutdown boundaries. It separates source
invariants from the timing and resource limits a product still has to qualify.

The [std channel performance analysis](std-channel-performance.md) connects
these requirements to the bounded inbox implementation and its reported
ESP32-S3 timing and memory observations. It distinguishes many producers sharing
one inbox from selection across independently bounded queues.

## Ownership and the HAL boundary

The service owns application state, processing policy and HAL session lifecycle.
The provider owns device resources and acquisition/parsing state. Owning a GNSS
session does not mean reading a UART on the service thread.

<figure class="architecture-art">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/nxrs-acquisition-boundary-dark.svg">
    <img src="assets/nxrs-acquisition-boundary.svg" alt="HAL acquisition waits, reads, parses and normalizes device data. A typed endpoint admits it to bounded service queues, where one owner selects inputs and runs local processing." loading="lazy">
  </picture>
  <figcaption>For GNSS, the provider owns UART parsing and coherent measurement assembly. Calibration, fusion and health policy run with the receiving service state. <a href="assets/nxrs-acquisition-boundary.svg">Open SVG</a>.</figcaption>
</figure>

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

Production acquisition is signal-driven: IMU IRQs or explicitly configured
ODR-derived timer wakes, and GNSS UART readiness, wake HAL-owned acquisition.
Services never poll devices on sampling timers. For an unwired IMU, a HAL timer
feeds the same FIFO drain/normalization path as an IRQ. It is only a wake hint;
actual FIFO contents admit samples. Missed ticks coalesce instead of generating
backfilled measurements. A configured IRQ failure never silently selects a timer.
ISR callbacks only use qualified OS notification; parsing, FIFO/UART I/O and
bounded sink admission execute in task context. Queue-full measurements follow
an explicit loss policy with observable counts/sequence gaps; terminal faults
remain available through the session result even if notification is rejected.

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

`shared/messaging` owns generic delivery results and the bounded `Sender<T>` /
`Inbox<T>` wrapper. It has no HAL/service dependencies. Its default `channels`
capability supplies std channels; HAL contracts disable that capability for
core-only builds. Producers accept ordinary `FnMut(T) -> DeliveryResult<T>`
callbacks; no sink trait or forwarding worker is needed. Multi-instance identity
uses local variants or bounded tags when needed. Explicit fan-out needs a partial-admission policy; a
sender clone is not a broadcast subscription.

### Requests that leave the owner responsive

While external work is pending, its requesting owner must continue handling its
other permitted inputs. Retain the operation state and receive completion through
the existing dispatch/wake path. A synchronous reply-channel wait inside a
handler introduces a second wait dependency even if the destination itself has
one well-structured event loop. Blocking sends, worker joins and destructors that
join need the same review.

The [navigation demonstration](event-driven-demo.md#control-without-suspending-observation)
uses startup-allocated reply storage, one outstanding request per sensor handle,
and a wake callback shared with fusion output. Completion is retained before
notification. Full wake admission therefore does not lose the result. Consuming
the reply releases the outstanding slot; a caller's expired deadline does not.
The private reply channel scopes request identity to the service lifetime, and
stale wake hints carry no response data. Existing blocking convenience calls
remain available for explicitly blocking callers.

Keep callback behavior bounded and in its declared context. A producer callback
may publish owned data or a wake hint; it must not call the recipient's mutable
handler. Source/API checks prevent particular misuse, but do not prove that
arbitrary Rust, dependency or foreign code cannot block. Qualify the complete
admission, publication and native wake path on the selected scheduler/backend.
This follows the event-loop discipline described by
[Cummings, Managing Concurrency in Complex Embedded Systems](https://www.state-machine.com/doc/Cummings1006.pdf).

### Notification and retention

Specify what a wakeup tells the reader and what remains available to read. The distinction applies at different layers, from driver buffers to application middleware.

| Example | Contract to examine |
| --- | --- |
| NuttX buffered sensor driver | Interrupt or polling acquisition pushes records into the driver's upper-half ring buffer. The reader retrieves buffered events through the device interface. [NuttX sensor drivers](https://nuttx.apache.org/docs/latest/os/drivers/special/sensors/sensors_uorb.html#programming-modes) |
| Zephyr zbus | A subscriber receives a channel reference and reads its current value. A message subscriber receives a copy, while an inline listener runs in the publisher's context. [Zephyr zbus observers](https://docs.zephyrproject.org/latest/services/zbus/index.html#concepts) |
| PX4 uORB | A topic in the flight stack retains one message by default. Configured deeper queues retain more history, and a slow subscriber can still lose messages. [PX4 uORB buffer length](https://docs.px4.io/main/en/middleware/uorb#uorb-buffer-length-orb_queue_length) |

For NXRS, decide per consumer whether it needs current state or ordered history. If one wake can cover several retained records, define how a bounded drain will resume while unread records remain. Check freshness for latest-state readers and use sequence numbers or explicit loss reports for readers that need history.

### Motion Tracker observations and capture

Motion Tracker's acquisition callbacks publish directly through navigation's
fixed latest-value endpoints. Construction creates two independent readers
before opening any acquisition session: one belongs to the display owner
(including USB and satellite rendering), and one belongs to communications.
Each `FnMut` callback exclusively owns its source publisher. The GNSS publisher
owns separate fix and satellite streams. No worker is added.

The optional `latest` feature in `shared/messaging` wraps pinned `triple_buffer`
9.0.0 with non-cloneable, non-`Sync` endpoints and `Copy + Send` payloads.
Publication and reading require mutable ownership. Each source has one
triple-buffer state per reader; two IMUs, GNSS fixes and satellite reports use
eight preallocated states, with three payload slots per state. Atomic metadata,
cache padding, endpoint arrays and reference counts add to the payload budget.
Neither operation allocates, deallocates, invokes user `Clone`/`Drop`, or waits
for a peer. Endpoint teardown may deallocate. Target atomic lowering still
determines the supported progress guarantee; this API is not a hardware
wait-free or ISR-safety claim.

An independent latest reader starts in `Waiting`, then sees a complete
committed sample with its original reception time. Later samples may replace
unread ones. IMU readings expire at 500 ms, GNSS fixes at 3 s and satellite
reports after two receiver intervals with a six-second minimum. Faults stay
terminal for the acquisition lifetime. Per-source mailboxes permit skew
between sources and between readers, so their consumers must tolerate it.

Navigation owns a separate shared set of monotonic atomic fault flags. Callback
faults, failed opens and joined-session reconciliation set those flags without
acquiring a payload publisher. Readers check the flag after copying the payload;
a late sample cannot clear a fault. Sensor notifications in the display inbox
are wake hints, so full or missed notifications do not hide terminal state. A published
`ObservationService` cannot call the local service's `observe` method. A new
acquisition lifetime requires new endpoints and fault state.

Communications keeps its fixed reader while inactive. Startup transfers it
through the existing control state; failed thread creation restores it for a
later attempt. The running worker reads outside that control mutex. Exit or
unwind returns the endpoint, and it can be recovered only after joining.
This permits explicit reuse without adding subscribers. It does not add an
automatic restart policy for a communications worker that has exited.

The [owner qualification fixture](../tests/owner-qualification/README.md)
keeps the original raw-buffer experiment and adds `publication-progress`, which
calls the actual navigation API. Its separate `publication` suite uses the
normal PI-enabled mutex default and runs the ordinary mutex donation control
in the same image. It checks complete reads while a low-priority publisher is
paused, publication while a low-priority reader is paused, original timestamps,
freshness, independent sources and sticky faults. Pauses are between production
API calls; they do not inject preemption into an atomic instruction. The same
image compares two IMU updates and two snapshots for 4,096 rounds, with setup,
first-use, steady-state and teardown Rust allocation windows. Host lifecycle
tests additionally cover device-open failure, a full terminal wake inbox,
failed communications spawn and joined reuse.

Product scheduling, actual target stack high-water, physical acquisition jitter
and worst-case latency remain qualification gates. Freshness thresholds describe
which readings are usable; timing acceptance also needs a publication deadline
and an acquisition jitter budget.

Ordered capture belongs to Motion Tracker's export. The app uses the optional
[`messaging::pooled`](../shared/messaging/src/pooled.rs) transport: 512 shared
scalar records, three exclusive source publishers and one exclusive consumer.
Each acquisition callback receives its own publisher. The consumer moves with
`Telemetry` into the existing communications worker, through failed-spawn
rollback and joined reuse. A separate shared view carries clock, scales and
fault state. No payload mutex, new worker or runtime dependency is added.

The pool's lifecycle gate reserves capacity, epoch and a source's outstanding
publication in one atomic operation. A source paused after reservation cannot
prevent another source from publishing or the owner from reading its complete
records. The consumer checks at most three index lanes, preserving FIFO within
each source. Cross-source arrival order is not retained. All three wire versions
track deltas by source and accept this interleaving.

Closing an export cancels admission and drains published records on the owner.
A producer can still owe a publication after the visible queue becomes empty;
that obligation prevents terminal completion and a new epoch. The owner keeps
retirement in its existing 15 ms bounded poll even after the radio disappears.
A reconnect retains its request while retirement is pending. It does not block
communications or overwrite old work. Overflow retains the first missing source
and sequence until accepted records and source obligations retire. Unexpected
publisher disconnection closes with an error after accepted records drain; it
cannot invent an overflow key or become a successful export.

The radio's export session keeps one fixed 604-byte encoded frame while TLS
writes accept only part of it. Pool slots return after copying and before
encoding or transport I/O. Full batches contain up to 16 records; smaller ones
wait for the existing 40 ms v1/v2 or 75 ms v3 deadline. An overflow terminal stays
pending until its complete bytes have been written. Partial requests, writes,
`WouldBlock`, disconnect and reconnect use the same session code in the app and
its host behavioral tests.

The wake destination can be replaced after the previous communications worker
joins. Registration prewarms the owner mutex and retires the old thread handle
outside it. Acquisition retains readiness or fault state before a `try_lock`
wake attempt; it does not wait for this lock or clone/retire thread handles.
Starting idle work has its own control wake, and active work or pending
retirement contributes a deadline to the owner's single park. The scalar payload
path avoids a mutex, but the callback and platform wake primitive are not a
whole-system lock-free or deadline guarantee.

The [telemetry service's capture admission](../service/telemetry/src/export/policy.rs)
checks the application's storage and workload configuration before sensor startup.
[Motion Tracker's configuration](../app/motion-tracker/src/communications.rs)
supplies the following product budget:

| Policy | Enforcement and scope |
| --- | --- |
| Shared capacity | Exactly 512 records; any one source can use all slots |
| Retained pool heap | At most 22 KiB of requested Rust allocations, including pool control storage; checked before allocation |
| Idle lifetime | The arena remains allocated until final endpoint teardown, including between BLE sessions and joined worker reuse |
| Runs without export | A headless run without `--ble` reserves no pool; display runs reserve for later pairing |
| Stall and burst | Two seconds at the resolved IMU rates, 2 Hz GNSS and 64 additional burst records must fit 512 slots |
| Nominal workload | Two 100 Hz IMUs and 2 Hz GNSS need 468 slots including the burst allowance, leaving 44 slots |
| GNSS rate | 2 Hz is the current product-profile assumption; the portable device API does not expose a rate query |
| Excess traffic | First true capacity failure closes the epoch and retains its exact missing key |
| Epoch lifetime | 131,071 successful opens per acquisition lifetime, then an explicit exhaustion error; no wrap or silent stale reuse |

The four large scalar/index allocations use fallible reservation. Small shared
`Arc` allocations retain Rust's ordinary allocation-failure behavior, before
acquisition starts. Pool requested bytes exclude allocator bookkeeping, the
separate wake/clock object, transport buffers and task stacks. Firmware heap and
stack measurements must account for those costs separately. Keeping a fixed arena
avoids replacing shared storage while callbacks still own endpoints; releasing
it on every BLE disconnect would require a separate quiescent endpoint handoff.

The [host capture comparison](../tests/mpsc-comparison/README.md#host-capture-admission-comparison)
uses the production pool and encoder with an independent wire oracle. A nominal
two-second, 404-record stalled-consumer workload fits shared capacity but an
evenly partitioned SPSC design admits only 346 records. A rate-weighted split
admits all 404, then only 406 of a three-second, 606-record workload while 106
slots remain unused elsewhere. The shared pool admits 512. Partitioning would
change the product's admission behavior.

The same production gate serves the independent non-Copy desktop MPSC reference
through the explicit `probe` feature. Its 24 lifecycle cases and the
[22 pooled controls](../tests/mpsc-comparison/README.md#shared-pooled-payload-transport)
cover admission, retained terminal state, stale epochs, cancellation, unwind,
last-source races and complete per-source records. Public pause hooks are absent
from normal builds. The host allocation harness checks budget rejection before
allocation, setup, first use, steady publication, retained inactive storage and
final teardown. These checks do not turn arbitrary compare-and-exchange retries
into a per-call SMP bound.

Publication currently requests a wake for every accepted record, whereas the
previous queue woke when reaching its batch threshold. Batching remains on its
existing deadlines, but the extra wake load needs target measurement. Stopping
only communications joins that worker and can return an endpoint with pending
acquisition obligations; a later owner continues retirement before reopening.
Full product shutdown stops and joins acquisition before retiring communications.

Historical fixture measurements remain source-specific. The setup at `60ee9f07`
requested 21,712 B in five allocations, retained while inactive; its first use
and 4,096 steady round trips made no Rust allocator calls. The raw components
were 18,432 B payload, 3,072 B indices, 64 B bitmap and 24 B cursors, already
included in that total. The earlier mutex implementation released its 20,480 B
payload allocation on finish. Different allocation scope and lifetime prevent
calling their subtraction a complete product heap delta.

At `60ee9f07`, native build `8c3b0380` passed the
[single-core fixed-priority capture witness](../tests/owner-qualification/README.md#pooled-capture-transport-witness)
and its same-image std-mutex inheritance control, with independent replay. That
receipt predates promotion and the current owner/transport integration; it is
not a new-source product qualification. Deployed-target atomics, scheduler and
wake behavior, active/idle heap, stack high-water and physical timing still need
matching evidence. The implementation and qualification case are tracked in
[#446](https://github.com/yongkyuns/nxrs/issues/446).

#### ARM product build comparison

Retained `mps2-an521-mock` builds of the actual Motion Tracker application
compare parent `66c57797`, including the fixed-reader implementation
from #451, with reusable publication revision `21b7cafa`. Both use the same
qualified frontend, pinned ARM compiler, effective Kconfig, SDK patch receipt,
ARMv8-M SysTick correction, linker script and normal PI-enabled mutex policy.
Both builds pass the native
ABI gate, with identical results for all 126 checked values within that gate's
documented scope.

| Linked product measurement | Parent `66c57797` with #451 | Reusable API `21b7cafa` | Change |
| --- | ---: | ---: | ---: |
| File-backed allocated ELF sections | 428,154 B | 429,978 B | +1,824 B |
| Unpadded `nuttx.bin` | 428,156 B | 429,980 B | +1,824 B |
| Static SRAM sections | 10,900 B | 10,900 B | 0 B |

The binary includes two bytes of flash alignment; the SRAM address span is
10,908 bytes in both builds, including eight bytes between sections. ELF debug
file size is excluded. Dynamic publication storage, native heap and task stacks
are separate from static SRAM. The parent uses seven mailboxes with display-only
satellites; the reusable API provides satellites to both readers and uses eight.
Their payload layouts differ, so mailbox counts alone do not establish the net
runtime heap difference.

The later entry-task correction raises the `motion_tracker` reservation from
14,848 B to 16,384 B, adding 1,536 B of requested task stack. This cost is
separate from the retained 10,900 B static-SRAM measurements and was not present
in those two measured images. The [coexistence fixture](../tests/nuttx-std/tracker-sim/README.md)
records the later export run at `3e841457`: 3,448 of 15,864 usable bytes remained
free (21.7%). Its separate map startup failed before the first frame; that
export result does not qualify the map path or subsequent source changes.

Both images completed two sequential four-frame runs in a fresh MPS2 QEMU
8.2.7 boot using `motion_tracker --interval-ms 100 --frames 4 --stream-usb --ble`.
Each run returned zero, with both IMUs and GNSS live in its final USB frame.
This exercises startup, the existing optimized 23 KiB communications task,
joined shutdown and sensor reopening. Unbound TLS pairing follows its ordinary
failure path. Active BLE snapshot consumption, physical execution and stack
high-water remain outside this smoke test.

This ARM mock profile has unbound display and radio capabilities; it does not
qualify the ESP32/CoreS3 product footprint or timing. Source inputs and final
artifact hashes were checked before and after retiring reproducible build
caches. Reproduce each selected revision through the normal frontend:

```sh
cargo nxrs build --app motion-tracker --profile mps2-an521-mock \
  --out target/tracker-publication-comparison
```

#### Earlier ARM mutex comparison

The earlier matched comparison used the PI-enabled mutex product at `204f4297`
and publication revision `94e1f9ea`, before #451 was incorporated into the
parent branch. It measured the original mutex-to-fixed-reader change:

| Linked product measurement | Mutex baseline | Fixed-reader implementation | Change |
| --- | ---: | ---: | ---: |
| File-backed allocated ELF sections | 425,298 B | 429,842 B | +4,544 B |
| Unpadded `nuttx.bin` | 425,300 B | 429,844 B | +4,544 B |
| Static SRAM sections | 10,900 B | 10,900 B | 0 B |

Those images also passed the two-run headless product smoke under their matched
SDK and configuration. These historical figures describe their own source pair;
the retained `66c57797`/`21b7cafa` comparison above measured a +1,824 B increment.
Subsequent profile and task-stack changes require new build results for their
actual revisions.

#### CoreS3 product-path baseline

A physical CoreS3 and M135 ran the `m5stack-cores3-dual-imu` product profile
at `283e7597` with temporary timing probes. The same image used the live
display, both 100 Hz IMUs, GNSS, and authenticated BLE v3 export in each case.
The preemption case made a priority-95 task runnable for 120 ms while the
priority-90 display held the observation mutex. NuttX reported both
priority-100 IMU tasks waiting on a semaphore inside observation publication.
The BLE peer withheld cumulative acknowledgments independently. The four main
cases observed export for 10 seconds each, with a second run for the control
and combined cases.

| Load | Longest IMU callback | Longest BLE batch gap | Peak capture depth |
| --- | ---: | ---: | ---: |
| Control | 0.81–3.05 ms | 0.121–0.123 s | 17–19 |
| Display preemption | 121.67 ms | 0.137 s | 20 |
| 0.95 s BLE acknowledgment hold | 1.60 ms | 1.043 s | 152 |
| Preemption and BLE hold | 119.04–119.58 ms | 0.816–0.877 s | 137–138 |

Observation publication accounted for the forced delay. The ordered capture
step took at most 2.86 ms. The peer saw no sequence gaps in the received
prefixes. With acknowledgments held for 7 seconds, the 512-record pool filled.
The peer received all 1,093 admitted records and a loss marker for IMU 1
sequence 788, immediately after that source's record 787.

An IMU stopped early in the control as well as under load. Runs that recorded
the cause reported a sensor timeout. Qualifying sustained acquisition requires
isolating the timeout and checking hardware FIFO continuity.

These product-path measurements predate the normal PI-enabled mutex policy
and the fixed-reader integration. They identify a shared observation-lock wait;
they do not establish the speedup from replacing a PI-enabled mutex. Keep the
historical image and its results separate from the current-source publication
suite and future physical product captures.

#### CoreS3 fixed-reader comparison

The implementation merged in [#451](https://github.com/yongkyuns/nxrs/pull/451)
used seven mailboxes, with satellite reports available only to the display.
These measurements predate the normal PI-enabled policy and the reusable
`messaging::latest` integration described above.

With the original profile priorities, the display retained an IMU reader
slot through a 120 ms forced pause. Both IMUs kept publishing, with 15 and 12
publications during the pause, and the longest callback was 2.41 ms. In a
combined repeat with BLE acknowledgments held for 0.95 s, the IMUs published
11 and 12 samples during the pause and the longest callback was 2.50 ms. Both
IMUs and GNSS stayed live through that 25-second run.

A 0.95 s acknowledgment hold alone raised queue depth to 148 records with
contiguous received sequences. A 7 s hold filled the 512-record queue. The peer
received all 1,099 admitted records followed by the first-loss marker.

The BMI270 timeout also occurred with fixed readers after three FIFO snapshots
crossed sensor sampling boundaries. A CoreS3 trace separated the bus lock,
controller interrupts and dispatch after the interrupt. In one 12.50 ms read,
the controller phases took 0.52 ms and the acquisition worker waited another
11.90 ms to resume. The bus lock took 8 µs. Dispatch dominated 106 of the
first 112 retained slow reads, and `tracker-links` was the most frequent
running task at completion.

The dual-IMU profile gives communications priority 99 and keeps acquisition
at 100. The physical control in [#461](https://github.com/yongkyuns/nxrs/pull/461),
head `c50d939a` based on `5511a6c`, still used seven fixed-reader mailboxes before
the new PI policy and reusable publication integration. With the Mac BLE peer, the longest retained
post-interrupt delay fell to 4.38 ms. Both IMUs maintained continuous 100 Hz
timestamps in the control, forced display pause, 0.95 s acknowledgment hold,
and both combined runs. During the 7 s acknowledgment hold, both IMUs kept
sampling and the peer received all 1,103 admitted records before the expected
overflow marker.

The dual-IMU profile also runs NuttX LPWORK at 99 because it dispatches BLE
callbacks. The physical comparison in [#467](https://github.com/yongkyuns/nxrs/pull/467)
recorded 71 I²C transfers of at least 1.8 ms across the matched Mac control and
combined-load runs, down from 257 with LPWORK at 100. These remain measurements
of that fixed-reader product configuration.

The matched fixed-reader build added 3,348 bytes of linked flash and 104 bytes to the boot
image. Publication setup used 3,720 bytes of heap versus 480 bytes. Static RAM
and task stack reservations were identical.

## Dedicated capacity, one logical wait point

The baseline for a service needing importance isolation is independently bounded
important and ordinary queues. A dedicated lifecycle-stop queue can additionally
isolate shutdown from an important-event burst. This replaces the earlier rule
of one physical queue for every source.

<figure class="architecture-art">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/nxrs-priority-inboxes-dark.svg">
    <img src="assets/nxrs-priority-inboxes.svg" alt="A reserved stop queue, an important queue for critical commands and terminal faults, and an ordinary queue for HAL data and peer events feed one selection point and one owner." loading="lazy">
  </picture>
  <figcaption>Separate capacity isolates admission classes. Selection preference does not preempt a handler or establish fairness by itself. <a href="assets/nxrs-priority-inboxes.svg">Open SVG</a>.</figcaption>
</figure>

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

Assign task priorities using service deadlines and dependencies. A sensor
drain should preempt bulk export and rendering when the device buffer requires
it. Include the driver work that makes the acquisition task runnable. For
example, GNSS UART service follows receive-buffer headroom at the configured
baud rate. The fix cadence only describes when fixes are published. Verify
effective priorities and measure worker wake latency and sample loss with slow
or stalled consumers.

Lifecycle meanings are separate:

| Milestone | Meaning |
| --- | --- |
| Start accepted | Session exists and initialization has begun |
| Ready | Provider configured and capable of acquisition; not necessarily a valid GNSS fix |
| Operation accepted | Request admitted; later completion is explicit where needed |
| Stop requested | Cancellation initiated without blocking normal service dispatch |
| Acquisition stopped | Native acquisition has ended; apply the capability's callback-retirement contract |
| Callbacks quiescent | No remaining callback or sink invocation can access its captured resources |
| Joined | Worker execution has terminated and its resources are reclaimed |

Create queues and install sinks before enabling production. Roll back partial
starts. Provider stop must wake a blocked device wait. Quiescence does not remove
already queued events: the session contract defines draining/discard and, when
restart is supported, how old-session data is rejected.

For Wi-Fi, `ProviderStatus::Stopped` records successful native cleanup before
the final status sink runs. Retain sink-owned resources until successful
`Wifi::finish()` establishes callback retirement and worker joining. Publishing
the terminal snapshot before its wake preserves reconciliation when the final
event is rejected. Status observation alone is not permission to reclaim the
sink. The worker releases its pending-state mutex before external wake and final
notification calls.

Do not join a producer that still needs the joining service to drain a full
queue. The single-wait rule applies to normal dispatch; explicit joins belong to
teardown after progress dependencies are resolved. Do not hide a blocking join
in `Drop`. A retained HAL session whose worker retains a sender cannot rely on
automatic inbox closure to start shutdown. [Join semantics][join]

## Compile-time ordering for shared locks

Keep state with its execution owner when possible. When an operation must hold
several shared task-context mutexes, `shared/lock-order` provides an optional,
dependency-free `nxrs-lock-order` crate, imported as `lock_order`. It wraps the
ordinary std mutex; it does not add a thread, queue, executor or async API.
Existing locks are not automatically migrated or covered by its checks.

Declare one consistent acquisition order with type aliases. `Next<Unlocked>`
is the first rank, `Next<State>` is the next rank, and further ranks follow the
same pattern. The relation is strict and transitive: callers may skip ranks,
but cannot acquire an equal or lower rank while holding a higher one. Two
different mutex values at the same rank cannot be held together. Rank aliases
describe positions in this order, not independently extensible lock classes;
downstream code cannot implement additional ranks or ordering edges.

```rust
use lock_order::{scope, Mutex, Next, Unlocked};

type State = Next<Unlocked>;
type Statistics = Next<State>;

let state = Mutex::<_, State>::new(Vec::<u32>::new());
let statistics = Mutex::<_, Statistics>::new(0_u32);
scope(|root| {
    state.with_lock(root, |held, samples| {
        samples.push(42);
        statistics.with_lock(held, |_, count| *count += 1)
    })
})
.expect("one root scope per thread")
.expect("state is not poisoned")
.expect("statistics are not poisoned");
```

Pass the current `&mut LockContext<Level>` to every helper that acquires these
locks. Helpers can use `Level: Before<RequiredRank>` instead of naming every
caller rank. Each acquisition exclusively borrows its parent context for the
entire call, so a closure cannot bypass its current rank by reusing an earlier
context. The native guard stays private inside `with_lock`; only borrowed data
and a borrowed higher-rank context reach the closure. Their lifetimes prevent
them from escaping, including through a returned future. There is no public
guard to retain with `mem::forget` and no unranked native-mutex accessor.
Exclusive ownership still permits `get_mut` and `into_inner` without locking.

Start `scope` at an ordinary thread entry or at the outer boundary of a bounded
synchronous operation, then pass its context through the call tree. Contexts
cannot be constructed, cloned, sent or shared across threads. Each thread has
an independent root. A nested call to `scope`, including one from a callback,
returns `ScopeError::AlreadyActive` before invoking its closure. This small
thread-local runtime check closes the fresh-root bypass; root uniqueness is
not a compile-time claim. Its flag clears on normal return and unwinding.
Poisoned mutexes return `Poisoned` without running the data closure or silently
recovering state. Exclusive owners can explicitly inspect std's poison result
through `get_mut` or `into_inner`.

The compile-time contract excludes lock-order cycles among acquisitions made
through this API. It does not establish global deadlock freedom: channel waits,
joins, callbacks waiting for other threads, unmanaged locks and foreign code
can still form cycles. Do not hold these locks across rendering, device I/O,
queue waits or worker joins. Acquisition remains std's blocking operation;
fairness, bounded waiting, ISR safety and timing are separate qualification
work. Priority inheritance comes from the target's normal std backend and
[NuttX configuration policy](build-system.md). It limits one scheduling source
of inversion; it does not replace lock ordering or prove a deadline.

Run `cargo test --locked -p nxrs-lock-order` for real downstream `cargo check`
controls covering reversed/equal ranks, reacquisition, parent-context reuse,
escaping borrows, thread transfer, forged ranks/contexts and native access.
The same command checks valid nested/helper calls, independent ordinary
threads, callback reentry, poisoning and unwind cleanup, and compiles the
example above. The crate is host-checked with Rust 1.90 and the pinned NuttX
nightly. Target TLS initialization, native mutex initialization, allocations
and linked footprint still require qualification before production adoption.

## Memory budgets for deadline-sensitive work

Reserve bounded scratch, queue and buffer storage during construction or session
setup. The owning component supplies and reuses it through the normal API.
Keep capacity and exhaustion behavior explicit. Reject excess work or apply
bounded backpressure using the capability's existing policy.

On fixed-priority targets, qualify steady-state code for zero calls to the
shared heap, including frees, first-use work, error paths and native SDK calls.
Measure setup, reconfiguration and teardown separately. Exercise the full
supported inputs when adding a reserve. Use priority inheritance for shared
task-context locks where supported and qualify the resulting wait times.


Use positive-capacity bounded channels with a fixed set of participants and
selection arms. Do not create channels/selectors/threads during normal dispatch.
Prefer fixed-arm macros to constructing a dynamic `Select` repeatedly.

Neither `std::sync::mpsc::sync_channel` nor Crossbeam's bounded API promises an
allocation-free lifecycle. Allocation behavior depends on the selected source:
the [generic std implementation][std-implementation] and
[Crossbeam][cb-source] have their own blocking bookkeeping, while nxrs's pinned
NuttX SDK uses the [bounded queue/waiter backend](std-channel-performance.md#where-the-memory-goes)
described in the performance analysis. That backend preallocates the ring and
keeps blocked-operation waiters on caller stacks. Thread/parker setup and
payload-owned buffers still need accounting. Fixed message storage does not
prove allocation-free waiting; reusable first-use bookkeeping is also different
from allocation per message. Audit the pinned sources and measure the complete
path.

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

The [GNSS HAL](../hal/gnss/README.md#event-driven-session) implements bounded event
delivery through `open(sink)` and a session with explicit `request_stop()` and
`join()`. `shared/acquisition` supplies worker lifetimes and readiness; GNSS
owns queue-full handling and terminal notification. The event demo exercises
the topology under portable and stress workloads.
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
