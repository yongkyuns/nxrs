# Lean service qualification

This is the next application-level check after the
[frozen arithmetic evaluation](../arithmetic-parity/FROZEN.md). It does not
add another compiler optimization or activate the experimental toolchain.
Builds and measurements are local, not CI jobs.

## What runs

An input owner publishes typed LED commands through independent service event
loops. The LED owner controls the real GPIO2 USERLED and verifies its level;
a final monitor counts delivered events. Three services are the small demo.
The twenty-service case adds seventeen forwarding owners on the same path,
giving **20 worker threads and 60 queues**. No service sends a reply to the
input owner. The command task is the composition/test fixture, not another
business service.

Every owner has one `poll` wait point across its inboxes. Stop, control and
ordinary data have independent capacities of **1, 8 and 8**. Events are 16 B:
source timestamp, sequence, LED level, source identity and event kind. An
owner retains at most one pending outbound event when the next queue is full.
It then waits for downstream writability and its reserved stop queue together;
it does not spin, discard the event, or block inside a send operation.
Control takes precedence over ordinary data. This is not a guarantee against
ordinary-data starvation under unlimited control traffic.

Both languages validate the same event fields and the exact per-class sequence,
maintain the same counters, perform the same LED calls, and forward unchanged
events. Startup gates permit a full-capacity check: fill every slot, verify
that one additional send is rejected, measure live heap, then drain and verify
the contents before releasing the workers. Shutdown first stops production,
waits for delivery, stops and joins owners, closes the HAL and unlinks queues.
Host tests also exercise LED-open and LED-apply failures.

## What is held equal

| Item | C and Rust |
| --- | --- |
| Kernel | Same resolved NuttX configuration and archive bytes; only app selection differs. The verified uname build date may change. |
| Worker stacks | 4,096 B each: 12,288 B for three or 81,920 B for twenty. Never subtracted from total RAM or reduced to improve a messaging result. |
| Adapter | Same qualification-only native pthread/MQ/`poll` adapter. Native layouts stay behind scalar FFI. |
| LED | Same real USERLED driver, level readback and 1 µs settling interval. Host uses an explicit test double only. |
| Code policy | C and Rust application code at `-O2`; Rust std remains size-optimized, aborting panic, without `println!`. |
| Traffic | Same event count, input period, control/data mix and topology. Order rotates between languages; each topology boots fresh. |

This deliberately measures Rust domain dispatch on the lean native adapter,
**not `std::thread`, std MPSC or Crossbeam**. It does not replace the proposed
[production messaging API](../../docs/concurrency-event-communication.md).
The shared C code owns OS mechanics and diagnostics; the C and Rust service
loops separately implement event validation, sequencing, dispatch and
outbound retention. Rust's normal startup and CLI argument collection remain
in its image and live memory accounting.

The earlier 64 B, eight-slots-in-each-queue stress contract is unchanged.
This lean app uses fewer bytes and one reserved stop slot because it has a
smaller real event. A reduction relative to that stress test is **not solely
a Rust optimization**, and cannot be applied to apps needing 64 B payloads.
An arbitrary cyclic service graph needs its own backpressure/deadlock analysis;
this acyclic forwarding pipeline does not qualify every graph or producer mix.

## Measurements and limits

### Local ESP32-S3 results

#### Current message-source images (2026-10-09)

The [current evidence](results/esp32s3-message-2026-10-09.json) measures the
shutdown-hardened runtime with the coherent GPIO snapshot helper compiled in.
The source is **messages**, not physical interrupts. Matching kernel headers,
configuration, archive bytes, C compiler and 4 KiB worker stacks were enforced
by the corrected harness. The compiler remains the frozen 29-patch package;
neither image uses diagnostic tracing, forced IRAM placement or layout padding.

Three alternating-order blocks test both languages at three and twenty
services, with a fresh boot for every case and 1,000 events per case. All
**12,000 events** reached the terminal monitor without validation errors or
missing sequence numbers. Capture completed without errors, then the complete
original 16 MiB firmware was restored and verified.

| Current image | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Binary file | 213,380 B | 214,040 B | +660 B |
| Code + initialized data | 174,536 B | 184,770 B | +10,234 B |
| Resident RAM, before live heap | 66,360 B | 66,504 B | +144 B |

Binary length includes headers and alignment padding; it is not a substitute
for code/data growth. Both columns include the same kernel.

| Current matrix | C: 3 services | Rust: 3 services | C: 20 services | Rust: 20 services |
| --- | ---: | ---: | ---: | ---: |
| Full-capacity total RAM | 98,396 B | 98,724 B | 193,596 B | 193,924 B |
| Observed peak total RAM | 98,784 B | 99,112 B | 193,984 B | 194,312 B |
| Median run mean, input → LED | 55.0 µs | 36.0 µs | 327.3 µs | 311.0 µs |
| Worst observed LED latency | 529.4 µs | 591.4 µs | 953.7 µs | 1,032.5 µs |
| Events over 1 ms, out of 3,000 | 0 | 0 | 0 | 2 |

Rust adds **328 B of total RAM at both scales**: 144 B resident and 184 B
entry-task live heap. Worker/queue heap growth is identical: 16,752 B at three
services and 111,952 B at twenty. This is a fixed language difference for
these two topologies in the same app, not a bound for future APIs or state.
Rust's twenty-service peak leaves **55,688 B of the 250,000 B budget**. The
common platform still consumes about 78% before substantial business logic;
this PSRAM-enabled board does not qualify a no-PSRAM product.

Rust has a lower mean in this image pair, but a worse observed tail and two
1 ms misses. Earlier image pairs had a much slower Rust mean. That reversal
is consistent with the documented [flash-layout sensitivity](LATENCY.md), not
proof of a new worker optimization or universal Rust speed advantage. Keep a
final-image timing check; neither language has a proven hard 1 ms guarantee.
No sustained run or physical interrupt test was repeated in this matrix.

The same image pair also passed the [current same-boot restart check](results/esp32s3-message-restart-2026-10-09.json):
80 normal invocations and 8,000 delivered events across two reversed-order
blocks. Ten rounds per language/block alternate three and twenty services;
two rounds are warmup. All sixteen post-warmup samples per boot were flat at
7,332 B C / 7,372 B Rust, with 31 / 32 live allocations. These are heap samples
after the command exits, not total RAM while services run. The complete
original firmware was restored and verified again. This is a bounded normal
lifecycle check, not a new fault-injection or interrupt qualification.

#### Archived pre-hardening images (2026-10-07)

The device numbers in this section describe the pre-hardening runtime, identified
by the archived source hashes. The shutdown changes have separate rebuilt-image
restart evidence below; these earlier footprint/timing tables are not
current-runtime proof.

[Compact evidence](results/esp32s3-lean-2026-10-07.json) records the exact source,
firmware and frozen compiler identities, numerical results and open review
status. The matrix used three alternating-order blocks, 1,000 events per
language/topology/block. The sustained check used another 30,000 events per
language at twenty services. All **72,000 events** reached the terminal monitor
with no validation errors or missing per-class sequence numbers. The original
full-device firmware was restored and verified after both measurement sets.

Each language uses one binary for both service counts:

| Image measurement | C | Rust | Rust − C |
| --- | ---: | ---: | ---: |
| Binary file | 213,240 B | 213,988 B | +748 B |
| Code + initialized data | 173,304 B | 184,286 B | +10,982 B |
| Resident RAM, before live heap | 66,312 B | 66,456 B | +144 B |

Alignment padding absorbs much of the section growth in this binary. The
748 B file delta must not be substituted for the 10,982 B code/data delta when
predicting future layouts. These are whole-image measurements, including the
same kernel, not application object sizes or a universal Rust fixed-cost bound.

| Matrix result | C: 3 services | Rust: 3 services | C: 20 services | Rust: 20 services |
| --- | ---: | ---: | ---: | ---: |
| Full-capacity total RAM | 98,292 B | 98,620 B | 193,492 B | 193,820 B |
| Observed peak total RAM | 98,680 B | 99,008 B | 193,880 B | 194,208 B |
| Median run mean, input → LED | 35.7 µs | 98.4 µs | 308.1 µs | 379.7 µs |
| Worst observed LED latency | 532.1 µs | 616.8 µs | 944.9 µs | 1,052.8 µs |
| Events over 1 ms, out of 3,000 | 0 | 0 | 0 | 3 |

In the sustained twenty-service check, mean LED latency was **307.4 µs C /
379.1 µs Rust**, with **0 / 1** one-millisecond misses out of 30,000 events per
language. Its maxima were 934.1 / 1,040.1 µs. Sixty seconds is the *nominal
offered duration* at a requested 2 ms period; fixture sleep granularity can
lengthen the actual run. This is not a long-term endurance qualification.

The Rust RAM delta is **328 B at both scales**: 144 B resident plus 184 B
entry-task live heap. The worker/queue allocation increase itself is identical
in both languages. At twenty services, the C total breaks down as follows:

| Budget component | Bytes |
| --- | ---: |
| Resident RAM | 66,312 |
| Entry-task live heap before service startup | 15,284 |
| Worker stacks | 81,920 |
| Service context storage | 1,600 |
| Full queue payload storage | 5,440 |
| Remaining startup, thread, queue and allocator costs, not separately attributed | 22,936 |
| **Total at full capacity** | **193,492** |

Rust's observed peak leaves **55,792 B of a 250,000 B budget**. The language
delta is small, but the common threaded/native-queue platform still consumes
about 78% of that budget before substantial business logic. Smaller events
helped both languages; stacks were held equal and were not an optimization
variable. Whether this leaves enough room must be answered with a concrete
product budget, not the Rust delta alone.

**Decision:** keep compiler work frozen and use this lean adapter as a
qualification demo. Memory and image growth are modest for Rust domain logic
here, but the original flash images are **not speed-equivalent**. The later
[latency investigation](LATENCY.md) identifies flash instruction-fetch/layout
effects as the dominant cause: unused padding changes timing, and a matched
IRAM control removes nearly all of the large gap. Neither hot loop uses
formatting or heap allocation. No production placement fix was selected; a
product requiring a hard 1 ms bound remains unqualified. The measured gap
must not be called a general Rust tax.

### Open qualification work

This demo is **not ready to merge as a production runtime**. Independent review
identified three shutdown faults, now addressed and exercised locally in the
[host shutdown matrix](results/linux-shutdown-2026-10-08.json) and the
[ESP32-S3 fault matrix](results/esp32s3-shutdown-faults-2026-10-08.json). These
qualify the specific error models described below, not arbitrary OS failures.
Those archived device images predate the later GPIO snapshot fix; they are not
hardware evidence for the current source.
The two approved harness findings are now addressed: `measure.py` records the
original capture failure and any restoration error separately, and every
measurement/export path requires valid, matching C/Rust kernel-header hashes
alongside matching configuration/archive inputs. New exports include the
verified header identity. Historical published records remain unchanged; a
record without these fields does not establish automatic header verification.

The optional GPIO fixture is deferred because no jumper is available. Its
sequence/timestamp handoff now uses the coherent snapshot described below;
target IRQ delivery remains unqualified. The earlier
fresh-boot tests did not distinguish the small post-command heap increase from
one-time kernel initialization or repeated-invocation growth. Same-boot checks
address that observation over a bounded run, not every possible leak or fault
path. Interrupt qualification, arbitrary handler hangs, real foreign-task
recovery and untested target fault models remain unproven.

### Host restart and partial-failure checks

The [Linux lifecycle evidence](results/linux-lifecycle-2026-10-08.json) adds
repeated calls to the native coordinator with the unchanged C or Rust service
workers. Each language/scenario pair runs in one process, for five rounds.
Normal rounds alternate three and twenty services. Fault rounds use twenty
services and follow each expected failure with a successful recovery call:

- LED-open and LED-apply failures in the host test double.
- Queue creation failure after 0, 1, 30 or 59 successful opens.
- Thread startup failure after 0, 1, 7 or 19 successful starts.

All **220 calls** passed their expected outcome checks, including **100 injected
failures and 100 successful recoveries**. Successful normal/recovery calls
verified **16,000 delivered events**. After every call, file descriptors and
threads returned to their initial counts, and no named queues owned by the
fixture remained. A short settling interval permits exited Linux threads to
disappear from `/proc` before checking the thread count.

This qualifies only the tested Linux functional paths. It does **not** measure
ESP32 timing/RAM or prove allocator-leak freedom, and it does not re-enter the
normal Rust CLI/startup path. This historical record predates shutdown hardening;
its matrix did not inject stop-send, join or queue-close failures. No compiler,
dependency or runtime changes were made for that earlier check.

### Shutdown error handling

The updated [Linux evidence](results/linux-shutdown-2026-10-08.json) repeats all
five earlier scenarios and adds these three for both languages:

| Failure injected | Required handling and check |
| --- | --- |
| Persistent stop-send `EIO`, at workers 0, 7 and 19 | Return failure without joining the unstopped worker. Keep its run state and all queues alive; another still-faulted call must also return. |
| Persistent join `EINVAL`, at workers 0, 7 and 19 | Do not free worker state or gates until every join succeeds. A different-owner retry is rejected without touching the original handles. |
| Queue-close `EBADF`, at closes 0, 1, 30 and 59 | Report failure but still attempt unlink for every owned name. This fault model closes the real descriptor before returning the error. |

All **510 calls** passed: **290 expected failures**, **200 successful recovery
calls**, and **26,000 verified events on successful calls**. The host checks
require each shutdown-fault call to finish within two seconds, including resource
inspection. That is a functional timeout, not an embedded latency guarantee.
After each final recovery, descriptor/thread counts returned to baseline and no
fixture-owned named queues remained. Persistent faults deliberately retain
resources between calls; those intermediate states are checked, not counted as
successful cleanup.

Worker contexts and their borrowed startup gates now share one heap allocation.
They can therefore outlive a failed call safely. Cleanup releases nothing until
every started worker has been joined. Once the fault clears, the original
coordinator retries cleanup before starting a new run. A later NuttX command
task can have a different descriptor table, so it must not reuse the failed
task's handles; recovery then requires the original owner or an application
domain/device restart. Concurrent coordinator calls are outside this demo's
contract. An accepted stop does not make joining an arbitrarily wedged handler
bounded.

There are no new queues, worker stack changes or worker dispatch changes. The
common run header now uses heap storage, so the earlier RAM/image numbers must
not be presented as measurements of this implementation. Compiler, dependencies
and the default flash/IRAM placement policy are unchanged; relinking can still
change actual addresses and timing. A further two-round matrix passed with
AddressSanitizer/UndefinedBehaviorSanitizer on the shared C adapter (and C worker);
the Rust worker itself was not sanitizer-instrumented. At that stage, fresh
ordinary C/Rust CLI builds and all 78 Python tests also passed on Linux. Independent Luna review
reported no additional findings in these shutdown paths.

The [rebuilt ESP32-S3 restart matrix](results/esp32s3-shutdown-restart-2026-10-08.json)
also passed **80 normal invocations and 8,000 delivered events**, with the same
round/order/warmup protocol described below. Post-shutdown heap remained flat
at 7,332 B C / 7,372 B Rust after warmup, in both blocks. The full original
16 MiB firmware was restored and verified. This is normal-path target evidence,
not device fault injection or a replacement for the earlier latency/peak matrix.

Each rebuilt image adds **380 B of code/data and 8 B of resident RAM** relative
to its earlier image. The C/Rust differences remain +748 B binary file,
+10,982 B code/data and +144 B resident RAM. The new common heap header is
additional running storage, not included in resident RAM. Matching resolved
kernel inputs were enforced by the existing builder; archived kernel-header
hashes were also manually compared for this pair. The later harness fix makes
valid, matching header hashes mandatory for new measurements and exports; it
does not retroactively change this archived evidence.

The harness rejects unknown stdout, unexpected stderr, and missing, malformed
or out-of-order success/failure records. LED-apply failures must include the
full-capacity record; partial-startup failures must not claim a completed run.
A failed or timed-out child remains a failed test. The harness terminates its
own timed-out child but never unlinks queues based only on its PID: stale named
queues can survive a process and PIDs can be reused. Any leftovers require
manual ownership inspection. The published matrix was rebuilt and repeated
after these checks were tightened.

On Linux with GCC, Rust and mounted `/dev/mqueue`, reproduce from the repository
root using a fresh private output directory:

```sh
SQ_LIFECYCLE_PARENT=$(mktemp -d /tmp/nxrs-lifecycle.XXXXXX)
python3 tests/service-qualification/lifecycle.py \
  --out "$SQ_LIFECYCLE_PARENT/evaluation" --repetitions 5
SQ_LIFECYCLE_DIR="$SQ_LIFECYCLE_PARENT/evaluation" \
  python3 -m unittest discover -s tests/service-qualification -p 'test_*.py' -v
```

The second command replays all eight profiles once per language and rejects stale
sources or executables. Raw transcripts and binaries stay in the private
directory; the public record contains counts and source/compiler/binary
identities, not transcripts or personal paths.

### Shutdown faults on the ESP32-S3

The [device fault evidence](results/esp32s3-shutdown-faults-2026-10-08.json)
exercises the same stop-send, join and queue-close error models on the board,
using the unchanged production runtime and C/Rust workers. Shared diagnostic
hooks inject the errors; they are excluded from normal images and rejected by
the footprint/timing publisher. The full 510-call Linux matrix was also rebuilt
and repeated after extracting these shared hooks.

Two blocks alternate C/Rust order. Each language/block boots once, then keeps
one real coordinator task alive for a warmup and three repetitions at every
fault position. All runs use twenty services, sixty queues and 100 events per
successful call. Persistent stop/join faults require an initial failed call,
another still-faulted retry, then recovery after the fault clears. Queue-close
faults require a failed call followed by recovery; the real descriptor is closed
before the synthetic `EBADF`, so this does not model every possible close failure.

All **316 calls** passed their expected outcomes: **192 expected failures**,
**120 successful recoveries** and four warmups. The warmups/recoveries verified
**12,400 delivered events**. After every recovery, fixture-owned descriptors,
unjoined thread handles and queue names were zero, and live heap returned
exactly to its warm baseline. Persistent stop/join failures intentionally retain
all sixty queues and one unjoined handle until cleanup is safe.

The longest measured call, including resource checks, was **222 ms**, below
the predeclared two-second functional bound. This is not event latency or a
speed comparison. Warm/final heap was 20,324 B C / 20,468 B Rust in both blocks;
these samples include the still-running diagnostic coordinator. They are not
the NSH post-task samples below or a new production RAM measurement.

Recovery here stays in the original real task: no foreign PID is substituted
on the device. This does not qualify cross-task recovery, kernel corruption,
arbitrarily wedged handlers or interrupt timing. Independent Luna review found
no additional issues in the diagnostic fixture. The original full 16 MiB
firmware was restored and verified after the complete matrix.

To reproduce, use the local linking commands below with `--faults` on **both**
links and fresh diagnostic output directories. Do not combine this mode with
the timing/layout diagnostics. Then run:

```sh
python3 tests/service-qualification/device_faults.py \
  --c "$SQ_C_FAULT_IMAGE" --rust "$SQ_RUST_FAULT_IMAGE" --port "$SQ_PORT" \
  --flasher "$SQ_ESPTOOL" --backup "$SQ_PRIVATE_BACKUP" \
  --backup-sha256 "$SQ_BACKUP_HASH" --out "$SQ_FAULT_RUN" --blocks 2
```

The existing private-backup, preflight and full-restoration safeguards apply.
Raw transcripts stay private; `device_faults.public_report` exports only a
complete successful matrix with verified restoration and matching provenance.

### Repeated startup/shutdown on the ESP32-S3

The [device restart evidence](results/esp32s3-restart-2026-10-08.json) uses the
original, pre-hardening, uninstrumented C/Rust images above. Each language/block
boots once and runs ten rounds of three services followed by twenty services, with 100
events per call. Two blocks reverse the language order. There is no reboot
between the twenty app calls within a block; each NSH invocation does start a
new app task, including normal Rust startup. This complements, rather than
replaces, the same-process host test.

All **80 invocations and 8,000 delivered events** passed. Two complete rounds
per boot were declared warmup before capture; the remaining eight rounds give
sixteen post-command samples per boot. Both blocks produced the same result:

| After shutdown, following warmup | C | Rust |
| --- | ---: | ---: |
| NSH-observed live heap | 7,332 B | 7,372 B |
| Live allocator blocks | 31 | 32 |
| Additional observed heap growth during measured restarts | 0 B | 0 B |

The initial increases occurred within the first three-/twenty-service round
and did not keep growing with subsequent invocations. Their allocation owners
were not isolated. Rust retained **40 B and one allocator block more** here;
do not infer a universal fixed std cost from this one lifecycle. These are
**post-shutdown heap samples**, not total RAM while services run. The roughly
194 kB running peak reported above is unchanged.

The original complete firmware was restored and verified. No target source,
compiler, dependency or code-placement changes were needed. This bounded
normal-path check does not qualify injected device failures, every allocation
state, interrupt timing, arbitrary backpressure graphs or a no-PSRAM product.
It does not validate the later shutdown changes.

To reproduce with the same private backup safeguards and paired images:

```sh
python3 tests/service-qualification/restart_device.py \
  --c "$SQ_C_IMAGE" --rust "$SQ_RUST_IMAGE" --port "$SQ_PORT" \
  --flasher "$SQ_ESPTOOL" --backup "$SQ_PRIVATE_BACKUP" \
  --backup-sha256 "$SQ_BACKUP_HASH" --out "$SQ_RESTART_RUN" \
  --rounds 10 --blocks 2 --warmup-rounds 2 --events 100
```

The output directory must be new and private. The harness records restoration
errors separately and preserves the original capture failure. Warmup is an
explicit, reported setting shared by both languages, not selected after
looking at the samples. Its `public_report` helper exports aggregate evidence
only after a complete successful matrix and verified restoration.

### Accounting and timing definitions

Report binary file bytes separately from **code + initialized data** in
allocated ELF sections. Debug symbols are excluded from both the flashed
binary and that section total. Headers, bootloader content and alignment can
make the binary larger than the section sum; they are not debug symbols.

Full-capacity RAM is resident IRAM/DRAM/BSS plus live heap with every queue
slot occupied. It includes worker stacks and kernel metadata. The remaining
budget is calculated against **250,000 B**, not 250 KiB. This is conservative
capacity accounting, not a promise that allocator use never increases under
all future application states. Observed peak RAM adds resident RAM to the
larger of the full-capacity heap sample and NuttX's recorded heap high-water
mark. The inherited board SDK has PSRAM enabled, but this is not proof that
the measured heap lives there; memory placement and a no-PSRAM product build
remain unqualified.

Message-source latency is fixture timestamp → successful LED level readback.
It includes admission backpressure, queue delivery, all intervening services
and HAL work. A declared 1 ms deadline counts misses rather than hiding them
in averages. Test-fixture sleeps set the offered rate; this is neither a
maximum-throughput measurement nor a hardware-interrupt result.

The optional GPIO mode requires a physical **GPIO15 → GPIO21 jumper**, using
only those two GPIO pins, not supply pins. Native board nodes are `/dev/gpio0`
(GPIO15 output) and `/dev/gpio2` (GPIO21 rising-edge interrupt input). The input
owner includes the interrupt descriptor in its single wait point. The fixture
timestamps a request to drive GPIO15 high; native hardware IRQ readiness then
causes an event to reach the LED owner. This measures **GPIO write request →
LED readback**, not exact edge time or ISR-entry latency. Missing/coalesced
events fail the ordered-delivery/count checks. Until the jumper is confirmed
and this mode runs on the board, interrupt latency remains unqualified.

The fixture previously published timestamp and sequence as separate atomics.
A reader could observe the old sequence with the next pulse's timestamp; a
release/acquire on the sequence did not prevent that subsequent overwrite.
The shared native [snapshot helper](pulse_snapshot.h) now serializes a complete
event copy with GPIO acknowledgement/publication using one task-context mutex.
It holds the guard only across the native GPIO operation and copy, never across
sleep, queue delivery or service processing. No ISR takes this mutex.

The [host regression fixture](pulse_snapshot_test.c) checks both overlapping
publication/acknowledgement orderings, exact event fields, and recovery after
failed GPIO callbacks. Its stress check makes 50,000 publications and 50,000
snapshot reads, checking consistency rather than claiming delivery of every
publication. This remains a latest-pulse slot, not an interrupt-event buffer:
missing/coalesced edges must still fail the service sequence/count checks.
The fix is shared by C and Rust and adds no queues or worker threads. Earlier
archived device evidence predates it; new target IRQ measurements are required.

The helper regressions passed on macOS and Linux, including a Linux
AddressSanitizer/UndefinedBehaviorSanitizer run. Fresh C/Rust host builds also
passed the full five-repetition lifecycle matrix: 510 calls, 290 expected
failures, 200 recoveries and 26,000 events on successful calls. This checks the
ordinary lifecycle after the fixture change, not real interrupt delivery.
Independent Luna review found no additional issues in the snapshot fix.
C and Rust target images rebuilt with matching kernel configuration, archives
and header hashes. The later message-source measurements above flash these
images and qualify their normal message path. The IRQ fixture remains deferred
because no jumper is available; message tests do not qualify interrupt delivery.

### Diagnostic latency breakdown

`build.py link --trace` adds identical native timing wrappers to both languages
without editing either worker or the shared runtime. These images are
**diagnostic only**: their stamp ring, counters, calls and changed code layout
perturb execution. They are rejected by the normal footprint publisher.

The trace divides each event's path at two boundaries: just after the native
wait returns, and just before the worker records its completed handling.

- **Between-service time:** previous owner's record → next owner's native wait
  return; for the input owner, command creation → wait return. This includes
  forwarding bookkeeping, native MQ/`poll`, scheduling and tracing overhead,
  not just time sitting in a queue.
- **Worker time excluding LED:** wait return → record entry, minus the measured
  LED call. This includes validation, sequencing, call boundaries and tracing
  overhead, not pure application CPU time.
- **LED time:** entry → return of the same native LED HAL call, including GPIO
  write, the settling interval and level readback.

Raw cycle sums for all stages through the LED owner must add **exactly** to
the traced input → LED-record total. A 64-entry predecessor stamp ring detects
and rejects missing/overwritten samples. Both target disassemblies were checked
to verify that calls actually reach the wrappers; host parser tests also
reject malformed, incomplete or non-closing traces.

Use the same `link` and `measure.py` commands below with `--trace` on **both**
links. After verified restoration, export timing-only evidence with:

```sh
python3 tests/service-qualification/trace_report.py \
  --report "$SQ_MEASUREMENTS/report.json" --out "$SQ_PUBLIC_TRACE"
```

Compare paired trace builds with each other, not their absolute latency with
an uninstrumented image. Profiling a segment identifies where the difference
occurs; it does not by itself prove the underlying cause or a production fix.

#### What the timing controls established

The [paired trace](results/esp32s3-latency-trace-2026-10-07.json) placed most of
the twenty-service difference between services, rather than inside Rust event
validation or LED control. However, instrumentation reduced the small-case
gap from about 63 µs to 24 µs. Its extra calls and changed image layout made
it unsuitable for assigning the original gap directly to individual functions.

The next [same-image worker control](results/esp32s3-same-image-workers-2026-10-07.json)
kept both unchanged worker implementations in one firmware and selected one
before the startup gates. The final
[entry/worker control](results/esp32s3-same-image-entry-2026-10-07.json)
also kept both the normal Rust entry and a diagnostic C entry in one firmware.
Neither selector runs in the event loop. Post-shutdown markers verified every
worker and the actual entry path. All choices used the **same image bytes**.

Final traced input → LED-record means, pooled over three fresh boots and
3,000 messages for each cell:

| Entry / service worker | 3 services | 20 services |
| --- | ---: | ---: |
| C entry / C worker | 39.46 µs | 341.64 µs |
| C entry / Rust worker | 39.70 µs | 344.57 µs |
| Normal Rust entry / C worker | 39.46 µs | 341.63 µs |
| Normal Rust entry / Rust worker | 39.71 µs | 344.49 µs |

Using the normal Rust entry, the twenty-service path splits as follows:

| Traced segment | C worker | Rust worker | Rust − C |
| --- | ---: | ---: | ---: |
| Between-service time | 315.38 µs | 316.98 µs | +1.60 µs |
| Worker time excluding LED | 19.47 µs | 20.96 µs | +1.49 µs |
| Shared LED HAL | 6.78 µs | 6.55 µs | −0.23 µs |
| **Total** | **341.63 µs** | **344.49 µs** | **+2.86 µs** |

Rounding can affect displayed sums; closure is checked on exact raw cycle
sums. The terminal monitor is outside this LED latency endpoint.

The large original penalty **does not consistently follow the Rust worker or
the normal Rust entry**. The frozen Rust input ELF/driver did not change, and
the worker remained 383 B across these links. Switching entry paths had at
most 0.08 µs effect on the pooled means. Startup itself occurs before the
stopwatch; this does not claim zero startup time or zero std footprint.

These controls narrowed the large discrepancy to **build/call-path context**,
not a large unavoidable event-processing cost in Rust. Adding the diagnostic
entry changed absolute timings substantially even with the same compiled
worker. This is evidence of sensitivity, not a reason to ship diagnostic
helpers as an optimization or to claim the original binary has been fixed.
The follow-up [flash-fetch/layout diagnosis](LATENCY.md) uses unexecuted
padding, hardware fetch-wait counters and matched IRAM placement to isolate
the dominant mechanism. The exact conflicting cache lines remain unidentified.

The final control delivered all 24,000 messages correctly. Each twenty-service
invocation still had one event over 1 ms, including the C cases. Similar mean
speed is **not a worst-case deadline guarantee**. All three diagnostic matrices
restored and verified the original full-device firmware. No LLVM, Rust std or
NuttX dependency changes were made for this investigation. The application
decision can proceed without another arithmetic optimization round; production
timing and the open lifecycle/interrupt qualifications remain separate gates.

To reproduce the final control, add `--trace` to the C link and
`--trace --dual-worker --entry-switch` to the Rust link. Use fresh output
directories, then run the matched pair with:

```sh
python3 tests/service-qualification/measure.py \
  --c "$SQ_C_TRACE" --rust "$SQ_CONTROL_IMAGE" --language rust \
  --rust-worker alternate --rust-entry alternate-pairs --blocks 12 \
  --events 1000 --port "$SQ_PORT" --flasher "$SQ_ESPTOOL" \
  --backup "$SQ_PRIVATE_BACKUP" --backup-sha256 "$SQ_BACKUP_HASH" \
  --out "$SQ_CONTROL_RUN"
python3 tests/service-qualification/trace_report.py \
  --report "$SQ_CONTROL_RUN/report.json" --out "$SQ_PUBLIC_CONTROL"
```

The C entry is a diagnostic control for these scalar-FFI workers only, not a
replacement for std initialization in a general Rust application.

## Local reproduction

Use an isolated, already provisioned private NuttX SDK tree; do not point these
diagnostic builders at a working upstream checkout. Dependencies and the
frozen compiler package are documented in
[the public patch builder](../../upstream/rust-llvm/BUILDING.md). The builder
requires the frozen `mul-range` package, its provenance/patch ledger, complete
std snapshot, matching target JSON and target GNU linker. It validates actual
driver/std identities; it never installs or patches a compiler.

```sh
# Linux compiler host; reuse the frozen package's warm Cargo directory.
python3 tests/service-qualification/build.py rust \
  --sysroot "$SQ_SYSROOT" --cargo "$SQ_CARGO" --ld "$SQ_LD" \
  --ledger "$SQ_LEDGER" --compiler-provenance "$SQ_COMPILER_PROOF" \
  --target "$SQ_TARGET" --cargo-target "$SQ_CARGO_CACHE" --out "$SQ_RUST_INPUT"

# Local final-link host, with GPIO/IRQ enabled in the matched prepared kernel.
# prepare saves the original config and builds the C demo once to resolve
# the driver configuration; use its resolved.config as SQ_BASE_CONFIG.
python3 tests/service-qualification/build.py prepare \
  --tree "$SQ_SDK" --prefix "$SQ_GCC_PREFIX" --out "$SQ_KERNEL_PREPARE"
python3 tests/service-qualification/build.py link \
  --tree "$SQ_SDK" --prefix "$SQ_GCC_PREFIX" --baseline "$SQ_BASE_CONFIG" \
  --language c --out "$SQ_C_IMAGE"
python3 tests/service-qualification/build.py link \
  --tree "$SQ_SDK" --prefix "$SQ_GCC_PREFIX" --baseline "$SQ_BASE_CONFIG" \
  --language rust --bundle "$SQ_RUST_INPUT" --out "$SQ_RUST_IMAGE"

python3 tests/service-qualification/measure.py \
  --c "$SQ_C_IMAGE" --rust "$SQ_RUST_IMAGE" --port "$SQ_PORT" \
  --flasher "$SQ_ESPTOOL" --backup "$SQ_PRIVATE_BACKUP" \
  --backup-sha256 "$SQ_BACKUP_HASH" --out "$SQ_MEASUREMENTS"

# After verified restoration, export numerical evidence without raw transcripts.
python3 tests/service-qualification/publish.py \
  --report "$SQ_MEASUREMENTS/report.json" --out "$SQ_PUBLIC_RESULT"
```

All output directories must be new. Measurement requires a private full
16 MiB backup, verifies the current board against it before overwriting
anything, and restores/verifies the complete backup in `finally` after a flash.
The paired artifact/source inventories and compiled kernel-header identities
are checked before each flash and again after capture. The harness also records
and rechecks its own source hashes. `report.json` keeps `failure` and
`restoration_error` separate; either error prevents a successful export, even
if a contradictory restoration flag is present. The exporters validate the
header hashes themselves rather than trusting a recorded verification flag.
No GPIO mode should be selected without confirming the physical fixture.
Do not publish backup images, personal filesystem paths or device credentials.

Linux host checks (POSIX MQ readiness is not supported by this macOS fixture):

```sh
cd tests/service-qualification
cc -std=c11 -D_DEFAULT_SOURCE -O2 -Wall -Wextra -Werror \
  runtime.c worker.c hal_host.c ../service-footprint/native_thread.c \
  -pthread -lrt -o "$SQ_HOST_C"
CARGO_TARGET_DIR="$SQ_HOST_CARGO" cargo test --offline --locked
CARGO_TARGET_DIR="$SQ_HOST_CARGO" cargo build --release --offline --locked
SQ_HOST_C="$SQ_HOST_C" SQ_HOST_RUST="$SQ_HOST_RUST" \
  python3 -m unittest discover -s . -p 'test_*.py' -v
```

Host results protect functional behavior only, never target latency/RAM.
The finish line is an evidence-backed application decision, not another round
of arithmetic tuning. Real sensors, Wi-Fi/Bluetooth, power, arbitrary overload
patterns and a sustained no-PSRAM product qualification remain separate work.
