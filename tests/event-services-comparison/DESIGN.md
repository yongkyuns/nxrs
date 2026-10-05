# Event service comparison contract

This replaces the application model, not historical evidence. Twenty services
own state and receive from three peers at offsets 1, 3 and 7 modulo 20. Each
service publishes control, data and status events to all three peers. A service
has either three class queues of eight slots or one mailbox of 24 slots. Both
layouts configure exactly 480 slots of 64 bytes, or 30,720 bytes of payload
capacity. Zephyr and Embassy reserve those buffers statically; NuttX allocates
message storage on demand, so observed heap use is not a full-capacity bound.
All queues have
one receiving owner; all peer services can send to them. There are no replies
and no collector service. The test coordinator checks per-flow accepted and
received counts, order and token digests only after all service loops stop.

Each service has one wait-any point covering all inboxes and its next publication
deadline. It handles at most one received event per iteration and publishes at
most one due release batch per iteration. Ready queues are selected round-robin.
Publication sends never block: a rejected full queue is counted, never retried
or silently overwritten. Normal and burst qualification require zero rejected
events; overload reports rejections without treating them as unexplained loss.

Traffic lasts two seconds, then a 500 ms drain interval. Periods for control,
data and status are 250, 50 and 100 ms. The first release is one period after
the start plus a service phase of service_id milliseconds and a class phase of
2*class milliseconds. Deadlines are relative to the shared start cycle counter,
not the previous send. Every expired release is attempted and publication
lateness is recorded. Burst sends two data events per release. Overload uses
10 ms data periods, 16 data events per release and zero service/class phase.
There is no automatic retry or slowing of the release calendar. Unattempted
scheduled releases at the drain deadline invalidate the run.

Sequence numbers start at zero independently for each class, and increment
once per event (the same event is multicast to three peers). The wire event is
exactly the layout in contract.h. token = (source<<24 | destination<<16 |
kind<<8) XOR (sequence*0x9e3779b9) XOR 0xa5a5a5a5, using wrapping u32 arithmetic.
payload[i] = byte(token >> (8*(i modulo 4))) XOR byte(i). Handlers check all 44
payload bytes, source/destination/peer consistency and increasing per-flow
sequence, then update value = rotate_left(value,3) XOR token, followed by
value += 0x7f4a7c15 (wrapping). This deliberately small handler is identical in
C and Rust; larger business computation is a separate future experiment.

The [timing controls](CONTROLS.md) and [scheduling follow-up](SCHEDULING.md)
add explicitly identified CPU-work and simulated-I/O profiles without changing
the base traffic contract. Their measurements remain separate from the original
small-handler matrix.

Metrics cover all events, not periodic sequence samples. Service-local
histograms record publication lateness, release-to-handler-start,
release-to-handler-finish, control start latency and post-to-handler-start
queue response. The latter starts immediately before the nonblocking send;
it includes the send operation, queueing and scheduling, not just a syscall.
Histogram bounds are
1,1,2,3,4,6,8,12,... microseconds, with the last bin covering u32::MAX. Reported
percentiles are upper bounds, capped at the observed maximum, not exact
nearest-rank values. Deadlines are 20 ms control, 40 ms data and 80 ms status;
misses are counted per class. Timeliness failure is separate from protocol
correctness. Queue peaks are observed immediately after successful sends;
preemption can make this an underestimate, which must be labeled.

The CPU is single-core 240 MHz. All platforms timestamp the same always-running
ESP32-S3 SYSTIMER Unit0 counter at 16 MHz, scaled by 15 into the contract's
240 MHz units. Counter0 is enabled through CPU stalls without resetting the
counter or an alarm. All elapsed arithmetic is relative to a common start,
and runs plus the drain interval stay below the synthetic u32 counter wrap.
Staging, initialization, printing and post-run reconciliation are untimed.
RAM ledgers separate queue slots, queue/wait metadata, thread/task execution
storage, application state and test-only diagnostics. Whole RAM is reported
as well; stack reservations are never labeled messaging overhead. CPU and idle
power must not be inferred from elapsed handler time or a spin executor.
