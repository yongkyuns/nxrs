# RAM controls after the footprint review

These controls distinguish configured whole-system memory, runtime allocation
increments, requested stack budgets, usable stack observations and chosen
payload capacity. They create new evidence; they do not revise the frozen
October measurements or establish a minimum production footprint.

The historical full-telemetry NuttX Rust / Embassy ratio was 2.83×. The matched
lean images were 221,108 B / 61,172 B, a 159,936 B gap. Of this, 69,408 B was
the difference between twenty 4 KiB worker requests and Embassy's 8 KiB shared
stack plus 4,320 B service task pool. The task pool includes executor headers
as well as future state. Both sides had 30,720 B of payload capacity. There
were no per-worker stack observations proving the 4 KiB requests necessary.
See the [review](../../docs/rtos-comparison.md#what-the-ram-ratio-establishes)
for the accounting limits.

## Experiments

| Control | What it isolates | Accounting limit |
| --- | --- | --- |
| `alloc-baseline` | Initialized command/coordinator environment | The linked fixture still contains static platform and adapter objects |
| `alloc-threads-0/1/5/10/20` | Gates and native worker lifecycle without opening queues | NuttX heap deltas include native thread resources; Zephyr's entire static stack array exists at every count |
| `alloc-queues` | Empty, full, drained and released queues without workers | Statically allocated buffers remain in the baseline; full capacity is 480 × 64 B |
| `normal`, `burst`, `work-medium`, `work-long`, `io-wait` | Stack use under different existing workloads | Observed high water does not cover all error, interrupt or teardown paths |
| `saturation` | Three full/drain cycles while all twenty service contexts are alive | Reserve all slots even when normal traffic rarely fills them |
| [Ordinary std controls](../ram-footprint-std/README.md) | Real std threads, Nxrs inboxes, and Crossbeam construction/wait/cleanup | A separate workload and common NuttX configuration; host requested bytes are not MCU RAM |

Native allocation output has a terminal `ES_ALLOC_RESULT`, an `ES_PASS`, and
the console's successful `ES_COMMAND_EXIT`. Every phase identifies requested
and live workers, active queues, heap availability, payload capacity and
adapter static storage. `static_scope=adapter` deliberately excludes other
linked application/kernel storage. Diagnostic bytes are an identified subset
of static storage, not an additional allocation.

`worker_stack_requested` is the sum of requests for live workers.
`worker_stack_reserved` is unavailable for NuttX allocation snapshots; native
heap deltas measure the actual combined cost. Zephyr reports
`sizeof(worker_stacks)` with reservation availability set, including any
compiler/architecture padding. Its reservation and payload buffers remain
present even in the zero-worker baseline. A zero heap delta does not imply a
zero-memory Zephyr thread or channel.

The Zephyr helper uses effective `K_HEAP_MEM_POOL_SIZE`, including a board-added
arena when configured `CONFIG_HEAP_MEM_POOL_SIZE` is zero. Occupancy and
reserved arena size are separate. The historical whole-image ledger already
counted the arena through its ELF section.

## Build a new cohort

The existing defaults remain 4,096 B per native worker and 8,192 B for Embassy's
shared stack. The builder now accepts:

```text
--worker-stack-bytes BYTES   native pthread/k_thread request
--shared-stack-bytes BYTES   Embassy main/executor/interrupt reservation
--stack-watermark            enable observation support
```

Requests must be aligned to 16 bytes. Validation is a build constraint, not a
claim that the smallest accepted request is executable. Every fresh provenance
record includes both sizes, observation state and `ram_contract=1`. The
Embassy linker reservation is generated locally; the ELF `.stack` must exactly
match the requested size. The older fixture's `stack.x` is unchanged.

For a baseline using the pinned comparison tools, from the repository root:

```sh
python3 tests/event-services-comparison/setup.py \
  --platform all --root target/ram-tools --build --layout three \
  --instrumentation lean --worker-stack-bytes 4096 \
  --shared-stack-bytes 8192 --stack-watermark \
  --out target/ram-images-baseline
```

This builds the Zephyr and Embassy case directories with 1 ms timers and
natural Embassy scheduling. Prepare NuttX through the
[existing minimal-profile recipe](README.md#minimal-nuttx-configuration), using
the same common baseline and prepared compiler/sysroot for C and Rust. Add
`--instrumentation lean --worker-stack-bytes 4096 --stack-watermark` to both
`build.py` invocations, and place their fresh case directories alongside the
other two under `target/ram-images-baseline`.

Do not reuse a historical `--rust-input-bundle` compiled before these controls:
it lacks the current firmware sources and observation contract. Any changed
compiler, SDK, kernel configuration or optimization policy is recorded as a
new cohort. In particular, current tool pins need not match October's Zephyr
revision. Keep C/Rust substrate pairs identical within a new comparison.

After observing the baseline on a device, choose smaller requests from the
measurements plus a declared margin. For example, test 2,048 B native workers
only when observed usable headroom supports that candidate, then consider
1,024 B only if the new observations and exceptional-path checks support it.
An Embassy 4,096 B reservation is a separate candidate; a successful link
does not establish that it runs safely. For this instrumented lean build, the
entry frame alone exceeds 1 KiB, so a 1 KiB shared reservation is not a useful
starting candidate. Rebuild into fresh directories for every size.

## Stack observation coverage

| Scope | Observation point | Limitation |
| --- | --- | --- |
| Native worker | Current live TCB, after work and before thread return | Omits later native/std teardown and unexercised branches; the scan itself contributes |
| NuttX main / idle / IRQ | Coordinator snapshot through supported native APIs | Main reporting after that point is not covered; unrelated system work can contribute |
| Zephyr main / idle / workqueue | Public stack-space API | Depends on stack initialization/info support; IRQ is explicitly unavailable |
| Embassy shared stack | Cumulative scan of a region painted once at main entry | Omits earlier reset/runtime startup and includes previous profiles/console/interrupt activity |

`ES_STACK` distinguishes `requested`, `reserved` usable capacity, `used`,
`available`, and `capture`. Missing support is an unavailable observation,
never a measured zero. The collector requires observations for every service
worker, or the single Embassy shared stack, before accepting a run. Main/OS
unknowns remain visible rather than being silently removed.

The Embassy observer preserves the HAL's stack-bottom guard, excludes the
bottom 128 B, protects another 64 B inside its painted region, and stays
256 B below the live stack pointer. Interrupts are masked only for bounded
paint/scan loops, which make no calls and do not adjust SP. The unpainted top
is conservatively charged as used. Coverage, painted/unpainted bytes and guard
state are emitted; inconsistent coverage or a damaged guard fails collection.
It never repaints between profiles. Therefore profile-specific minima cannot
be inferred from decreasing watermarks.

Declare a margin before selecting a candidate. A useful experimental rule is
at least `max(256 B, ceil(25% × largest observed use))` of remaining usable
capacity above protected/guard bytes for **every** required stack. This is an
explicit engineering choice, not a proven worst-case bound or automatic
production approval. Exercise error handling, formatting, interrupt nesting,
startup and teardown separately; increase the margin or retain larger stacks
when coverage is missing. Keep requested, usable, observed and margin bytes
in the final table. Do not silently subtract a watermark from whole-image RAM.

## Collect on a physical board

Use one controlled single-core ESP32-S3 without PSRAM, and the private verified
16 MiB original-flash backup described in the existing device recipe. The
collector validates frozen image/ELF/configuration hashes and restores and
verifies that backup in `finally`, including after a failed invocation:

```sh
python3 tests/event-services-comparison/ram_matrix.py \
  --artifacts target/ram-images-baseline --out target/ram-runs-baseline \
  --backup /private/path/device-before.bin --port /dev/ttyACM0 \
  --flasher /path/to/esptool --runs 3
```

The output directory must be new. The default set is NuttX C, NuttX Rust,
Zephyr C and Embassy, all with three queues per service. Each native image
runs all seven allocation controls and six workload/capacity controls; Embassy
runs the six workload/capacity controls. Invocation order rotates by repetition.
The image remains booted across profiles, so allocator state is warmed by
earlier work. NuttX `maxused` and the Embassy watermark are cumulative since
boot, not independently reset per-profile peaks. Use the ordinary std fixture's
fresh-boot recipe to study first-use behavior separately.

`ram-matrix.json` retains source/artifact identities, raw serial hashes,
validated phases and stack observations, and a whole-image summary. Raw serial
and backup identity are private evidence; review and sanitize before publishing.
The summary adds NuttX resident ELF RAM to its observed native heap peak once.
It does not add worker-stack requests again. Zephyr/Embassy static buffers,
stacks and arenas already belong to ELF RAM; heap occupancy and stack watermarks
are explanatory subsets. Unavailable heap measurements leave the total unknown.

## Verification and evidence status

Host tests cover native lifecycle cleanup, capture before TCB destruction,
effective Zephyr heap sizing, static reservations, missing/contradictory stack
rows, build/configuration identity and serial failure capture. The Embassy
instrumentation has compiled and linked with the pinned Xtensa toolchain at
4,096 and 8,192 B shared reservations. This validates target code and linker
wiring, not physical stack safety. The ordinary Rust fixture has separately
recorded 78 successful host invocations.

No physical RAM observations for this new cohort are committed here. The
original historical totals remain the only measured cross-platform target
comparison. Populate a new dated result only after actual board collection;
report whole-system totals and per-resource increments separately.
