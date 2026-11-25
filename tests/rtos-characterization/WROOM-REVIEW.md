# WROOM comparison review — November 17, 2026

This review checks two assumptions behind the published comparisons: whether
both message paths run from internal memory, and whether RAM differences
remain after separating stack and payload capacity. It contains fresh physical
measurements on one ESP32-S3 WROOM. The Pico, CPU-charge and alarm-response
campaigns were not rerun and do not inherit this review's conclusions.

## Setup and controls

| | Message delivery | Native RAM |
| --- | --- | --- |
| Implementations | Same Rust workload: patched NuttX std channels versus Zephyr `k_msgq` | Same C fixture: NuttX pthread/POSIX queues versus Zephyr threads/queues |
| Board | One 240 MHz core; radio and PSRAM disabled | Same |
| Software | NuttX `2b5c0de5e933`; Zephyr 4.5.0 `41d10b597304`; Espressif Rust `abf50ae2e`; GCC 14.2.0 | Same kernels and GCC; no Rust application/runtime in either arm |
| Compilation | Rust `-O2`, no LTO; corresponding hot operations in IRAM/ROM | Kernel `-Os`, application `-O2`, lean reporting and stack coloration; ordinary kernel placement policies |
| Scheduling | FIFO workers, no timeslice, 100 Hz tick | 1 ms tick, 10 ms timeslice |
| Work | Four producers, one receiver, eight slots; 16/64-byte streams and 16-byte sustained throughput; separate one-producer paced case | 20 service contexts, 60 queues, 480 × 64-byte payload slots |
| Repetition | Three rotated passes; fresh boot per case: 24 captures | Separate 0/1/20-worker images, three rotated passes: 18 boots; matched usable-stack control: six more boots |
| Accounting | Nominal 16 MHz SYSTIMER; per-run distributions, no pooling | ELF reservations plus NuttX native heap high water; Zephyr's arena/stacks counted once in ELF |

Both campaigns used a fresh full flash backup, verified flashing and verified
restoration. All **24 timing captures and 48 RAM profiles** passed replay
against their frozen artifacts. RAM boot order was rotated; the 20-worker
boot ran thread-only, queue-only, then three saturation fill/drain cycles.
Cumulative heap peaks include earlier profiles and reporting. Original firmware
was restored after each campaign. The [numeric record](wroom-review-2026-11-17.json)
contains build/source hashes and numeric observations, without device identifiers,
credentials, private flash or raw boot logs.

The RAM controller requests 8 KiB on both kernels; reported usable capacity
is 7,936 / 8,192 B. IRQ reservations are 4 KiB each. NuttX reserves a 3 KiB idle
stack; Zephyr reserves 1 KiB idle and 1 KiB workqueue stacks. These background
budgets remain in the ledger rather than being treated as matched worker
storage or unavoidable kernel costs.

## Placement: what was corrected and what was reviewed

The old scanner followed direct calls but missed Xtensa `l32r`/`callx8`
sequences. Reading their targets from the ELF exposed `nxsig_addset` in flash,
reachable from the semaphore wait's pending-signal scan. The qualification
linker fragment now places that function and its literal in IRAM. This does
**not** prove that pending signals occurred in the timed runs or caused their
latency. The corrected image uses 36 more IRAM bytes overall; the measured
results remain close to the historical ones.

The [supplemental scanner](../rtos_harness/xtensa_calls.py) follows agreeing
register values through branches and windowed calls. It also starts from
interrupt entry, dispatch, timer and context-switch roots. On the measured
images it inspected 93 NuttX and 113 Zephyr functions without finding another
flash boundary before the listed cold exclusions. Source/ELF review covered:

| Boundary | Review |
| --- | --- |
| Payload operations | Both call the same ROM `memcpy` at `0x400011f4` and `memcmp` at `0x4000120c`; buffers/stacks are internal, PSRAM disabled |
| Native timer dispatch | NuttX `systimer_isr` at `0x40378214`; Zephyr `ccompare_isr` at `0x4037a904`; both in IRAM, but different timer/dispatch implementations |
| Paced callbacks | Fixture registration binds `pulse_expired` to `arrivals::tick`; both callbacks are in IRAM. Zephyr's dynamic timeout entry `z_timer_expiration_handler` is also in IRAM (`0x4037a2c0`) |
| Std variant dispatch | Six remaining calls select zero-capacity channel send/receive variants. The fixture constructs an eight-slot channel, so those flash targets are outside this workload |
| Error and first-use paths | Assertions, fatal printing, channel growth and thread-local initialization are excluded. Faults fail capture; setup/warm-up excludes first use by design, but this campaign did not trace every allocation or executed branch |

Total executable internal-memory sections are **62,840 B NuttX / 55,240 B
Zephyr**. Equal hot-operation residency does not require equal instruction
counts or equal total IRAM reservations. These diagnostic images are not a
minimum-product footprint measurement.

The automatic audit still reports `matched_placement_qualified=false`:
mutable callbacks, frame aliases and general indirect control flow are not
proved by constant propagation. The source review above is scoped to these
successful cases. It supports a configured-workload comparison, not a proof
that all error paths, future workloads or kernel costs are identical.

## Timing result and meaning

Ranges below are the minimum and maximum **per-run** result across three boots.

| Case | NuttX std | Zephyr native |
| --- | ---: | ---: |
| 16-byte stream delivery p99 | 50.375 µs | 51.938–52.000 µs |
| 64-byte stream delivery p99 | 85.375–85.438 µs | 85.313 µs |
| 16-byte sustained delivery rate | 268,505/s | 234,835/s |
| One-producer paced delivery p99 | 7.375 µs | 8.188 µs |

Every admitted message arrived with valid content/order and joined cleanup.
The streams remain close; sustained std throughput is 14.3% higher. Larger
payloads raise both stream tails by roughly 34–35 µs, consistent with copy and
queue-residence costs, but subtracting percentiles does not isolate copy time.
The sustained case omits per-message timing, so its advantage is not a 14.3%
application-latency improvement. Nominal clocks, three repetitions and an idle
board do not establish worst-case deadlines, energy efficiency or loaded
camera performance.

## RAM: separate requests, usable stacks and queue costs

Separately linked 0/1/20-worker controls remove Zephyr's otherwise permanent
20-stack reservation. One extra worker adds **4,256 B** to Zephyr's image:
4,096 stack, 136 thread object, 12 entry/join state and 12 observation bytes.
NuttX adds **4,328 heap bytes plus 28 static bytes** for one 4,096-byte request;
whole-section alignment adds another four bytes in the one-worker image.
Twenty native workers add 86,624 heap bytes, including 64 retained bytes after
join. That retention is included, not called a recurring leak.

Equal requests were insufficient: NuttX reported 3,864–3,872 usable bytes,
versus Zephyr's 4,096. The final control requests 4,336 bytes on NuttX, yielding
4,104–4,112 usable bytes. Thus both provide **at least 4 KiB usable space**;
NuttX retains 8–16 extra bytes per worker from alignment. Both keep the same
30,720-byte payload capacity. This is a capacity comparison, not proof that
this workload needs 4 KiB: observed worker use was only 720 / 372 bytes and
excluded later teardown and unexercised paths.

| RAM contribution, final control | NuttX C | Zephyr C |
| --- | ---: | ---: |
| Workers: linked storage + measured creation increment | 91,984 B | 85,120 B |
| Queues: linked storage + measured create-and-fill increment | 62,536 B | 34,880 B |
| RAM instructions/vectors | 34,520 B | 47,824 B |
| Other linked state/reservations | 24,284 B | 36,252 B |
| Baseline heap and combined-phase/peak remainder | 10,424 B | Already reserved above |
| **Observed/provisioned total** | **223,748 B** | **204,076 B** |

All three passes produced these totals. Equal **requested** stacks instead
produced 218,948 / 204,076 B, a 7.3% difference. Providing at least 4 KiB
**usable** stacks makes it **19,672 B, or 9.6%**. Neither is pure fixed RTOS
memory, and neither measures ordinary Rust std.

**The largest positive difference is queue machinery, not payload or the
common stack budget.** Its 27,656 B contribution comprises 13,624 B of NuttX
empty-queue creation, 7,552 B of dynamic message headers/alignment, 5,760 B of
static pool spare/headers, and 720 B more adapter controls. Saturation adds
472 × 80 B on NuttX; eight messages use the static pool. Zephyr reserves its
payload/queue objects directly. The NuttX full-to-drained heap returns exactly
to its empty level on every saturation cycle. Queue creation/cleanup retains
648 bytes on first use; it remains included in the measured increment.

The **6,864 B** worker-storage difference and **−13,304 B** RAM-code difference
are separate. Remaining baseline/reservation terms net to **−1,544 B**. They
include main/idle/workqueue stacks, arena capacity, diagnostics and runtime
state; do not relabel this mixed remainder “kernel overhead.” These RAM images
use different native code-placement policies and are not the timing images.

For developers, this points to queue admission/storage choices and native
pool configuration before shrinking already-matched worker budgets. A shared
std inbox may avoid POSIX descriptor and per-message bookkeeping, but changing
three independently admitted queues into one FIFO changes behavior. The
[ordinary Rust study](../../docs/rust-std-footprint.md#ram) measures that separate
choice. No speed-preserving RAM saving is inferred merely by moving code back
to flash.

## Reproduction and remaining scope

Use the [MPSC build/capture recipe](../mpsc-comparison/README.md) with the pins
above and the four named cases, three fresh boots each. Run
`tests/rtos_harness/xtensa_calls.py --elf IMAGE --objdump TOOL --backend std|native --out REVIEW.json`
on each frozen ELF, then review callbacks and cold exclusions against its
source inventory. The scanner never changes instruction bytes.

For RAM, follow [RAM.md](../event-services-comparison/RAM.md), adding
`--worker-capacity 0`, `1` and `20` to separate native C builds. Use
`--cross-prefix` for the same GCC in both kernels. Run only `alloc-threads-N`
on reduced-capacity images; at 20 also run `alloc-queues` and `saturation`.
Repeat the 20-worker pair with NuttX `--worker-stack-bytes 4336` and Zephyr
`4096`. Validate the usable-stack floor from `ES_STACK`, rather than assuming
it from the request. Build receipts bind the source, ELF, image and effective
configuration; `ram_matrix.validate_output` replays each captured profile.

Remaining work should answer a specific product question: qualify the older
Pico placement/interrupt measurements before attributing their gap; measure
critical-section duration with matched observers before ranking interrupt
response; and validate any queue or stack reduction under the actual handler,
error and teardown paths. This review does not justify another broad matrix
or a blanket “Rust is slower/larger” claim.
