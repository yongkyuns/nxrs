# ESP32-S3 arithmetic assessment — 2026-10-06–07

The tested arithmetic is correct within the suite's stated contracts. The
large 64-bit loop gap is closed: the sixteen-patch Rust recurrence takes **5.21 µs
versus C's 5.71 µs**, down from 12.80 µs. This is not universal speed parity:
some short checked, conversion and comparison kernels still favor C.

The [sixteen-patch numeric report](results/esp32s3-constant-hwloop-math-2026-10-06.json)
retains all cases and timing repeats, not just improvements. It binds the
actual native Rust compiler, standard-library inventory, public patches,
C compiler, kernel and flashed image. The [suite guide](README.md) explains
semantics and local reproduction. No installed SDK or normal build is changed;
the dependency changes remain inactive evaluation RFCs.
Later hardening and mixed-multiply follow-ups below keep this report frozen;
0018 removes two multiplies but does not materially improve S3 speed. The
separate 0019 runtime-width experiment helps narrow inputs but penalizes wide
ones, so it also remains inactive.
The independent 0020 zero-comparison follow-up below reduces code size and
passes the full corpus, but its timing benefit varies between the corpora.
The [0021 sign-bit follow-up](#signed-wide-multiply-with-leading-sign-bits)
halves the checked-multiply instruction count and improves both diagnostics,
without introducing the runtime-width branch rejected in 0019.
The [extension and conversion follow-ups](#native-extension-and-saturating-conversion-follow-ups)
also remove redundant sign-extension instructions and avoid conversion helpers
on values that will clamp. These are compiler decisions, not application
rewrites or std removal. Remaining differences are still reported below.
The [bit-branch follow-up](#direct-bit-test-branches) removes another
mask-and-branch sequence. Its full device check passes, with modest speed
and code-size improvements and several small regressions reported separately.

## What was validated

| Check | Sixteen-patch baseline evidence |
| --- | --- |
| Device corpus | 1,136 cases × 64 inputs; 942 matched C/Rust cases and 194 independently checked Rust-only 128-bit cases |
| Correctness | Three complete ESP32-S3 runs; 398,976 independent-oracle checks; zero C errors and zero Rust errors |
| Timing | Five masked and five normal repeats per case per run; all 34,080 paired records retained, including absent-C markers |
| Compiler regressions | All 111 Xtensa CodeGen/MC tests pass, plus 407 supported optimizer tests; 50 unsupported cases and three unchanged expected failures are recorded separately |
| Native object verification | The actual optimized full-corpus Rust IR also compiles through the final backend with `-verify-machineinstrs` |
| Host oracle/harness | 3,278,720 comparisons, including 3,145,728 exhaustive 8-bit comparisons; host execution is not Xtensa qualification |
| Device recovery | The full original 16 MiB firmware backup was restored and verified |

Correctness precedes timing. Repeating the same vectors for timing does not
create extra independent result checks. Exact contracts apply to integer
operations and integer-valued conversions. Floating operations use the explicit
NaN, signed-zero and accuracy contracts in the README.

The final source chain is reproduced from pinned preimages and checked against
the compiled affected files. Older saved compiler controls fail the new
regressions. Existing assertions and machine verification were not disabled.
The local compiler builds were incremental; this is not a claimed fresh CI
compiler build.

## Speed: before and after the sixteen-patch baseline

Times are interrupt-masked medians in µs per input, including input loads,
result stores, calls and batch overhead. Each recurrence performs **64 steps
per input**; the other rows do not. The earlier baseline already includes the
six base compiler patches and the alignment proposal, not an unoptimized SDK.

| Case | Earlier Rust | Final C | Final Rust | Final Rust/C |
| --- | ---: | ---: | ---: | ---: |
| `u32` rotate/multiply/add recurrence | 1.68470 | 1.67227 | 1.70319 | 1.02× |
| `u64` rotate/multiply/add recurrence | 12.80488 | 5.70573 | 5.20762 | 0.91× |
| `i32::signum` | 0.12891 | 0.05182 | 0.05964 | 1.15× |
| `u64` checked subtract | 0.24857 | 0.10247 | 0.13047 | 1.27× |
| `i64` checked multiply | 0.37415 | 0.17168 | 0.25742 | 1.50× |
| `i64` overflowing power | 0.88613 | 0.42324 | 0.64603 | 1.53× |
| `i64::max` | 0.15983 | 0.07096 | 0.17214 | 2.43× |
| `i8` overflowing divide | 0.19544 | 0.10065 | 0.20208 | 2.01× |
| `f32 as u64` | 0.29674 | 0.17689 | 0.26504 | 1.50× |
| `f64 as u64` | 0.63854 | 0.51530 | 0.59681 | 1.16× |
| `f32::acosh` | 2.21419 | 1.39440 | 1.42572 | 1.02× |
| `f64::asinh` | 20.65625 | 15.88646 | 15.89440 | 1.00× |

The wide recurrence is about **59% faster than earlier Rust** and about
**9% faster than C** in this placement. That does not make Rust generally
faster. The largest remaining positive Rust-minus-C median among the matched
cases is **0.223 µs/input**, in `i64` overflowing power. This bound describes
this corpus and image only; input distributions, loop lengths and application
composition can change it.

Not every case improves. Small add/multiply and some floating batches become
slower despite unchanged kernel instructions, and wide max remains slower.
C controls also shift between some images without C source/compiler changes.
Copy-only cases that alias the **same linked Rust function address** have
different timings for different input vectors. Thus arithmetic instructions
alone cannot explain every delta. Flash/data placement and benchmark overhead
need controlled isolation before assigning precise causes; cache effects are
plausible, not established individually. Subtracting copy timings is not an
exact instruction-latency measurement. Normal samples retain interrupt and
preemption delays and are not isolated instruction-speed results.

## What was fixed, and why

These are automatic compiler/library decisions, not application rewrites,
inline assembly, Cargo features or false target-layout declarations.

| Area | Confirmed cause | Traceable change |
| --- | --- | --- |
| Carry/borrow and Boolean values | Missing ESP32-S3 `SALT/SALTU` selection caused branch-based 0/1 results | [0008](../../platform/rust-llvm/proposals/0008-Xtensa-use-ESP32S3-set-less-than.patch); older profiles keep their fallback |
| Wide invariant products | IVUsers rejected an existing non-native-width counter before strength reduction | [0009](../../platform/rust-llvm/proposals/0009-IVUsers-consider-existing-non-native-induction-variables.patch); narrow-to-wide casts and wider-than-64 counters remain excluded |
| Wide loop exits | IndVarSimplify excluded that existing wide exit counter, leaving two counters after strength reduction | [0010](../../platform/rust-llvm/proposals/0010-IndVarSimplify-reuse-existing-wide-exit-counters.patch); existing trip-count and poison safeguards remain |
| Distinct branch predicates | Branch analysis treated matching opcodes as equivalent even with different operands | [0011](../../platform/rust-llvm/proposals/0011-Xtensa-reject-distinct-compound-branch-conditions.patch); this is a correctness fix, not a claimed speed gain |
| Call-frame verification | Custom CFG splitting lost active outgoing-call-frame metadata | [0012](../../platform/rust-llvm/proposals/0012-Xtensa-preserve-call-frame-state-in-custom-CFG.patch); the earlier `u128` verifier diagnostic is fixed, not waived |
| Integer selects | Branch diamonds were used despite native conditional moves; false inputs needed tied constraints | [0013](../../platform/rust-llvm/proposals/0013-Xtensa-select-integers-with-conditional-moves.patch) |
| Wide equality/inequality | XOR/OR Boolean materialization preceded the branch | [0014](../../platform/rust-llvm/proposals/0014-Xtensa-branch-on-paired-integer-inequality.patch); both DAG polarities and long-range relaxation are tested |
| Floating predicates selecting integers | Branch diamonds remained despite `MOVT/MOVF` | [0015](../../platform/rust-llvm/proposals/0015-Xtensa-use-Boolean-moves-for-FP-integer-selects.patch); predicate registers stay virtual until allocation |
| Constant hardware-loop counts | A wide count type was rejected even when the exact trip count fit the native counter | [0016](../../platform/rust-llvm/proposals/0016-HardwareLoops-admit-fitting-wide-constant-counts.patch); include the final iteration in the fit check; dynamic wide counts retain the guard |
| Inverse hyperbolic math | Rust std inline formulas differed from C's selected libm routines | [Separate std RFC](../../platform/rust-std/README.md); on NuttX, call the same six libm functions |

The final wide recurrence has a native hardware loop around the 64-step body,
rather than a multiword compare/branch every iteration. It retains the genuine
`n32` target layout. Its Rust wrapper shrinks from **199 to 133 B**, versus
C's 138 B. The [14-patch/math control](results/esp32s3-paired-branch-math-2026-10-06.json)
keeps std fixed while the final two compiler proposals reduce Rust's recurrence
from 6.51 to 5.21 µs/input.

The math RFC makes all six changed math cases bit-identical to C and within
1 ULP of the oracle in the tested corpus. Their wrappers become 49 B (`f32`)
and 51 B (`f64`), versus 99–184 B for the original formulas. Some original
formulas, notably `f32::atanh` and `f64::acosh`, were faster than this libm.
Consistent math implementation and smaller wrappers are not a blanket speed
improvement.

## Additional compiler regression coverage

The separate [0017 tests-only patch](../../platform/rust-llvm/proposals/0017-Tests-harden-predicates-and-wide-loop-boundaries.patch)
adds three groups, selected with `--proposal-set hardening`:

- Exact carry/borrow operands and their use in the high word, plus wide loop
  counters and scaled values that remain observable after the loop exits.
- Ordered floating not-equal predicate inversion, including its NaN polarity,
  while preserving the original false input for a later store.
- Direct 32-bit hardware-count boundaries: the largest fitting trip count,
  overflow from adding the final iteration, an already oversized exit count,
  and an unsigned exit count with bit 63 set.

The [qualification record](results/compiler-hardening-2026-10-06.json) contains
113 passing Xtensa tests and 409 passing optimizer tests, with the existing
50 unsupported and three expected failures unchanged. The checks reject all
27 deliberate wrong-output variants. Supplementary x86 execution checks
504 observable live-out results across original, IndVarSimplify and
strength-reduced IR, including wrapping input seeds, with no mismatch. These
host checks are not execution of Xtensa hardware loops or device FP/NaN cases.

The public seventeen-patch chain reconstructs all affected source files exactly.
0017 changes only four new test files; the native compiler hashes, patches
0001–0016 and all device reports remain unchanged. There is therefore no new
firmware speed, RAM or image-size claim.

## Mixed-width multiplication follow-up

The [eighteenth LLVM proposal](../../platform/rust-llvm/proposals/0018-SelectionDAG-expand-mixed-widening-multiply.patch)
addresses one avoidable part of checked signed multiplication. When LLVM
splits a wide signed product into smaller products, its cross terms multiply
a signed high word by an unsigned low word. The expander already recognized
two signed inputs and two unsigned inputs, but lacked this mixed case.

The missing path multiplied a sign mask by the other operand to repair the
high result word. A sign mask is either zero or all ones: instead, mask the
unsigned operand and subtract it from the unsigned high product. This is
an automatic compiler decision for either operand order, without application
branches, inline assembly or feature flags. C still uses the ordinary
`__builtin_mul_overflow`; its operand-width fast paths are compiler-generated.

In the actual checked `i64` Rust kernel, the static multiply count falls from
**ten to eight**. The earlier residual table incorrectly said nine; the count
here is from both final linked disassemblies. The wrapper grows from 164 to
166 B, and total `.flash.text` grows by 4 B. The diagnostic binary remains
5,706,920 B because this change fits within its existing layout padding.
RAM sections and the fixed 3,072-byte output buffers are unchanged.

The [compiler qualification record](results/compiler-mixed-mul-2026-10-06.json)
binds the exact public eighteen-patch chain to the rebuilt Rust driver and
unchanged std source. It records 114 Xtensa, 409 optimizer and five affected
x86 regression passes; the previous unsupported/expected failures remain.
The old compiler fails the new widening regression. Supplementary x86
execution checks 62,914 compiled calls across the candidate and control,
including both mixed operand orders and signed `i128` overflow, with no
mismatch. The Python arithmetic model is separate from native execution.

The [matched device report](results/esp32s3-mixed-mul-math-2026-10-06.json)
contains three complete runs: 398,976 oracle checks, no C or Rust errors, and
verified restoration. The six signed-zero differences remain allowed by the
existing contract. C source/compiler/function sizes, kernel archives/config,
vectors and the std snapshot/ledger all match the sixteen-patch control.

| Case | Control Rust (µs/input) | New Rust | Matched C |
| --- | ---: | ---: | ---: |
| `i64` checked multiply | 0.25742 | 0.25749 | 0.17168 |
| `i64` overflowing multiply | 0.24909 | 0.24909 | 0.16074 |
| `i64` overflowing power | 0.64603 | 0.64004 | 0.42324 |
| `u64` 64-step recurrence | 5.20762 | 5.20762 | 5.70573 |

There is **no meaningful checked-multiply speedup on this board**. Overflowing
power improves by under 1%; other multiply modes are effectively unchanged.
Instruction count is not instruction latency: two multiply instructions became
two AND instructions, with their high-word additions becoming subtractions.
Both linked functions contain 63 instructions. Independent arithmetic can
overlap a multiply's result latency. This explains why counting multiplies
alone did not predict a gain; it does not establish a cycle-by-cycle pipeline
attribution. The complete carry/overflow calculation and batch I/O remain.

Keep 0018 as an inactive, correctness-qualified code-generation RFC, **not an
ESP32-S3 speed fix**. It does not close the remaining GCC small-operand gap.
The largest positive Rust-minus-C median is still 0.217 µs/input in overflowing
power. Broader runtime fast paths need input-distribution and speed/size
evaluation before becoming compiler policy.

## Runtime operand-width experiment

The [nineteenth LLVM proposal](../../platform/rust-llvm/proposals/0019-Xtensa-bypass-wide-signed-overflow-multiply.patch)
tests the next hypothesis: check whether both runtime signed `i64` operands
fit signed `i32`, then use an exact native widening product. Any such product
fits signed `i64`, so no overflow calculation is needed on that path. Wider
operands retain the original intrinsic and its full-width expansion.
The pass is automatic, restricted to supported S3 speed-optimized functions,
and excludes `optsize`/`minsize`, already-narrow values and unused overflow flags.
Application code still uses ordinary `checked_mul`; there is no source fast path
or feature flag.

The [eight-group corpus](mul_widths.py) isolates operand distributions without
changing the original full catalog. It includes 64 runtime inputs per group,
edge values, overflowing and non-overflowing products, and seeded mixed widths.
It is stratified diagnostic coverage, not a claim about typical application
frequencies: several groups deliberately concentrate on small boundary sets.
Both the [eighteen-patch control](results/esp32s3-mul-width-control-2026-10-06.json)
and [nineteen-patch candidate](results/esp32s3-mul-width-candidate-2026-10-06.json)
pass three complete device runs, with **3,072 oracle checks per image**, no
errors or pair differences, and verified original-firmware restoration.
Kernel archives/config, C compiler/flags, target, vectors and std ledger/inventory
are identical. C timings are unchanged or differ by at most two cycles per
64-input batch.

Times below are interrupt-masked median µs/input, including the same input loads,
result stores and batch overhead. All normal and masked repeats remain in the
reports. A positive change means slower than the Rust control.

| Runtime operands | Control Rust | Candidate Rust | Matched C | Rust time change |
| --- | ---: | ---: | ---: | ---: |
| Both fit signed 32 bits | 0.25749 | 0.18099 | 0.14525 | −29.7% |
| Only first fits signed 32 bits | 0.25111 | 0.31842 | 0.23555 | +26.8% |
| Only second fits signed 32 bits | 0.25111 | 0.30807 | 0.26237 | +22.7% |
| Both wide, product fits signed 64 bits | 0.25111 | 0.30176 | 0.18607 | +20.2% |
| Both wide, product overflows signed 64 bits | 0.25749 | 0.30176 | 0.19512 | +17.2% |
| Near the signed 32-bit boundary | 0.25111 | 0.27839 | 0.19987 | +10.9% |
| Near the signed 64-bit boundary | 0.25742 | 0.30820 | 0.27077 | +19.7% |
| Seeded mixed widths | 0.25111 | 0.26549 | 0.20111 | +5.7% |

The narrow path drops from eight to **two executed multiplies**, improving that
group by about 30%, but it is still about 25% slower than C. The fallback still
executes eight multiplies and the full high-word carry/sign/overflow calculation.
Final linked code also adds sign-width tests and branches. Its fast and fallback
blocks sit outside the physical hardware-loop span and jump back to the loop
end; the control's straight-line body needs no such jumps. The merged overflow
flag is normalized before result selection, and result stores are rescheduled.
These are confirmed extra work/layout changes, not a precise cycle-by-cycle
attribution. The same Rust function is shared by all eight groups, so different
input data and taken paths matter; this is not eight independently tuned kernels.

GCC does more than the both-narrow shortcut. Its linked code has a one-narrow
path and high-word/range tests for the both-wide case, allowing it to decide
overflow without always constructing the full signed 128-bit product.
Appending one narrow shortcut to Rust's unchanged fallback does not reproduce
that strategy. This explains why the isolated narrow improvement is not
general C/Rust parity.

The shared Rust function grows **166 → 211 B**; it must not be counted eight
times. Total `.flash.text` grows **44 B** after alignment. `.flash.rodata`,
IRAM and RAM sections are unchanged; both diagnostic binaries are **250,908 B**
because the change fits existing image padding. Both functions retain a 32-byte
entry frame. This is a linked-section/frame check, not a new runtime stack
high-water measurement or a C-only versus Rust-only firmware comparison.
The guard cost belongs to each distinct emitted operation, not a one-time
std-library charge; identical kernels happen to share one function here.

The [compiler qualification](results/compiler-mul-width-2026-10-06.json)
binds all 19 public patches and 59 reconstructed source files to the actual
rebuilt Rust driver: 115 Xtensa, 409 optimizer and five affected x86 tests pass.
The previous 50 unsupported and three expected optimizer failures are unchanged.
The old compiler fails the new assembly check. Supplementary host execution of
the transformed IR checks **80,882 calls**, including boundaries, random widths
and sequential checked products, with no mismatch. The full 1,136-case Rust IR
passes the native backend machine verifier; **that is not a new full-corpus
device execution with 0019**.

**Do not activate 0019 as a default speed optimization.** It passes the stated
correctness checks but regresses every other measured operand group. Keep the
patch and paired evidence as an inactive profitability RFC. The next useful
compiler work is the wide overflow strategy itself, including one-narrow and
both-wide cases, followed by the same stratified control and full-corpus device
qualification. Application branches or optimizing only the narrow benchmark
would not address the general problem.

## Zero-comparison selection follow-up

Proposal [0020](../../platform/rust-llvm/proposals/0020-Xtensa-select-zero-comparisons-directly.patch)
addresses a smaller, general instruction-selection issue. The existing
register equality/inequality patterns construct an XOR during instruction
selection, after DAG simplification. For comparison against zero, that XOR
does nothing. Match the zero case directly with the existing `SALTU`:
unsigned `x < 1` implements `x == 0`, and `0 < x` implements `x != 0`.
This applies to any matching integer comparison, not benchmark symbols or
special input distributions. No application code, feature flag or IR pass
is added; other constants and unsupported CPUs keep their previous selection.
The `zero-compare` chain is 0018 plus 0020; it excludes the regressing 0019.

The [compiler qualification](results/compiler-zero-compare-2026-10-06.json)
reconstructs 57 affected files exactly and passes 115 Xtensa, 409 optimizer
and five x86 overflow tests. The previous unsupported cases and expected
failures remain separate. The old compiler fails the new zero-comparison
regression. The wide overflow legalizer is unchanged from its pinned source.
The ordinary Rust driver was rebuilt; this is not an assembly substitution.

The [operand-width device report](results/esp32s3-zero-compare-widths-2026-10-06.json)
passes three runs, with zero C/Rust errors or pair differences and verified
restoration. The shared multiply wrapper shrinks **166 → 163 B**, retaining
its 32-byte entry frame and eight multiply instructions. Total `.flash.text`
shrinks **20 B**; other loadable and RAM sections are unchanged. The diagnostic
binary stays **250,908 B** because of image padding. The function must still
be counted once, not once per input group.

The Rust masked median is **0.25931 µs/input** in all eight groups, compared
with **0.25111–0.25749** in the control: 0.7–3.3% slower in this placement.
Some unchanged C controls also shift. A shorter instruction sequence is not
a measured latency improvement; these data do not isolate an exact reason
for the timing shifts. Keep 0020 as an inactive code-size/selection RFC,
not a claimed checked-multiply speed fix or full C/Rust parity.

The [full-corpus device report](results/esp32s3-zero-compare-math-2026-10-06.json)
also passes three complete runs: **398,976 oracle checks**, with zero C/Rust
errors and verified firmware restoration. The six raw pair differences remain
the permitted signed-zero choices in `f32::min/max`; they are not new failures.
The kernel archives, C compiler/flags, application source, inputs and std
snapshot are unchanged from the eighteen-patch control.

In this larger image, checked multiply improves about **9%**, although it is
still **1.37× C's time**. Times below are masked medians in µs/input, including
the same loads, stores and batch overhead as earlier reports.

| Full-corpus case | Control Rust | New Rust | Matched C |
| --- | ---: | ---: | ---: |
| `i64` checked multiply | 0.25749 | 0.23470 | 0.17168 |
| `i64` overflowing power | 0.64004 | 0.63236 | 0.42324 |

Total `.flash.text` shrinks **1,188 B** across this broad diagnostic; other
loadable and RAM sections are unchanged. Its binary remains **5,706,920 B**
because the reduction fits existing padding before the large vector segment.
This is useful code-size evidence, not a C-only/Rust-only firmware delta.
The differing timing direction between the full and stratified images rules
out a blanket speed claim. Exact attribution would require controlled code
and data placement, beyond the instruction-selection regression. The wide
overflow strategy still computes eight multiplies and remains an open
optimization; 0020 does not make 0019 suitable for default activation.

## Signed wide multiply with leading sign bits

Proposal [0021](../../platform/rust-llvm/proposals/0021-SelectionDAG-check-signed-multiply-with-leading-sign-bits.patch)
addresses the wide overflow strategy rather than adding a narrow-input fast
path. The previous inline fallback calculates the full double-width product
to check whether its upper half is the sign extension of the lower half.
The alternative calculates only the wrapped product, then uses leading sign
bits, the expected sign and the zero-product boundary to determine overflow.
It preserves the wrapped result even when overflow occurs; it does not
negate the signed minimum or assume the inputs fit a smaller integer type.

This is a generic SelectionDAG change, restricted to scalar equal-half
expansions with a legal native leading-zero instruction. Existing libcalls,
unsigned multiplication and unsupported targets retain their old paths.
There is no application workaround, new feature flag or runtime width branch.
The `signed-overflow` selection extends 0020 and still excludes 0019.

The [compiler evidence](results/compiler-signbits-2026-10-06.json) records
116 Xtensa, 409 optimizer and six x86 test passes, with unsupported and expected
failures counted separately. All 60 affected source files reconstruct exactly
from the public patch chain, and the Rust driver was rebuilt normally. The
new compiler fixtures fail with the parent compiler. Additional correctness
checks cover **1,242,336 mathematical-model pairs** and **60,841 native signed
128-bit pairs per compiler**, testing both wrapped product and overflow against
an independent oracle. Model checks are not compiler execution; native x86
checks are not Xtensa timing or device qualification.

Both device corpora pass three runs, with verified original-firmware
restoration. The [width-stratified report](results/esp32s3-signbits-widths-2026-10-06.json)
has **3,072 oracle checks**, with no errors or pair differences. Its Rust masked
median drops **0.25931 → 0.23444 µs/input**, about **9.6%**, in all eight groups.
All eight C control medians are unchanged. This avoids 0019's measured
wide-input penalties, but is not full parity: Rust matches or beats C in three
groups, while C is faster in five. When both operands fit signed 32 bits,
Rust still takes **1.85× C's time**. GCC's two-multiply narrow path remains
cheaper than Rust's general path.

The [full-corpus report](results/esp32s3-signbits-math-2026-10-06.json) has
**398,976 oracle checks**, zero C/Rust errors and the same six permitted
`f32::min/max` signed-zero differences. These are masked medians in µs/input;
both C columns are shown because unchanged C code can move when Rust shrinks.

| Full-corpus case | Parent C | Parent Rust | New C | New Rust |
| --- | ---: | ---: | ---: | ---: |
| `i64` checked multiply | 0.17168 | 0.23470 | 0.19030 | 0.20983 |
| `i64` saturating multiply | 0.19479 | 0.27025 | 0.19479 | 0.23040 |
| `i64` overflowing multiply | 0.16074 | 0.22637 | 0.17923 | 0.20579 |
| `i64` checked power | 0.43711 | 0.42969 | 0.43711 | 0.38652 |
| `i64` saturating power | 0.46361 | 0.46335 | 0.48242 | 0.41556 |
| `i64` overflowing power | 0.42324 | 0.63236 | 0.42324 | 0.54440 |

Checked multiply improves **10.6%** and overflowing power **13.9%** relative
to parent Rust in this image. Checked multiply remains **1.10× the current
C time**; overflowing power remains **1.29×**. Some unchanged kernels also
shift, including Rust checked `i32` Euclidean remainder and C `i32` equality.
Thus this is not an assertion that every case gets faster or that each
full-image timing change is caused by the new arithmetic instructions.
The width diagnostic's unchanged C controls provide cleaner supporting
evidence; broader profitability and controlled placement remain open.

In the linked checked-multiply kernel, multiply instructions drop **8 → 4**,
function bytes **163 → 156**, and the entry frame stays **32 B**. The eight
width groups alias that one Rust function: count its size once. Across the
full diagnostic, the six affected multiply/power wrappers shrink; all C
symbol sizes are unchanged. Flash code (`.flash.text`) shrinks **8 B** in the
width diagnostic and **84 B** in the full diagnostic. Other loadable and RAM
sections are unchanged, as are the padded binaries (**250,908 B** and
**5,706,920 B** respectively). No allocator or thread-stack change is involved.
These are combined diagnostic images, not C-only/Rust-only firmware deltas.

Keep 0021 as an inactive, reproducible compiler RFC. The improvement is useful,
but native leading-zero legality alone does not establish profitability on
every target, and the remaining narrow-input gap is real. The installed SDK,
default six-patch chain and CI activation remain unchanged.

## Native extension and saturating conversion follow-ups

The same Rust expressions exposed two further missed lowering opportunities.
The dependency changes remain individually recorded, inactive RFCs:

| Change | Root cause | Measured result and limits |
| --- | --- | --- |
| [0022 native sign extension](../../platform/rust-llvm/proposals/0022-Xtensa-select-native-sign-extension.patch) | Byte/halfword sign extension expanded to two shifts even though this core has `SEXT`. | Many signed wrappers shrink by 3–21 B. `i16` saturating add takes 1,241 rather than 1,369 cycles per 64 inputs (9.4% faster); C remains 1,195. Some wide sign-mask construction initially duplicates work; 0026 addresses that separately. |
| [0023 guarded conversion](../../platform/rust-llvm/proposals/0023-CodeGenPrepare-guard-expanded-saturating-FP-conversion.patch) | The saturation expander calls a wide conversion helper before deciding the input will clamp. | The 16-group diagnostic separates finite values from low/high/NaN clamps. Finite casts improve 6–21%; clamps improve 37–71%. All matched C medians stay unchanged in this diagnostic. Native/custom conversions and size-optimized functions remain excluded. |
| [0024 NaN classification](../../platform/rust-llvm/proposals/0024-CodeGenPrepare-classify-NaN-in-guarded-casts.patch) | The signed low-result block requests a second software `f64` comparison to recognize NaN. | Existing `is_fpclass` lowering avoids that helper: `f64`→`i64` low and NaN groups improve 24.8% and 31.1%, with unchanged C medians. Finite/high groups are nearly unchanged. The `i64` wrapper shrinks 4 B, but the `i128` wrapper grows 7 B; total flash code grows 4 B. |
| [0025 soft-source conversion](../../platform/rust-llvm/proposals/0025-CodeGenPrepare-guard-soft-float-saturating-conversions.patch) | Software `f64` needs a helper even when the integer result is legal or promoted; the first guard excluded those casts. | All six `f64`→signed/unsigned 8/16/32 casts improve 19–38% in the full corpus, with unchanged matched C medians. Wrappers shrink 8–22 B. Native `f32` conversion instructions are unchanged. Other targets' custom hooks and wider-only helper inventories need portability/profitability qualification. |
| [0026 sign-mask reuse](../../platform/rust-llvm/proposals/0026-Xtensa-reuse-native-extension-for-sign-mask.patch) | Widening an 8/16-bit signed value to 128 bits builds two equivalent sign masks after native extension is enabled. | Reuse the extension already present in the DAG: all four affected wrappers shrink 62 → 56 B and take 980–981 rather than 1,108–1,109 cycles per 64 inputs (11.6% faster). They alias two emitted functions; do not count their bytes four times. No C 128-bit counterpart is available. |

The [guarded compiler evidence](results/compiler-guarded-casts-2026-10-06.json)
binds 0022–0023 to the actual Rust driver. Separate
[NaN-classification](results/compiler-fpclass-casts-2026-10-06.json) and
[soft-source](results/compiler-soft-casts-2026-10-06.json) records preserve
their parents unchanged. The final [sign-mask evidence](results/compiler-sign-mask-2026-10-06.json)
binds the full 25-patch selection and 67 reconstructed source files to the
rebuilt Rust driver. It passes 594 supported compiler tests:
120 Xtensa, 467 optimizer and seven x86 regressions; 104 unsupported tests
and three expected failures are reported separately. Public patches reconstruct
all affected files exactly. Old compiler controls fail the new regressions.
The full optimized Rust IR also passes backend machine verification.

Each measured selection (`guarded-casts`, `fpclass-casts`, `soft-casts` and
`sign-mask`) passes three full device runs, with **398,976 oracle checks per
selection**, zero C/Rust errors and the same six permitted signed-zero
differences. The [wide conversion control](results/esp32s3-cast-ranges-control-2026-10-06.json),
[guarded candidate](results/esp32s3-cast-ranges-candidate-2026-10-06.json) and
[NaN follow-up](results/esp32s3-fpclass-cast-ranges-2026-10-06.json) each add
6,144 checks. The [small-width control](results/esp32s3-small-cast-ranges-control-2026-10-06.json)
and [candidate](results/esp32s3-small-cast-ranges-candidate-2026-10-06.json)
each add 18,432 checks over 48 groups, including exact small-width maxima and
fractions immediately above them. All supplements have zero errors and pair
differences; every completed cohort restores and verifies the original firmware.
Supplementary x86 execution checks 270,592 inputs per compiler. These host
checks do not exercise ESP32-S3's software-floating path or sign-mask combine.
The final [25-patch device report](results/esp32s3-sign-mask-math-2026-10-06.json)
adds three complete runs with the same zero-error result. Only the four
sign-mask wrappers change size from the 24-patch parent; other instructions
and broader portability/profitability still need their own qualification.

Branch thresholds round in the correct direction and the source is frozen
before branching, preserving LLVM's undefined/poison-input rules. NaN still
returns zero. Applications keep ordinary `as` casts; no source range test,
inline assembly, Cargo feature or runtime type-width policy is added.
An integer-only replacement for conversion helpers was also evaluated but
not retained: it increased code substantially. This is a measured selection,
not a claim that fewer helper calls always mean a better implementation.

Timing depends on both executed paths and placement. In the small-width
diagnostic, some unchanged `f32` functions and C controls move by hundreds of
cycles after the image changes. Do not attribute those shifts to a changed
arithmetic routine. The full-corpus small `f64` comparisons have stable C
controls and changed Rust instructions, providing stronger supporting evidence.
Finite, low, high and NaN groups are diagnostic distributions, not predictions
of how frequently a real application encounters each input.

Across 0022–0026, full-diagnostic flash code falls **311,202 → 310,274 B**
(−928 B); other loadable and resident RAM sections are unchanged. The padded
binary is still **5,706,920 B**. These instruction savings occur at emitted
call sites/wrappers, not as a fixed std charge. Shared helpers are counted once
and can disappear only when their last user does. This is not a fresh runtime
stack high-water measurement or a C-only/Rust-only firmware-size comparison.

## Direct bit-test branches

The [27-patch follow-up](results/esp32s3-bit-branch-math-2026-10-07.json)
keeps the 25-patch `sign-mask` control frozen. C source/compiler/flags,
NuttX configuration and kernel archives, std source/features, target and
all generated input/expected vectors are identical. Only the Rust compiler
package changes; 0019 is still excluded.

Two separately packaged compiler fixes are needed:

- [0027](../../platform/rust-llvm/proposals/0027-Xtensa-branch-on-single-bit-tests.patch)
  selects Xtensa's direct bit-test branches instead of masking into a
  temporary register and then branching. It also completes branch analysis,
  inversion, insertion and far-target relaxation. Multi-bit masks, live
  shared masked values and materialized Boolean results retain their lowering.
- [0028](../../platform/rust-llvm/proposals/0028-SelectionDAG-discard-unused-BR_CC-simplifications.patch)
  fixes a generic combiner interaction: a speculative shift expression for
  a nonzero bit test has no users, but temporarily gives its masked operand
  a second use. That hides the target's single-use opportunity. Existing
  worklist-aware cleanup removes only unused expressions before the target
  check; real shared expressions and successful comparison folds remain intact.

Ordinary Rust `wrapping_pow`, checked power and other operations benefit
automatically. No application range check, inline assembly, feature flag
or std source rewrite is added.

The following masked medians are cycles per 64 inputs, not per operation.
Matched C medians stay unchanged for these C-supported rows.

| Operation | Rust cycles before → after | C cycles | Rust wrapper bytes before → after |
| --- | ---: | ---: | ---: |
| `u8::wrapping_pow` | 2,770 → 2,624 (−5.3%) | 2,500 | 100 → 97 |
| `u16::wrapping_pow` | 2,771 → 2,625 (−5.3%) | 2,500 | 100 → 97 |
| `u32::wrapping_pow` | 2,703 → 2,562 (−5.2%) | 2,296 | 92 → 89 |
| `u64::wrapping_pow` | 3,726 → 3,642 (−2.3%) | 3,590 | 121 → 118 |
| `u64::checked_pow` | 4,153 → 4,085 (−1.6%) | 5,321 | 225 → 215 |
| `i64::checked_pow` | 5,937 → 5,847 (−1.5%) | 6,714 | 277 → 272 |
| `u128::overflowing_pow` | 19,680 → 19,988 (+1.6%) | Not available | 592 → 598 |

This is not a uniform improvement. `u16` and `u64` saturating power slow by
2.4% and 2.0%, and `i128` checked power slows by 1.1%. A direct bit branch
has shorter reach than a zero-test branch; `u128` overflowing power needs
an inverted local bit branch plus a jump. Register allocation and placement
also change. Smaller functions do not automatically run faster. The large
21.7% improvement in the `isize` wrapping-power row should not be presented
as a distinct signed-power fix: its function aliases the `u32`/`usize`
implementation, which shows a 5.2% improvement with different input placement.

47 wrapper sizes change, representing 34 emitted functions; aliases must
not be summed repeatedly. Flash code falls **310,274 → 310,102 B** (−172 B).
Other section sizes are unchanged, including loadable data and resident RAM.
The binary remains **5,706,920 B** because the code savings do not move the
following flash-aligned segment. The inspected `i128` checked-power entry
frame shrinks 160 → 144 B; this is not a runtime stack high-water measurement.
These are compiler-generated call-site savings, not a fixed std charge or
a fresh C-only/Rust-only firmware-size comparison.

The [compiler evidence](results/compiler-bit-branch-2026-10-07.json) binds
all 27 patches and 72 reconstructed source files to the rebuilt Rust driver.
All **601 supported tests** pass: 122 Xtensa, 467 optimizer and 12 x86 tests;
104 unsupported tests and three expected failures remain separate. Both
the frozen parent and the selector without 0028 fail the new regression.
Native cast execution adds 270,592 zero-error checks per compiler as a
generic regression control, not execution of Xtensa bit branches.
The actual full Rust IR passes machine verification. Three device runs add
398,976 oracle checks with zero C/Rust errors and the same six permitted
signed-zero differences. The full original firmware was restored and verified.
The changes remain inactive RFCs; broader target profitability and upstream
acceptance are not established by this finite corpus.

## Checked multiply: value-only results versus an observed overflow flag

The 27-patch full-corpus kernel still takes **3,223 Rust versus 2,637 C
cycles per 64 inputs**, about **22% longer**. Both write the same result and
validity information. Rust computes a four-multiply wrapped product and
branchless operand-width/sign checks. GCC can use two multiplies when both
inputs fit signed 32 bits. Removing overflow reporting from only the Rust
kernel would change the contract, not establish parity.

The inactive 0029 compiler RFC examines
`a.checked_mul(b).unwrap_or(0)`. If the product is already zero, selecting
between zero and that product cannot change the returned value. Its generic
nested-select simplification freezes the newly unconditional predicate,
which may have been unselected and poison in that zero case. Either frozen
choice still returns zero; defined nonzero cases are unchanged.

The [standalone Rust reproducer](compiler/zero-select.rs), compiled through
both actual Rust drivers against the same core archive, gives:

| Returned information | Parent → candidate symbol bytes | Parent → candidate instructions |
| --- | ---: | ---: |
| Checked product, zero on overflow | **103 → 92** | **37 → 33** |
| Same result, plus a separately stored overflow flag | 106 → 106 | 38 → 38 |
| Checked product, one on overflow | 105 → 105 | 38 → 38 |

These are standalone function bytes, not firmware-size or latency results.
The optimization does not weaken an independently observed overflow flag
or discard the wrapped output of `overflowing_mul`.

The initial shared-predicate rewrite increased the actual benchmark symbol
from 156 to 158 B. A profitability guard preserves independently observed
predicates. With the guard and freeze, the rebuilt driver's full 1,136-case
IR, backend object and complete Rust linker input are all byte-for-byte
identical to the 27-patch parent. This does **not** close the measured
checked-multiply gap or add a new image-size/RAM result. No final link or
device run was repeated for an unchanged Rust input, and the existing
27-patch device report remains separately attributed.

The [compiler evidence](results/compiler-zero-select-2026-10-07.json) binds
all 28 patches and 74 reconstructed files to the actual rebuilt Rust driver.
All **608 supported compiler tests** pass: 123 Xtensa, 467 optimizer and
18 x86; 104 unsupported tests and three expected failures remain separate.
The frozen parent fails both new code-generation fixtures.
The [host execution check](compiler/check_zero_select.py) adds **4,387,358
zero-error checks per compiler**, including defined zero-result inputs that
mask overflowing-add or out-of-range-shift predicates. Another 270,592 cast
checks per compiler remain generic regression controls. Assembly checks
exercise the fold; the applicator test protects its use of LLVM's standard
[`freeze` mechanism](https://llvm.org/docs/UndefinedBehavior.html#the-freeze-instruction).
These finite checks and the zero-case reasoning are not a formal proof of
LLVM semantics or universal target profitability. The patch remains an
opt-in RFC; the installed SDK, default patch series and device firmware
are unchanged.

## Checked multiply when input ranges are known

The inactive 0030 compiler RFC fixes a separate missed optimization. Two
unsigned 32-bit values always multiply into an unsigned 64-bit value. That
product fits signed 64 bits exactly when its top bit is clear. The previous
lowering still counted leading bits and checked a special zero case, even
though the compiler knew both input ranges.

The new combine uses existing range analysis: both operands must be
nonnegative and their product must fit the unsigned width. It preserves the
wrapped product and overflow flag. It also works for unequal masked ranges,
such as 24 × 40 bits; a 33 × 32-bit bound is correctly rejected. Legal-width
native overflow instructions are left alone. No application fast path,
assembly or feature flag is added.

The [ordinary Rust](compiler/mul-range.rs) and [matched C](compiler/mul-range.c)
probes return zero on overflow and separately store the flag. Both use O2;
the two actual Rust drivers use identical core archives and frontend IR.

| Input contract | Rust parent → candidate bytes | C/GCC bytes | Rust parent → candidate instructions |
| --- | ---: | ---: | ---: |
| Two unsigned 32-bit values widened to signed 64 | **59 → 27** | 75 | **22 → 10** |
| Two 64-bit values masked to unsigned 32 bits | **57 → 25** | 63 | **21 → 9** |
| Unrestricted signed 64-bit values | 106 → 106 | 179 | 38 → 38 |

These are standalone function symbols, excluding padding, literal pools and
shared helpers—not firmware size or measured latency. Fewer instructions do
not alone establish faster execution than C's conditional paths.

The [compiler evidence](results/compiler-mul-range-2026-10-07.json) binds all
29 patches and 76 reconstructed source files to the rebuilt Rust driver.
All **610 supported compiler tests** pass: 124 Xtensa, 467 optimizer and
19 x86. Unsupported/expected-failure counts remain separate. The frozen
parent fails both new assembly fixtures. Independent native execution checks
**172,522 products per compiler**, including signed-boundary overflow,
wrapped products and rejected bounds, with zero mismatches. Another 270,592
cast checks per compiler remain unchanged regression controls. An independent
Luna review found no correctness issue; broader profitability and upstream
acceptance remain unproven.

The rebuilt driver's full 1,136-case IR, backend object and complete Rust
linker input are byte-identical to the 28-patch parent. Therefore no final
link or flash was repeated, and no new RAM/image-size/device-speed result is
claimed. The roughly 22% full-width checked-multiply timing gap above remains
open. This improvement applies when normal types or masks prove the input
ranges; it must not be represented as a runtime shortcut for arbitrary i64s.
The installed SDK, default patch series and CI remain unchanged.

## Remaining differences: accounted for, not declared fixed

There is no remaining observed arithmetic correctness failure in the corpus.
The following are still optimization or qualification limits.

| Residual | What the final code/source shows | Implication |
| --- | --- | --- |
| Checked/overflowing 64-bit multiply and power | The sixteen-patch checked Rust kernel uses ten multiply instructions; 0018 reduces this to eight and 0021 to four, retaining a 32 B entry frame. GCC uses two when both operands fit signed 32 bits and has additional wide-input paths. Power inherits checked-multiply cost. | 0021 improves all eight measured width groups, but the both-narrow group still takes 1.85× C's time. The rejected 0019 shortcut penalizes the other groups. Further profitability/target qualification and the remaining narrow-input gap are open, not observed correctness failures. |
| Saturating float→integer casts | 0023–0025 now guard wide/software conversions and avoid the extra signed NaN comparison. Native floating-source casts, custom saturation, vectors and size-optimized functions keep their previous policy. In-range software `f64` still needs comparison and conversion helpers. | The tested finite/clamping paths improve without removing std, but input distributions and other targets' custom hooks/helper inventories still require qualification. Branching is a profitability policy, not universal speed parity. |
| Wide max and short divide/remainder predicates | Rust's wide max materializes the full comparison and uses conditional moves; C can branch past part of it. Narrow division uses extra sign-extension/predicate instructions and branch paths outside the physical hardware-loop span. | A smaller or branchless kernel is not automatically faster. These are generated-code choices, not allocations or a generic division libcall; the S3 uses native `QUOS/REMU` in the inspected narrow kernels. |
| Tiny batch timing | Different inputs can time differently even when their Rust kernel aliases the same address. Both implementations write a 24-byte result per input. | Do not label the complete tiny-batch difference an arithmetic instruction cost or a recurring std tax. RAM-input/layout/order controls would be needed for a finer attribution. |
| Floating raw bits | Six differences across three runs, all signed-zero choices in `f32::min/max` | Both pass the specified contract. Forcing identical zero choices would impose a stronger contract, not repair a failure. |

None of these residuals should be hidden by a grand mean or described as
unavoidable. Conversely, adding GCC-like branches solely to win this finite
input corpus would not establish a better general compiler policy. Retain
both normal and masked samples, profile the actual application distribution,
and qualify any further dependency proposal separately.

## Image size and RAM

These are **per-function symbol sizes**, not full firmware deltas. They omit
shared helpers and literal pools, and some symbols alias. Both languages run
inside one image; adding all symbol sizes double-counts shared code.

The final 5,706,920-byte diagnostic binary includes both implementations and
roughly 5 MiB of input/expected vectors. It is not production firmware bloat,
and it does not fit unchanged on a 2 MiB device. Use filtered shards there.
Vectors stay in flash; the two output buffers occupy a fixed **3,072 B**.
This suite does not measure queue storage, thread stacks, std thread metadata
or whole-application RAM. Those remain the scope of the matched service tests.

No formatter, allocator, queue or worker thread runs inside these timed
kernels. The compiler changes do not add a fixed per-service std allocation.

## Reproduction and activation boundary

Use `--proposal-set constant-hwloop` and the optional, separately prepared std
math ledger with the [same local/CI builder](../../platform/rust-llvm/BUILDING.md).
For the wide-multiply evaluation, select `--proposal-set signed-overflow`.
The extension/conversion follow-ups extend it through `native-sext`,
`guarded-casts`, `fpclass-casts`, `soft-casts` and finally `sign-mask`, using
the same std snapshot; the earlier reports remain frozen controls. None
includes the rejected 0019.
Select `bit-branch` to append 0027–0028 and reproduce the 27-patch follow-up
against that frozen `sign-mask` control.
The normal six-patch helper remains unchanged. Applicator and builder tests
reject wrong pins, altered source/ledgers, reapplication, unsafe archives and
incorrect qualification counts. Compiler and std patch identities remain
separate; the measurement exporter independently reparses every raw run and
checks image/source provenance.

Before production activation, separate qualification is still needed for
floating-register preservation across preempting threads, broader hardware-loop
interrupt/nesting behavior, cold-cache/layout sensitivity, and workloads beyond
the bounded corpus. Here `CONFIG_ARCH_HAVE_FPU=y` but `CONFIG_ARCH_FPU` is
unset: this single-thread test is not evidence of safe FP context switching.
No self-hosted CI workload was dispatched and no SDK was replaced.

The README also lists untested APIs and contracts: panic/unsafe paths,
SIMD/DSP, atomics, alternative rounding modes and exhaustive transcendental
domains among others. Passing these samples cannot certify every arithmetic
program, universal speed parity or future compiler releases.

## Compiler follow-up: separating two 64-bit costs

The immutable [seven-patch baseline](results/esp32s3-2026-10-06.json) and
[eight-patch carry/comparison report](results/esp32s3-setlt-2026-10-06.json)
separate carry instruction selection from loop optimization. The eighth patch
reduces the wide recurrence from 12.80 to 8.14 µs/input, but does not remove
the repeated affine counter product.

## Compiler follow-up: existing wide induction variables

The [nine-patch report](results/esp32s3-wide-iv-2026-10-06.json) records a
**9.8% regression** when strength reduction alone removes the product but
retains a separate wide exit counter. Removing an instruction is not enough;
the generated loop as a whole matters.

## Compiler follow-up: reusing the wide exit counter

The [ten-patch report](results/esp32s3-wide-exit-2026-10-06.json) records the
combined counter reuse improvement to 7.02 µs/input. Later
[integer moves](results/esp32s3-conditional-move-2026-10-06.json),
[paired branches](results/esp32s3-paired-branch-2026-10-06.json), and the final
fitting hardware-count proposal close the larger loop gap. Earlier reports
remain unchanged, including their then-unresolved verifier limitation and
the initial ANSI-prompt recovery disclosure.
