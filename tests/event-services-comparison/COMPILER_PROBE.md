# Isolating and correcting the compiler timing gap

Two compiler issues account for the repeated arithmetic cost: instruction
scheduling and final hardware-loop alignment. Neither is a Rust queue,
allocation, or std call. The sixth patch fixes scheduling; the original
isolated probe then matches GCC's six-cycle loop, but its GNU assembly step
also fixes alignment. The six-patch native Rust object still has a misaligned
loop. The [handler diagnostic below](#native-object-alignment-follow-up)
separates these effects, and the rebuilt-driver check confirms the automatic
alignment correction without changing application code. This is a result for
this recurrence and CPU, not a general language ranking or deadline guarantee.

The subsequent [broad arithmetic qualification](../arithmetic-parity/RESULTS.md)
checks 1,136 cases on the device with no oracle failures, but still finds
64-bit and branch-heavy code-generation gaps and differing math-library costs.
That follow-up must not be described as full C/Rust performance parity.

## Measured evidence

The [numeric report](results/esp32s3-compiler-isolation-2026-10-05.json) retains
all samples, generated assembly hashes, image identities, patch hashes and
verified restoration. Before and after are separate cohorts, not pooled runs.

| Compiler | First five patches: short median cycles/iteration | Six patches: short median | Six patches: long normal median |
| --- | ---: | ---: | ---: |
| C / GCC | 6.051 | 6.051 | 6.01424 |
| C / LLVM | 7.041 | 6.041 | 6.01423 |
| Rust / LLVM | 7.041 | 6.041 | 6.01423 |

Short tests include function and counter-read overhead, so these are not
exact instruction latencies. In each cohort, one first-repeat C/GCC flash
sample takes 3,287 cycles rather than 3,098; it is retained, not discarded.
The other C/GCC short samples are 3,098 cycles. Each LLVM control's short
samples change from 143 readings of 3,605 cycles and one of 3,606 to 144
readings of 3,093 cycles. The median drops by exactly 512 cycles for 512
iterations. The long tests support the same sustained improvement.

Each cohort has 48 linked functions: three compiler controls, flash/IRAM
placement and eight entry offsets within a 32-byte span. Offsets are 0, 4,
8, …, 28 because Xtensa `ENTRY` must be word aligned. The tool changes only
symbols, sections and entry padding in compiler-generated assembly, never
the instructions. Function prologues differ, so entry alignment is not a
claim of identical loop addresses. Actual linked addresses are validated.

Each function is warmed before timing. The short control uses 512 iterations
with interrupts masked, repeated nine times. Long controls use 400,000
iterations, repeated three times, with:

- Normal scheduling and interrupts.
- `sched_lock()`, with interrupts still enabled.
- A controlled competing POSIX thread that runs for about 100 µs and sleeps
  for 1 ms. This is not the twenty-service workload.

That gives 864 samples per cohort. All results pass an independent Python
wrapping-arithmetic oracle, with zero mismatches. The long counter readings
include interruptions and preemption; they are elapsed CPU-counter cycles,
not measurements of on-task execution time. This warm-cache sweep measures
GNU-assembled loops. GNU `as` automatically aligns the first loop instruction;
the entry-offset sweep therefore does **not** qualify alignment in LLVM's
integrated object output or cold-cache behavior.

## Why GCC did not have this gap

The same portable C source compiled through the pinned LLVM backend shows
the same gap as Rust. This separates the backend from the source language.
Before patch 6, LLVM places the consuming ADD immediately after `MULL`.
GCC places the independent XOR between them. The two loops have the same
six body instructions, but LLVM pays one extra dependency cycle each time.

The CPU-specific, incomplete scheduling model describes the measured
two-cycle low-product result latency and single-issue width. The compiler
selects LLVM's model-aware machine scheduler **after register allocation**;
there is no application feature, inline arithmetic assembly, or LLVM flag.
Other CPU profiles keep their existing scheduling behavior. Unmodelled
instructions retain fallback estimates; this is not a complete ESP32-S3
pipeline model.

Qualification exposed an additional compiler issue: a scheduler could move
the body ahead of `LOOPSTART`, collapsing a hardware loop to one multiply.
The patch makes `LOOPSTART` a scheduling boundary. A regression checks that
the loop remains before its multiply with both default scheduling and the
explicit pre-allocation scheduler test control. All 99 Xtensa tests pass
(63 CodeGen and 36 MC). Two existing tests retain their full instruction
checks with refreshed order; IR, calls and stack-frame sizes are unchanged.
The discarded pre-allocation and legacy-scheduler prototypes were not used
in the reported after cohort.

## Reproduce without changing an installed SDK

First build the evaluation compiler using the shared
[local/CI setup](../../upstream/rust-llvm/BUILDING.md). Keep its source pins,
patch ledger, actual tool hashes and qualified std snapshot with the result.
Use the unchanged [C](compiler-probe.c) and [Rust](compiler-probe.rs) wrappers
to emit generated assembly. The Rust wrapper requires the matching embedded
`core` and `compiler_builtins` rlibs. To isolate backend scheduling, retain
optimized C/Rust LLVM IR and feed the same IR to the five-patch and six-patch
`llc` builds at `-O2 -mtriple=xtensa-esp-unknown-elf -mcpu=esp32s3`.

The C frontend in this measurement is Espressif clang 20.1.1 from
[esp-20.1.1_20250829](https://github.com/espressif/llvm-project/releases/tag/esp-20.1.1_20250829),
not the backend pin in `upstream.json`. The report distinguishes that
frontend release, the private backend and the GNU 14.2.0 control. The old
`llc` binary hash was not captured before its incremental rebuild; retained
source/patch, assembly and image identities identify that control. The new
`llc` hash is recorded. No old binary hash is inferred.

```sh
python3 tests/event-services-comparison/compiler_probe.py prepare \
  --c-gcc /absolute/path/to/c-gcc.s \
  --c-llvm /absolute/path/to/c-llvm.s \
  --rust-llvm /absolute/path/to/rust-llvm.s \
  --out /absolute/path/to/fresh-layout
```

Assemble the generated layout with GNU `as --text-section-literals`. Compile
[compiler-probe-main.c](compiler-probe-main.c) against the matched NuttX
configuration and generated header, using `-std=c99 -O2 -mlongcalls`. Combine
both objects with `ld -r`; register the input ELF as `compiler_probe` through
the existing NuttX app wrapper (`NXRS_STD_ELF`, priority 100, stack 8,192).
Use the same kernel configuration and libraries for both cohorts. The local
evaluation checks kernel archive/member identities, allowing only NuttX's
build timestamp metadata to differ. This driver is diagnosis-only, not an
application HAL or a replacement for the service example.

Use a protected, independently retained **full 16 MiB flash backup**. The
collector is local POSIX-only, requires sole access to the device, validates
all outputs and restores/verifies that backup in `finally`, including on a
failed run. Do not run it concurrently with another serial or flashing tool.

```sh
python3 tests/event-services-comparison/compiler_probe.py measure \
  --image /absolute/path/to/image.bin \
  --backup /absolute/path/to/protected/full-flash-backup.bin \
  --out /absolute/path/to/fresh-private-measurement \
  --port /device/serial-port --flasher /absolute/path/to/esptool
```

`compiler_probe.py export --help` describes the compact before/after export.
It reparses serial evidence, checks its hashes and restoration, matches
firmware sources/configuration and verifies patch identities. Keep backups,
device identifiers and raw logs private. Host tests exercise malformed,
missing and duplicate samples, completion handling, layout preservation,
export validation and restoration after a flashing failure.

## Full service check with the sixth patch

The separate [service report](results/esp32s3-compiler-scheduled-2026-10-05.json)
reruns unchanged twenty-service, sixty-queue images: five cases, four profiles,
two rotated blocks and two runs per profile/block. All 80 invocations accept
and receive their 312,000 offered messages, with zero rejections or protocol
errors. Every CPU/I/O run completes its 117 offered jobs. Normal, medium-CPU
and simulated-I/O profiles have zero deadline misses. Original full-flash
restoration is verified. The C and Zephyr image hashes are unchanged from
the [five-patch cohort](ARITHMETIC.md); the Rust images use the rebuilt compiler.

| Long profile, 400,000 iterations/job | Longest handler | Worst peer post-to-handler response | Deadline misses, four runs |
| --- | ---: | ---: | ---: |
| NuttX C | 11.133 ms | 10.920 ms | 11 |
| NuttX Rust, six patches | 13.759 ms | 11.228 ms | 50 |
| Zephyr C | 10.879 ms | 10.035 ms | 0 |
| Embassy natural waits, six patches | 10.032 ms | 21.255 ms | 16 |
| Embassy chunked, six patches | 13.615 ms | 1.251 ms | 28 |

These maxima are observations, not hard bounds or pooled distributions.
NuttX Rust's maximum falls from 15.902 ms in the previous cohort, but remains
about 24% above C's maximum here. This original cohort does not apportion the
remaining costs. The separate diagnostic below finds a final-object alignment
penalty that the GNU-assembled isolated probe did not expose. Keep its
instrumented-kernel timings separate from this table.

Faster arithmetic does not make a cooperative executor preempt a handler.
Natural Embassy's worst peer **scheduled-release-to-handler** response is
31.536 ms, including delayed publication; chunked Embassy reaches 3.454 ms.
The chunked policy protects peers but adds handoff time to its own handler,
and still misses deadlines. Loss-free delivery is not deadline qualification.

| Image | Code + initialized data | Flash binary size | Resident ELF RAM |
| --- | ---: | ---: | ---: |
| NuttX C | 176,884 B | 214,508 B | 104,384 B |
| NuttX Rust, six patches | 177,724 B | 214,532 B | 104,392 B |
| Zephyr C | 88,167 B | 142,312 B | 231,208 B |
| Embassy natural, six patches | 69,697 B | 182,656 B | 87,256 B |
| Embassy chunked, six patches | 69,857 B | 182,816 B | 87,256 B |

The new patch changes neither NuttX Rust's code/data total, binary size nor
resident RAM. Its matched C delta remains **840 B / 24 B / 8 B**. Embassy's
natural/chunked code/data totals fall by 56/100 bytes relative to the five-patch
images; resident RAM is unchanged. Debug sections are excluded from these
flash totals. Binary packaging, boot components and padding account for the
gap between code/data and `.bin` size.

Resident ELF RAM excludes dynamic reservations. Observed NuttX traffic heap
peaks are 118,032 B for C and 118,184 B for Rust in this cohort. Adding resident
RAM gives 222,416/222,576 B, **not** a full-capacity bound or an application RAM
budget. Stack and queue settings are unchanged; the
[full-queue qualification](CONTROLS.md) still governs capacity claims. This
twenty-thread fixture is not qualified for a 250 kB product RAM budget.

The practical conclusion is that the compiler-owned scheduling fix removes
the demonstrated arithmetic penalty without an image/RAM increase. It does
not establish universal language speed parity, full service deadline parity,
or a production-ready RTOS configuration. Keep the older cohorts separate,
and retain wider loop/interrupt and firmware qualification before activating
the patchset as a default toolchain.

## Native object alignment follow-up

The [numeric evidence](results/esp32s3-handler-alignment-2026-10-05.json)
contains two separate cohorts, each with eight rotated C/Rust invocations,
936 completed long-work jobs and 31,200 delivered messages. There are zero
rejections or protocol errors, but deadline misses remain. Full original
flash restoration is verified after each cohort.

A diagnostic NuttX build adds scheduler-switch and IRQ hooks to both apps.
The unchanged service handler is timed with the hardware cycle counter.
The hooks divide that interval into target-thread, other and IRQ buckets.
These are hook-boundary measurements, not exact instruction-only CPU
attribution: callbacks and parts of context switches are included. Diagnostic
code, kernel configuration and kernel object identities match between C and
Rust; this is not a new baseline size or RAM comparison.

| Diagnostic cohort | C median target-thread interval | Rust median target-thread interval | Rust minus C |
| --- | ---: | ---: | ---: |
| Native LLVM object | 10.025 ms | 11.697 ms | 1.672 ms |
| Same generated Rust assembly through GNU `as` | 10.025 ms | 10.027 ms | 0.002 ms |

Each median uses 468 jobs within its own cohort. Before reassembly, Rust takes
401,247 extra cycles for 400,000 iterations: approximately one extra cycle
per iteration. The GNU control keeps the generated loop arithmetic and its
six-instruction order, while correcting alignment. It also changes some
padding and density encodings elsewhere, so it is a diagnostic control, not
a proposed application workaround or proof of universal speed parity.

The native linked Rust loop starts its three-byte `ssai` at `0x4202653e`,
crossing the `0x42026540` fetch boundary. The control starts it at
`0x42026538`, within one word. A dump of the compiler passes shows why:
hardware-loop fixup chooses safe padding, then branch relaxation adds bytes
earlier in the function and invalidates it. LLVM's pinned integrated assembler
does not repair that alignment. GNU `as` explicitly does.
[GNU Xtensa alignment documentation](https://sourceware.org/binutils/docs/as/Xtensa-Automatic-Alignment.html)
and the
[pinned LLVM pass order](https://github.com/espressif/llvm-project/blob/14b9f5575f37489d4c25c069d04c9ed216dadc31/llvm/lib/Target/Xtensa/XtensaTargetMachine.cpp)
describe the relevant behavior.

The corrected control's observed longest handler is 12.552 ms, versus
11.758 ms for C in that cohort. Removing the repeated arithmetic penalty
does not remove all scheduling, interruption or fixed-handler differences.
These maxima are observations, not bounds; full-service deadline parity is
still unproven.

The [probe](handler-probe.c) and [adapter/parser](handler_probe.py) are
diagnosis-only. `handler_probe.py adapt --out NEW_DIRECTORY` produces isolated
copies of `runtime.c` and `platform_nuttx.c`; build those with the probe and
unchanged core/HAL sources in a separate, single-core NuttX tree with switch
and IRQ instrumentation enabled. Disable note-buffer logging; preserve the
baseline timer, stacks and workload. Link the frozen C/Rust inputs against
the same diagnostic kernel. `handler_probe.py parse CAPTURE` validates the
117 job identities, arithmetic category sums and completion marker. The
default clock/accounting skew limit is 256 cycles; this exported diagnostic
explicitly uses 4,096 cycles (17.1 µs) after retaining larger counter-read
outliers. Their exact cause is not established. No samples are discarded.

A [final-alignment compiler proposal](../../upstream/rust-llvm/proposals/0007-Xtensa-align-hardware-loops-after-layout.patch)
remains under qualification, outside the active evaluation series. Its
private backend passes all 102 Xtensa tests, including a direct-object
regression that fails with the sixth-patch backend. Its three-profile matrix
decodes loop and near/expanded-branch targets against independent block
markers, with six negative checker controls. Inline assembly anywhere in a
function selects software-loop lowering; the final pass does not guess its
emitted length. Simply moving
the original loop-fixup pass last is **not** safe: a local prototype fails two
existing regression tests, including a nested-loop compiler assertion. Do
not deploy that prototype, GNU reassembly, application arithmetic assembly
or a feature flag as a fix. Qualification needs direct LLVM object and final
linked-image checks, not only assembly-text tests.

### Rebuilt Rust-driver confirmation

The public setup helper completed a fresh six-patch build and produced an
app input byte-for-byte identical to the earlier six-patch input. A separate
evaluation then applied the alignment proposal, rebuilt LLVM and the actual
Rust driver, and rebuilt the unchanged application and embedded std through
Cargo. The original six-patch compiler package and installed SDK are untouched.
Compiler/driver, patch, std-input and final firmware identities are bound in
the [new numeric record](results/esp32s3-handler-rust-driver-2026-10-05.json).
This is native Rust object output, not GNU reassembly or standalone `llc`
output from saved IR.

The matched instrumented test completes another eight rotated invocations:
936 long-work jobs and 31,200 deliveries, with no rejections or protocol
errors. Original full-flash restoration is verified. The median target-thread
interval is **10.025 ms for C and 10.027 ms for Rust**, a 535-cycle difference
(about 2.2 µs) over 400,000 iterations. The linked Rust first-body instruction
is word-aligned at `0x42026540`. The repeated one-cycle-per-iteration penalty
is gone in this workload without changing application code.

The longest observed instrumented wall intervals are 11.691 ms for C and
13.038 ms for Rust. Those intervals include interruptions and time away from
the target thread. They are not bounds, and the close target-thread medians
do not establish equal completion deadlines. The inline-assembly fallback is
covered by compiler code-shape regressions; its wider execution semantics and
general loop/interrupt safety remain part of activation qualification.

### Uninstrumented service confirmation

The separate [native-driver service report](results/esp32s3-compiler-aligned-2026-10-05.json)
uses the normal kernel, not the diagnostic hooks. It compares only matched
NuttX C/Rust: four profiles, two rotated blocks and two runs per profile/block.
All 32 invocations deliver their **124,800 messages**, with no rejections or
protocol errors. CPU/I/O runs finish all 117 offered jobs; normal, medium-CPU
and simulated-I/O runs have no deadline misses. The original full flash is
restored and verified. Application sources, std inputs, kernel configuration
and C image stay unchanged; the Rust image uses the rebuilt driver above.

| Uninstrumented long-work observation | C | Rust |
| --- | ---: | ---: |
| Longest handler | 11.311 ms | 11.762 ms |
| Worst peer post-to-handler response | 10.921 ms | 22.541 ms |
| Control deadline misses, four runs | 1 | 16 |

These are observed maxima and counts, not bounds or a pooled comparison with
older cohorts. Equal arithmetic throughput does **not** imply equal service
response: fixed handler work, interruption and scheduling remain in this
measurement. Their remaining contributions are not apportioned here. Zephyr
and Embassy were not rerun with the alignment proposal; their earlier results
must not be presented as a new four-RTOS comparison.

The final Rust image has 177,728 B of code + initialized data, a 214,532 B
flash binary and 104,392 B of resident ELF RAM. Its matched C deltas are
**844 B / 24 B / 8 B**, respectively. Alignment adds four code/data bytes
relative to the six-patch Rust image, without changing binary or resident RAM
size. Observed traffic heap peaks are 117,552 B for C and 118,104 B for Rust
(a 552 B difference). Resident plus observed heap is 221,936/222,496 B;
this includes the platform, stacks and benchmark resources, excludes the
full-queue capacity bound, and does not qualify a 250 kB product RAM budget.
