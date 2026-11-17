# Rust std memory and image size

This report assesses two costs of using Rust on NuttX: the RAM needed for ordinary std workers and inboxes, and the linked image size of a matched C/Rust application. The studies use different firmware and workloads. Their results are reported separately and cannot be combined into a single “Rust overhead” figure. A fresh native C control also separates NuttX/Zephyr resource costs from equal stack and payload budgets.

<a id="matched-work-not-just-matching-names"></a>

## Test setup

### RAM: ordinary Rust workers and inboxes

| Item | Tested configuration |
| --- | --- |
| Hardware and OS | Physical ESP32-S3 WROOM, one 240 MHz core, flat NuttX; networking and external RAM disabled |
| Build | `esp32s3-ram-footprint` profile; ordinary `fn main` binaries with allocation probes and stack coloration; source `ab7fdc45` |
| Workloads | Instrumented baseline, std threads, std inbox services, Crossbeam selection services; 0, 1, 5, 10 and 20 owners |
| Worker storage | 4,096-byte requested stack per worker; controller provision in the fixture recipe is 16,384 bytes |
| Std service inbox | One 24-slot FIFO per owner, with 64-byte inline events |
| Crossbeam service inbox | Three eight-slot FIFOs per owner, with 64-byte inline events and three-arm `select_biased!` |
| Repetition | Three rotated passes of 26 cases: 78 physical captures |
| Lifecycle | Construction, capacity fill/drain, first timeout, eight repeated wait cycles, joined teardown |
| Correctness | Payload and ordering validation; full-queue rejection returns the submitted value |

The observer sampled Rust allocations and native heap at quiescent gates, with printing outside the measured phase. First-use reporting machinery was warmed before the workload baseline. Separate queue-only controls distinguished channel storage from thread storage. The linked-image totals came from the corresponding images. [Fixture and build procedure](../tests/ram-footprint-std/README.md).

### Cross-RTOS RAM control: native C workers and queues

On November 17, 2026, the WROOM ran separately linked 0/1/20-worker C images under NuttX `2b5c0de5e933` and Zephyr 4.5.0 `41d10b597304`, using the same GCC 14.2.0. The fixture provides 60 queues with 480 × 64-byte slots, lean reporting, stack coloration, one 240 MHz core, 1 ms ticks and 10 ms timeslices; radio and PSRAM are disabled. Three rotated passes exercised thread-only creation/join, queue-only fill/drain, and saturation with twenty workers alive. A further paired control supplied at least 4 KiB **usable** stack per worker. All 24 boots / 48 profiles passed replay. This uses native pthread/POSIX and Zephyr APIs, with no ordinary Rust runtime. [Setup and evidence](../tests/rtos-characterization/WROOM-REVIEW.md).

### Image size: matched C/Rust packet application

| Item | Tested configuration |
| --- | --- |
| Hardware | Freenove ESP32-S3 WROOM, 240 MHz; measurements recorded October 3, 2026 |
| Software | NuttX `2f3eb6d`, apps `85539a1`; Rust `esp-1.90.0.0` / `abf50ae2e` / LLVM 20.1.1; C GCC 14.2.0 |
| Workload | Four producers, 15 workers, one collector; 60 POSIX queues; 248-byte events containing 32 XYZ frames |
| Matched controls | Kernel configuration, queue/poll behavior, stack provisions, clock and release gate; 5,760 event/reply pairs validated |
| Stack reservations | 83,968 bytes for 20 spawned threads, plus an 8,192-byte command stack |
| Measurement | Final linked code and initialized data, flat binary size and mutable static storage from each image |

This older fixture uses native adapters and retained formatted reporting. It does **not** measure the ordinary std inbox implementation above. Original source hashes, configuration identity and per-image records are in the [packet evidence](../tests/service-footprint/results/esp32s3-2026-10-03.json). The later cleanup checkout is not byte-identical to those measured sources.

### Assumptions and accounting boundaries

- **Native heap includes Rust allocations and worker storage.** Rust requested-byte counters are a subset, so adding the two would double-count memory.
- **Heap growth is relative to a warmed workload baseline.** It is neither total firmware RAM nor a per-channel price. Linked resident RAM, baseline heap and platform reservations remain separate.
- **Equal capacity does not mean equal semantics.** At 20 owners both inbox variants hold 480 events, or 30,720 payload bytes. Crossbeam provides three independently admitted queues per owner; std provides one shared FIFO.
- **Image contents differ from storage span.** A flat binary includes layout gaps. The full ELF file also contains non-deployed metadata. Neither is interchangeable with linked code/data.
- **Lifecycle observations are bounded by the exercised paths.** A stable retained allocation is not evidence of a continuing leak, and observed stack high water is not a worst-case bound.

## RAM results

<a id="ram"></a>
<a id="ordinary-rust-scaling-controls"></a>

| Image / workload | Linked resident RAM | Observed native heap increase |
| --- | ---: | ---: |
| Instrumented baseline | 57,688 B | Reference baseline |
| 20 std workers | 57,704 B | 90,448 B |
| 20 services with std inboxes | 57,720 B | 124,064 B |
| 20 services with Crossbeam selection | 62,040 B | 152,232 B |

The std inbox configuration added 33,616 bytes of observed heap growth beyond the thread-only control. Its inline payload capacity accounts for 30,720 bytes; the remainder includes queue/control storage and allocator effects. The Crossbeam configuration used more storage under its different queue and selection contract. These differences describe the tested 20-owner cases, not universal per-owner costs.

After joined teardown, worker cases retained 112 bytes above the warmed baseline. Queue-only controls returned to baseline. The repeated lifecycle controls distinguish this observed retained state from the storage released with queues and workers. [Physical RAM results](../tests/rtos-characterization/PHYSICAL-RESULTS.md#ram).

## Native RAM control results

| Twenty workers and 30,720 payload bytes | NuttX C | Zephyr C |
| --- | ---: | ---: |
| Equal 4,096-byte stack requests | 218,948 B | 204,076 B |
| At least 4,096 **usable** bytes per worker | **223,748 B** | **204,076 B** |

NuttX's 4,096-byte request yielded 3,864–3,872 usable bytes; its 4,336-byte request yielded 4,104–4,112. Zephyr provided exactly 4,096. The latter control therefore retains 8–16 extra aligned bytes per NuttX worker. All passes produced the same total RAM. NuttX totals combine ELF reservations and observed native heap high water; Zephyr's reserved stacks and heap arena already belong to its ELF. The lower apparent NuttX total with equal requests does not provide equal usable capacity.

The final **19,672 B / 9.6%** difference reconciles to +27,656 B queue storage/controls, +6,864 B worker storage, −13,304 B RAM instructions/vectors, and −1,544 B net remaining reservations/baseline/measurement storage. Equal payload and usable stack budgets do not explain the difference. The [detailed ledger](../tests/rtos-characterization/WROOM-REVIEW.md#ram-separate-requests-usable-stacks-and-queue-costs) identifies queue headers, allocator alignment, descriptor-related creation and spare preallocated pool storage. These are configured native API costs, not a Rust tax or unavoidable fixed kernel memory. Code-placement policies also differ from each other and from the MPSC timing images.

<a id="image-size"></a>
<a id="final-packet-fixture"></a>
<a id="which-choices-mattered"></a>

## Image-size results

| Matched packet image | C | Rust | Rust minus C |
| --- | ---: | ---: | ---: |
| Linked code + initialized data | 135,400 B | 155,418 B | +20,018 B |
| Flat binary | 214,440 B | 215,116 B | +676 B |

Rust retained about 14.8% more linked code/data in this fixture. Its much smaller binary-size difference reflects image layout and padding; it does not mean the additional code occupies only 676 bytes. Mutable static storage also increased by 104 bytes. Detailed section accounting remains in the [original packet study](history/rust-footprint-study.md#final-packet-fixture).

<a id="a-memory-budget-you-can-use"></a>
<a id="what-grows-with-another-owner"></a>
<a id="one-time-versus-recurring-growth"></a>
<a id="speed"></a>
<a id="keep-footprint-and-speed-separate"></a>
<a id="what-to-adapt"></a>
<a id="measure-your-application"></a>

## Interpretation

**Stack provision dominates the ordinary std worker configuration.** Twenty 4,096-byte requests reserve 81,920 bytes before native metadata. That provision is about 91% of the 90,448-byte thread-only heap increase and 66% of the std-service increase. These are comparisons with requested capacity, not allocator attribution. They explain this application's total, not its excess over another RTOS.

**Inline payload capacity dominates the added std inbox storage.** The 30,720 payload bytes account arithmetically for about 91% of the 33,616-byte increase above the thread-only control. Crossbeam adds 28,168 heap bytes and 4,320 linked resident bytes over the std-service image. Its three queues and selection machinery provide different admission isolation; these aggregate differences do not isolate selector overhead. Consolidating queues saves resources only if a shared FIFO preserves the required stop/control behavior.

**The packet image difference is primarily instructions.** Of the 20,018 extra linked bytes, 19,262 are instructions, 676 read-only data and 80 initialized RAM data. The section ledger establishes where the bytes reside, but does not assign them all to formatting, runtime support or generic specialization. Shared support can link once, while each new type or handler may retain specialized code. The small flat-binary delta reflects padding, not free executable storage. [Section breakdown](history/rust-footprint-study.md#final-packet-fixture).

**The native control identifies recurring costs without calling the remainder fixed RTOS overhead.** Zephyr's separately linked images add 4,256 B per worker: 4,096 stack and 160 object/entry/observation bytes. NuttX's one-worker control adds 4,328 heap plus 28 static bytes for an equal request, before whole-section alignment. Raising NuttX's request to meet the usable-stack floor adds 4,800 B across twenty workers. Queue bookkeeping is still the largest positive cross-RTOS difference. Native POSIX message nodes and descriptor-backed queues have different storage policies from both Zephyr's static queue and Rust's typed ring. The older October ledger remains historical; its 15,728-byte difference must not be combined with these new values.

## Conclusions

The first RAM optimization should target provisioned execution and payload storage. Budget one worker for an execution need, choose queue capacity from the tolerated burst, and keep large buffers out of inline messages when a bounded ownership transfer suffices. A buffer handle changes where memory is counted; the backing pool still belongs in the budget.

For image size, inspect retained symbols and their callers before removing APIs. Removing required thread cleanup or diagnostics changes the product contract. The measured packet delta is not a charge repeated for every new service, and these fixtures omit costs such as networking and model state.

### Focused follow-up

- **Target the measured native queue cost before changing execution structure.** Review spare pool capacity and descriptor/message bookkeeping against the required admission semantics. Compare one change with the same saturation and lifecycle checks. Smaller stacks require actual handler, error and teardown coverage; the observed 720-byte NuttX / 372-byte Zephyr worker high water does not qualify a new safe size. Neither zero-worker image is a bare-kernel baseline: application and queue reservations remain linked.
- **Attribute flash growth with one matched build change.** Use the final map/symbol inventory to select the largest retained optional component, rebuild with it removed or simplified, and compare linked sections as well as binary span. Keep behavior checks fixed; do not infer current savings from the older fixture's formatting controls.

## Evidence and reproduction

- [Physical RAM summary](../tests/rtos-characterization/PHYSICAL-RESULTS.md#ram), [ordinary Rust fixture](../tests/ram-footprint-std/README.md) and [separate host allocation records](../tests/ram-footprint-std/results/README.md).
- [Matched packet fixture](../tests/service-footprint/README.md), [recorded image data](../tests/service-footprint/results/esp32s3-2026-10-03.json) and [original footprint study](history/rust-footprint-study.md). The archive retains the earlier entry, formatting and computation controls.
- [Allocation observation](memory-qualification.md), [native heap and stack measurement](nuttx-memory-qualification.md) and [application budgeting procedure](../website/guide/performance.md).
- [Bounded-channel implementation](std-channel-performance.md) and [message-delivery comparison](rtos-comparison.md) for the separate timing question.
