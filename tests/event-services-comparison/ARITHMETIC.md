# Xtensa arithmetic: compiler diagnosis and measured patchset

This follow-up separates the synthetic handler's compiled arithmetic from
queue and scheduling costs. The [earlier scheduling results](SCHEDULING.md)
remain historical measurements of portable Rust, not of the assembly control below.

The first five patches in the private [compiler patchset](../../platform/rust-llvm/README.md)
were built and tested on the ESP32-S3. That cohort removes the demonstrated CPU overload in this
workload, but **does not establish full C speed or deadline parity**. The
application uses unchanged portable Rust, with no assembly helper or
arithmetic feature flag. Installed SDKs and upstream checkouts are untouched.

The separate [sixth-patch control](COMPILER_PROBE.md) now identifies and removes
the remaining one-cycle arithmetic gap in both C/LLVM and Rust/LLVM. It uses
warm-cache cycle measurements, not the service timings below. Keep these
cohorts distinct; the following 80-run service results use the first five
patches and retain their original firmware and compiler identities.

## What was slow

Every work iteration rotates a 32-bit state left by five, multiplies it by
`0x9e3779b9`, and adds `token XOR (iteration * 0x7f4a7c15)`. All arithmetic
wraps at 32 bits. There is no formatting, allocation, queue operation or std
thread call inside this loop. C and Rust execute the same recurrence; the
result becomes observable service state.

Both Rust configurations use Espressif rustc `1.90.0-nightly`, commit
`abf50ae2e46066e67e29fe856f9764c23aa9a3ca`, with LLVM 20.1.1. Its pinned
[LLVM source](https://github.com/espressif/llvm-project/tree/14b9f5575f37489d4c25c069d04c9ed216dadc31)
lets us distinguish a target backend problem from a Rust source-language cost.

| Measured loop | Rotation | Loop control | Instructions per iteration |
| --- | --- | --- | ---: |
| NuttX C | `ssai` + `src` | Hardware `loop` | 6 |
| Portable Rust, NuttX/Embassy | `extui` + `slli` + `or` | Counter update + conditional branch | 9 |
| Portable Rust, hardware-loop compiler flag | `extui` + `slli` + `or` | Hardware `loop` | 7 |
| Portable Rust, private patched compiler | `ssai` + `src` | Hardware `loop` | 6 |
| Discarded assembly control | `src`, with shift setup before the loop | Hardware `loop` | 5 |

The Rust front end correctly emits `llvm.fshl.i32(value, value, 5)` in
optimized LLVM IR. SelectionDAG's
[funnel-shift combiner](https://github.com/espressif/llvm-project/blob/14b9f5575f37489d4c25c069d04c9ed216dadc31/llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp#L11049-L11056)
converts a funnel shift with identical inputs into a rotate. The pinned Xtensa
[operation actions](https://github.com/espressif/llvm-project/blob/14b9f5575f37489d4c25c069d04c9ed216dadc31/llvm/lib/Target/Xtensa/XtensaISelLowering.cpp#L250-L258)
mark rotates for expansion. The
[legalizer](https://github.com/espressif/llvm-project/blob/14b9f5575f37489d4c25c069d04c9ed216dadc31/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp#L3810-L3814)
then expands that rotate into shifts and OR. This bypasses the backend's
custom funnel-shift lowering: this is the exact reason for the three rotate
instructions.

Separately, Xtensa's
[target optimization policy](https://github.com/espressif/llvm-project/blob/14b9f5575f37489d4c25c069d04c9ed216dadc31/llvm/lib/Target/Xtensa/XtensaTargetTransformInfo.cpp#L15-L40)
initializes `disable-xtensa-hwloops` to **true**. It vetoes hardware-loop
formation before the later conversion pass can act. Simply enabling `+loop`
is not a fix: that instruction-set feature is already enabled.

Local controls found that `-O3` leaves the same nine-instruction loop. Explicit
four-way unrolling reduces branch frequency but retains the expanded rotation
and grows a standalone function from 40 to 137 bytes. Forcing generic LLVM
hardware-loop intrinsics crashes this compiler on `llvm.set.loop.iterations`;
that option is not used in any measured firmware.

The target-private override `-C llvm-args=-disable-xtensa-hwloops=false` does
work in the standalone code-generation control: it produces a hardware loop
with seven body instructions, but leaves the expanded rotation. It was not
applied globally to std, drivers or the executor, and was not measured on the
board. The assembly control bypassed both inefficiencies in this routine.

Instruction counts identify concrete inefficiencies, not a complete cycle
breakdown. Flash alignment, instruction scheduling, interruptions and other
service work can also affect the recorded handler duration.

## Compiler-owned fix, not application assembly

The rotate patch marks 32-bit rotate nodes for custom lowering and
routes them through the existing Xtensa funnel-shift/SRC implementation. This
lets normal Rust `rotate_left` and `rotate_right` benefit without a Cargo flag,
target branch or assembly helper. The separate hardware-loop policy patch
changes the compiler's default while retaining its legality checks. That change
has a wider impact and must pass backend and firmware qualification, not just
this one arithmetic loop.

The complete series also corrects loop pseudo control-flow metadata exposed
by the machine verifier, selects immediate `SSAI` for constant shift amounts,
and regenerates four affected existing CodeGen tests without changing their
IR or `RUN` lines. The private LLVM passes all **98 Xtensa tests**: 62 CodeGen
and 36 MC, including machine verification and fallback controls. The pinned
Espressif Rust bootstrap against that LLVM completed successfully.

The app's [portable range routine](core.rs) is unchanged from the original:
it rejects index overflow and preserves the absolute iteration index when
chunked. Host tests compare with C through 400,000 iterations, verify wrapping
and chunk equivalence, and cover range overflow. The discarded device helper
was also checked against the portable routine before its timed runs; those
startup checks are no longer in the app.

No OS, toolchain, thread stack, queue capacity, release calendar, deadline or
number of arithmetic iterations was changed. The existing platform
comparison remains an application-workload comparison, not an isolated
scheduler test. In particular, faster arithmetic cannot make a cooperative
executor preempt an individual handler.

## Compiler-built ESP32-S3 results, 2026-10-05

The [patched-compiler evidence](results/esp32s3-compiler-patched-2026-10-05.json)
contains five frozen images, four profiles and four runs per image/profile in
two rotated blocks: 80 invocations. All **312,000 attempted deliveries** were
accepted and received, with zero protocol errors or rejected sends. Normal,
intermediate CPU and simulated-I/O runs have zero deadline misses. Every CPU
and I/O run completes its 117 offered jobs. The original full 16 MiB flash was
restored and verified after measurement.

| Long profile: 400,000 iterations per CPU job | Longest handler | Worst peer queue response | Deadline misses, four runs |
| --- | ---: | ---: | ---: |
| NuttX C | 11.288 ms | 10.963 ms | 3 |
| NuttX Rust, patched compiler | 15.902 ms | 11.311 ms | 60 |
| Zephyr C | 10.879 ms | 10.035 ms | 0 |
| Embassy Rust, natural waits, patched compiler | 11.696 ms | 24.611 ms | 41 |
| Embassy Rust, chunked, patched compiler | 15.216 ms | 1.318 ms | 28 |

These are observed maxima, not hard bounds. Handler duration is wall-clock
time including validation, platform calls and time off CPU. Peer queue
response starts at posting, so it excludes delayed publication. Chunked
Embassy's worst peer release-to-handler response is 9.618 ms, and its own
service still misses some deadlines. It is loss-free here, not fully
deadline-qualified. A faster compiler does not make a cooperative executor
preempt an individual synchronous handler.

A fresh, separate [unpatched NuttX baseline](results/esp32s3-compiler-baseline-2026-10-05.json)
used the same portable source, kernel configuration and four profiles:

| NuttX Rust long profile | CPU jobs per run, offered 117 | Longest handler | Rejected sends / deadline misses, four runs |
| --- | ---: | ---: | ---: |
| Original compiler | 87 | 30.067 ms | 120 / 705 |
| Private patched compiler | 117 | 15.902 ms | 0 / 60 |

The C controls stayed near 11 ms in both cohorts. The patched Rust maximum is
about 47% lower than its original maximum, but remains about 41% above C's
maximum in the new matrix. These cohorts are separate runs, not interleaved
before/after images or a pooled latency distribution. Neither ratio is an
isolated CPU-speed measurement or a general language-performance result.

### Why equal instruction counts do not establish equal speed

The final linked bodies differ in order and placement:

| Six-instruction body | C | Patched Rust |
| --- | --- | --- |
| Order | `ssai, src, mull, xor, add, add` | `xor, ssai, src, mull, add, add` |
| First / last body byte | `0x42026a88` / `0x42026a97` | `0x420261fe` / `0x4202620d` |
| Multiply result | Independent XOR before the consuming add | Consuming add immediately after multiply |
| Placement | Body within one aligned 32-byte span | First three-byte instruction crosses the next 32-byte boundary |

The instruction-order difference is proven by disassembly. A multiply-result
stall or instruction-fetch penalty was a hypothesis at this stage, not a measured cycle
breakdown. Both bodies execute from cached flash; Espressif documents that
[code placement and cache misses can affect execution](https://docs.espressif.com/projects/esp-idf/en/release-v5.2/esp32s3/api-guides/memory-types.html).
The NuttX handler also includes preemption. Embassy's much shorter monolithic
handler under the same compiler is another reason not to attribute NuttX's
entire remaining gap to arithmetic instructions alone.

The [follow-up diagnostic](COMPILER_PROBE.md) uses a flash/IRAM layout sweep
and isolated cycle measurements. It confirms the multiply dependency gap and
adds a separately tested, compiler-owned scheduling patch. The service timing
above is still the five-patch cohort, not a measurement of that sixth patch.

### Image size and RAM

| Measured image | Code + initialized data | Flash binary size | Resident ELF RAM |
| --- | ---: | ---: | ---: |
| NuttX C | 176,884 B | 214,508 B | 104,384 B |
| NuttX Rust, patched compiler | 177,724 B | 214,532 B | 104,392 B |
| Zephyr C | 88,167 B | 142,312 B | 231,208 B |
| Embassy natural, patched compiler | 69,753 B | 182,704 B | 87,256 B |
| Embassy chunked, patched compiler | 69,957 B | 182,912 B | 87,256 B |

The matched NuttX Rust language delta is **840 bytes of code + initialized
data, 24 bytes in the flash binary and 8 bytes of resident RAM** over C.
The compiler change adds only 16 code/data bytes to the original Rust image;
its binary size and resident RAM are unchanged. Binary packaging/alignment
can absorb a code increase. Debug sections are not part of these flash totals.

NuttX traffic heap peaks are 117,552 B for C and 118,424 B for Rust. Including
resident RAM gives 221,936 / 222,816 B. These are observed traffic reservations,
not a full-capacity bound; they do not supersede the
[full-queue RAM qualification](CONTROLS.md). No stack or queue capacity was
changed to improve the language comparison.

For Embassy, the same-host unpatched natural control is 69,785 B of code/data,
182,736 B of binary and 87,272 B of resident RAM. The private compiler build
therefore reduces those totals by 32 / 32 / 16 B. The earlier Mac-built image
was 69,461 / 182,496 / 87,264 B: the cross-host unpatched build itself differs,
so its whole delta must not be attributed to the LLVM patches. All platform
images retain their deliberately different OS/API/boot features.

## Provenance and qualification limits

Rust and LLVM revisions remain pinned. The report binds each Rust image to the
five patch hashes and before/after source ledger, compiler executable **and
driver library** hashes, Cargo and linker hashes, selected std/core source
inventory fingerprint and unchanged application source hashes. Rust built
from a source archive reports an unknown commit in `rustc -vV`; its source
revision is recorded separately rather than inventing compiler output.

The rebuilt compiler ran privately on Linux. NuttX's relocatable app was
finally linked with the original Mac kernel, C helpers and GNU tools. An
unpatched cross-host NuttX control reproduces the original image except for
the build timestamp and image checksum. Kernel archive/member checks permit
only the verified `lib_utsname.o` timestamp field to change. Embassy uses the
pinned bundled linker and the same image packager; its host/build variation
is accounted for separately above.

The [discarded assembly-control evidence](results/esp32s3-arithmetic-2026-10-05.json)
is retained as a diagnostic, not as compiler or current-app qualification.
Its five-instruction loop was different from the compiler's six-instruction
loop. Its near-C timings cannot be substituted for the measurements above.

The series remains **inactive and evaluation-only**. The tested workload and
98 backend tests do not certify all hardware loops, interrupt-heavy firmware,
drivers, boundary counts or future compiler versions. Current
[local build tools](README.md#reproducing-locally) use the installed unpatched
compiler unless a private rebuilt toolchain is explicitly selected. Preserve
source/toolchain proof and a full flash backup when repeating the protected
matrix; raw serial logs and backups remain private.
