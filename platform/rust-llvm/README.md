# Xtensa LLVM compiler patch candidates

Status: **inactive, evaluation-only**. The pinned revisions, installed SDK
and application source/flags are unchanged. The ordered six-patch series
passes all 99 Xtensa CodeGen/MC tests. The new
[isolated device control](../../tests/event-services-comparison/COMPILER_PROBE.md)
confirms six-cycle arithmetic parity between its GNU-assembled C/GCC, C/LLVM
and Rust/LLVM controls. GNU `as` also repairs loop alignment. The
[native-object follow-up](../../tests/event-services-comparison/COMPILER_PROBE.md#native-object-alignment-follow-up)
finds that LLVM's final loop alignment is still affected by branch expansion;
that separate fix is under qualification. The six-patch series does not
establish native-object arithmetic parity, full service deadline parity or
qualify every loop.

The sixth-patch service check also completes 80 invocations and 312,000
loss-free deliveries. The long NuttX Rust handler improves to 13.759 ms,
versus 11.133 ms for C; some long deadlines still fail. NuttX image totals and
resident RAM are unchanged by the sixth patch. The
[follow-up report](../../tests/event-services-comparison/COMPILER_PROBE.md#full-service-check-with-the-sixth-patch)
keeps that end-to-end limit separate from isolated arithmetic parity.

The historical five-patch [service matrix](../../tests/event-services-comparison/ARITHMETIC.md)
has 80 invocations and 312,000 accepted/received messages with no rejections
or protocol errors, but retains speed/deadline differences. Its 98-test
compiler build and patch hashes remain separate from the sixth-patch evidence.
Use the [shared local/CI setup](BUILDING.md) to build into a fresh directory;
normal application builds do not silently activate this compiler.

The fix belongs here, not in an application helper or Cargo feature. Once
qualified, a private compiler build should apply this series automatically;
applications should continue to use ordinary `rotate_left` and Rust loops.

## Pinned inputs and changes

[upstream.json](upstream.json) records the unchanged Espressif Rust and LLVM
pins and SHA-256 hashes of the pinned affected source. The six-patch ordered
series is registered in the existing `tools/apply-nuttx-patches.py`
applicator (its historical name is retained for build compatibility).

| Patch | Compiler change |
| --- | --- |
| [0001](patches/0001-Xtensa-custom-lower-i32-rotates.patch) | Lower 32-bit rotate nodes through the existing Xtensa funnel-shift/SRC path instead of expanding them into shifts and OR. |
| [0002](patches/0002-Xtensa-default-eligible-hardware-loops.patch) | Enable eligible hardware loops by default; retain the target-feature, text-literal and machine-pass legality checks. |
| [0003](patches/0003-Xtensa-model-loop-end-fallthrough.patch) | Correct loop pseudo control-flow metadata: `LOOPINIT` and `LOOPDEC` calculate values, not terminators; `LOOPBR` and `LOOPEND` are conditional, not barriers. |
| [0004](patches/0004-Xtensa-use-immediate-SAR-for-constant-funnels.patch) | For constant SRC amounts, use SSAI with explicit `Defs=[SAR]`; preserve left-zero's `SAR=32` behavior for distinct funnel inputs. |
| [0005](patches/0005-Xtensa-refresh-affected-codegen-checks.patch) | Update generated expectations in existing CodeGen tests; no IR or `RUN` line changes. |
| [0006](patches/0006-Xtensa-model-ESP32S3-multiply-result-latency.patch) | Model ESP32-S3 multiply-result latency, select model-aware post-allocation scheduling, preserve the hardware-loop start boundary, and refresh affected instruction-order checks. |

The [draft final-alignment proposal](proposals/0007-Xtensa-align-hardware-loops-after-layout.patch)
is separate from `series`: the shared build still applies exactly six patches.
The proposal preserves early structural loop repair, reserves late padding
for range calculations, and aligns bodies after branch/constant layout.
Its private backend passes all 102 Xtensa tests. The direct-object matrix
checks 12 loop ends and eight near/expanded branches by decoding bytes and
comparing destinations with independent block markers, across three target
profiles. Six corrupted-layout/target controls verify that the checker rejects
bad objects. Functions containing inline assembly conservatively use software
loops; unsupported explicit MIR loops with assembly are rejected rather than
guessing layout. The offset walk also honors bounded block alignment. These
are bounded regressions, not general firmware/interrupt qualification. The
proposal is not ready for activation; do not replace an installed SDK with it.
The rebuilt Rust-driver [device check](../../tests/event-services-comparison/COMPILER_PROBE.md#rebuilt-rust-driver-confirmation)
confirms that normal Cargo/native object output removes the repeated alignment
penalty in this handler: C/Rust median target-thread intervals are
10.025/10.027 ms. This does not establish equal wall-time deadlines or qualify
all loops, inline-assembly execution and interrupts.
Its separate [uninstrumented service check](../../tests/event-services-comparison/COMPILER_PROBE.md#uninstrumented-service-confirmation)
delivers all 124,800 messages in 32 matched NuttX C/Rust runs. The final Rust
delta is 844 B of code/data, 24 B of binary and 8 B of resident ELF RAM;
long-work deadline misses remain (1 C / 16 Rust). Other RTOSes were not rerun.

The [broader arithmetic suite](../../tests/arithmetic-parity/RESULTS.md) runs
1,136 primitive/conversion/expression cases with the same private candidate.
Its covered device results pass independent expectations, but 64-bit loops,
checked arithmetic and some math functions retain performance gaps. It does
not qualify universal parity or activate this compiler.

The separate [0008 proposal](proposals/0008-Xtensa-use-ESP32S3-set-less-than.patch)
adds the missing `SALT`/`SALTU` instruction definitions and value-comparison
selection for ESP32-S3, including multiword carry/borrow bits. Older cores keep
their existing lowering. CPU selection is automatic; application source and
features are unchanged. Its backend passes all 104 Xtensa CodeGen/MC tests and
compiles the full arithmetic corpus. The public eight-patch chain reproduces
all 27 affected compiled files from verified pinned preimages. A separate
[three-run device qualification](../../tests/arithmetic-parity/RESULTS.md#compiler-follow-up-separating-two-64-bit-costs)
passes 398,976 oracle checks with no C/Rust errors. The 64-bit recurrence
improves from 2.24× C's time to 1.43×, while its Rust wrapper shrinks from
199 to 171 B. Checked subtraction and signum also improve, but one small
saturating-negation case regresses. A separate non-native-width loop-strength-
reduction guard remains; this is not full performance parity. Use the
[explicit proposal setup](BUILDING.md#explicit-proposal-evaluation) to reproduce
it; the default six-patch series remains unchanged.

The [0009 wide-IV eligibility proposal](proposals/0009-IVUsers-consider-existing-non-native-induction-variables.patch)
removes an invariant counter product, but its isolated device candidate is
9.8% slower: it keeps a separate wide exit counter. The
[0010 exit-counter proposal](proposals/0010-IndVarSimplify-reuse-existing-wide-exit-counters.patch)
allows the existing directly tested wide unit-step counter to participate in
the ordered-comparison-to-equality transformation. Together, the compiler can
reuse the product counter for the exit. The
[combined device check](../../tests/arithmetic-parity/RESULTS.md#compiler-follow-up-reusing-the-wide-exit-counter)
reduces the recurrence from 8.14 to 7.02 µs/input versus C's 5.71, with a
171 → 147 B Rust wrapper. This is an improvement, not full parity. The public
ten-patch chain reproduces 32 affected files; 104 Xtensa and 399 supported
optimizer tests pass. Use `--proposal-set wide-exit` for this inactive candidate.
Neither generic proposal changes the real target layout, std or application
source. Profitability/upstream review and broader qualification remain needed;
the separate pre-existing `u128` call-frame verifier diagnostic was still
unresolved in that cohort. It is fixed by proposal 0012 below.

The [0011 branch-analysis proposal](proposals/0011-Xtensa-reject-distinct-compound-branch-conditions.patch)
fixes a separate correctness bug found while investigating the remaining wide
comparison cost: two branches with different operands are not duplicate
conditions merely because their opcodes match. The old branch folder can
drop one predicate. The regression reproduces that failure with a saved
six-patch compiler and passes with the fix, while identical conditions still
fold. All 33 affected files reproduce through the public eleven-patch chain;
105 Xtensa and 399 supported optimizer tests pass. Use
`--proposal-set branch-analysis` for this source/compiler-qualified candidate.
It has no new Rust-driver/device result and is not a claimed speed fix.
The first direct wide-inequality experiment was not included in that cohort:
its source-level regression coverage was insufficient. The independently
qualified follow-up is proposal 0014 below.

## Later arithmetic qualification

These additions remain inactive RFCs. The default is still six patches and
99 tests; applications do not gain flags, source workarounds or a false
native-width declaration.

| Proposal | Root cause and change | Xtensa gate |
| --- | --- | ---: |
| [0012](proposals/0012-Xtensa-preserve-call-frame-state-in-custom-CFG.patch) | Custom CFG splitting lost entry call-frame metadata. Carry the actual active frame into new blocks, including atomic CFG paths. | 107 |
| [0013](proposals/0013-Xtensa-select-integers-with-conditional-moves.patch) | Integer selects used branches despite conditional-move instructions. Model the tied false input and select the native moves. | 108 |
| [0014](proposals/0014-Xtensa-branch-on-paired-integer-inequality.patch) | Wide inequality materialized XOR/OR predicates. Keep a paired branch opaque through allocation, then expand to two native branches; handle both DAG branch polarities and long-range relaxation. | 109 |
| [0015](proposals/0015-Xtensa-use-Boolean-moves-for-FP-integer-selects.patch) | Floating predicates selecting integers formed branch diamonds. Reuse the predicate mapping and tied Boolean conditional moves with a virtual predicate register. | 110 |
| [0016](proposals/0016-HardwareLoops-admit-fitting-wide-constant-counts.patch) | Hardware-loop analysis rejected wide types even for small constant trip counts. Accept exact fitting counts, including the final iteration; keep dynamic wide counts excluded. | 111 |
| [0017](proposals/0017-Tests-harden-predicates-and-wide-loop-boundaries.patch) | Tests only: exact carry/borrow consumption, ordered FP predicate polarity with a live false input, wide counter live-outs, and direct 32-bit hardware-count limits. | 113 |
| [0018](proposals/0018-SelectionDAG-expand-mixed-widening-multiply.patch) | Mixed signed/unsigned half-width products used a multiplication for sign correction. Use an unsigned high product and a masked subtraction, in either operand order. | 114 |
| [0019](proposals/0019-Xtensa-bypass-wide-signed-overflow-multiply.patch) | Inactive profitability experiment: use a native widening product when both runtime signed `i64` operands fit `i32`, preserving the full-width overflow fallback and size-optimized policy. | 115 |
| [0020](proposals/0020-Xtensa-select-zero-comparisons-directly.patch) | Match integer zero equality/inequality directly with existing S3 `SALTU`; omit the XOR introduced by the general register-comparison pattern. This selection forks from 0018 and excludes 0019. | 115 |
| [0021](proposals/0021-SelectionDAG-check-signed-multiply-with-leading-sign-bits.patch) | Inactive generic lowering experiment: use leading sign bits and the wrapped product instead of a full double-width product when half-width `CTLZ` is legal. Both overflow outputs are preserved. It extends 0020, excluding 0019. | 116 |
| [0022](proposals/0022-Xtensa-select-native-sign-extension.patch) | Select native byte/halfword sign extension on capable cores; other profiles keep shifts. | 117 |
| [0023](proposals/0023-CodeGenPrepare-guard-expanded-saturating-FP-conversion.patch) | Range-check expanded saturating conversions before calling their helper; preserve NaN-to-zero and rounded thresholds. | 118 |
| [0024](proposals/0024-CodeGenPrepare-classify-NaN-in-guarded-casts.patch) | Classify NaN in the signed clamp block without an additional software floating comparison. | 118 |
| [0025](proposals/0025-CodeGenPrepare-guard-soft-float-saturating-conversions.patch) | Extend the conversion guard to softened floating operands with legal/promoted integer results; query the promoted helper width. | 119 |
| [0026](proposals/0026-Xtensa-reuse-native-extension-for-sign-mask.patch) | Reuse an existing byte/halfword extension for a multiword sign mask instead of duplicating its shift path. | 120 |
| [0027](proposals/0027-Xtensa-branch-on-single-bit-tests.patch) | Select core bit-test branches and complete branch analysis, inversion and far-target relaxation. | 122 |
| [0028](proposals/0028-SelectionDAG-discard-unused-BR_CC-simplifications.patch) | Discard a dead speculative comparison before it conceals one-use operands from target combines. | 122 |
| [0029](proposals/0029-SelectionDAG-simplify-zero-defaulting-selects.patch) | Simplify a nested predicate when both result arms are zero in its zero case; freeze the newly unconditional predicate and preserve separately observed flags. | 123 |

The `bit-branch` selection appends 0027–0028 to the frozen `sign-mask`
selection. Its [device assessment](../../tests/arithmetic-parity/RESULTS.md#direct-bit-test-branches)
passes the full corpus and reduces flash code by 172 B, with unchanged
static RAM sections and binary size. Wrapping power improves about 5% at
8–32 bits and 2% at 64 bits; small regressions in other power forms remain
visible. Its native gate is 601 supported passes, and the public chain
reconstructs all 72 affected files exactly. This is not universal speed
parity or default activation.

The `zero-select` selection appends 0029 without changing the frozen parent.
Its generic fold helps value-only `checked_mul(...).unwrap_or(0)`; it does
not discard a separately reported overflow flag. The full gate is 608
supported passes (123 Xtensa, 467 optimizer, 18 x86), with 104 unsupported
tests and three expected failures separate. The
[assessment](../../tests/arithmetic-parity/RESULTS.md#checked-multiply-value-only-results-versus-an-observed-overflow-flag)
distinguishes this code-generation result from unchanged device measurements.

Through 0015, the complete optimizer gate is 399 supported passes. 0016 adds
HardwareLoops, taking it to 407; unsupported cases and unchanged expected
failures remain separately recorded. Public patches reproduce the compiled
affected source from pinned preimages. Previous compiler controls fail the new
regressions; assertions and machine verification remain enabled.

The `hardening` selection appends 0017 without changing any earlier patch or
selection. Its complete gate passes 113 Xtensa and 409 optimizer tests, with
50 unsupported and three expected failures still reported separately. The
[tests-only evidence](../../tests/arithmetic-parity/results/compiler-hardening-2026-10-06.json)
records deliberate wrong-output rejection and supplementary host live-out
execution. The qualified native compiler is unchanged; this is not a new
device measurement or broader interrupt qualification.

The `mixed-mul` selection appends 0018. Its generic SelectionDAG change also
affects x86 wide overflow expansion, so the setup requires five additional
x86 overflow tests alongside 114 Xtensa and 409 optimizer passes. This is an
automatic lowering decision, not a Rust application feature. The new widening
regression fails with the unchanged seventeen-patch compiler control.
The [device follow-up](../../tests/arithmetic-parity/RESULTS.md#mixed-width-multiplication-follow-up)
passes the complete corpus but finds no meaningful checked-multiply speedup
on ESP32-S3. Fewer multiplies do not imply fewer instructions or cycles; this
candidate remains inactive and is not presented as closing the GCC gap.

The `mul-width` selection appends 0019 and keeps all parent gates. It is an
automatic target compiler pass, not an application feature or manual source
branch. Its runtime branch trades a small-operand fast path for guard and
layout costs when operands are wide; the separate operand-width diagnostic
[device assessment](../../tests/arithmetic-parity/RESULTS.md#runtime-operand-width-experiment)
finds a 30% narrow-input improvement but 17–27% slowdowns in the four
fixed wide-operand groups and 5.7% in the randomized mixture.
It remains inactive: the unchanged wide fallback must be addressed
before claiming general improvement. The default chain is unchanged.

### Regression tests and upstream suitability

A regression guard is a compiler test that detects a specific wrong result
or missed lowering. It is not an application workaround, a benchmark timing
threshold, or proof that a patch is acceptable upstream. Keep small compiler
tests separate from the broad device corpus and from evidence-consistency
tests that recompute published medians and ratios.
This separation follows the purpose of small regression cases in
[LLVM's testing guide](https://llvm.org/docs/TestingGuide.html#regression-tests).

The 0020 change uses existing TableGen instruction-selection rules and the
existing CPU-capability predicate. It matches any integer zero comparison,
not nxrs symbols, services or benchmark loops. For every unsigned 32-bit
bit pattern, `x < 1` means `x == 0`, and `0 < x` means `x != 0`. Nonzero
constants and unsupported CPUs retain their previous selection. The new
regression checks both operand orders and those controls with machine
verification; the old compiler fails its S3 zero-comparison checks.
The [device follow-up](../../tests/arithmetic-parity/RESULTS.md#zero-comparison-selection-follow-up)
passes both unchanged corpora and reduces full-diagnostic flash code by
1,188 B without changing RAM sections or padded binary size. Checked multiply
improves about 9% in the full image but is slightly slower in the separate
width diagnostic; general speed parity remains unproven.

The generic 0021 follow-up replaces the full double-width signed product check
with leading sign bits and the wrapped product where native half-width `CTLZ`
is legal. It preserves both intrinsic results and leaves libcalls, unsigned
expansion and unsupported targets alone. Its
[device assessment](../../tests/arithmetic-parity/RESULTS.md#signed-wide-multiply-with-leading-sign-bits)
passes both corpora: the width diagnostic improves about 9.6% in all groups,
while the checked kernel drops from eight multiplies to four with no larger
entry frame. The narrow-input C gap and broader profitability qualification
remain open. This proposal is inactive and does not change the default chain.

The 0019 width branch is not part of this selection. Its measured wide/mixed
regressions rule out default activation despite its correctness tests.
Upstream suitability still requires maintainer review, a current-base port,
and broader profitability/target qualification. Local test counts and device
results do not establish upstream acceptance.

The [arithmetic assessment](../../tests/arithmetic-parity/RESULTS.md) keeps
matched device evidence, remaining differences and activation limits in one
place. The `u128` verifier diagnostic is no longer waived: the call-frame
regressions and the extracted affected corpus compile with machine verification.
This does not individually qualify every atomic CFG, hardware-loop interrupt
context or floating context switch. The separate
[std math RFC](../rust-std/README.md) changes library selection, not LLVM;
its ledger and source inventory are independent.

0001 reuses existing lowering rather than adding a new instruction sequence.
It covers both rotate directions, constant and variable amounts, zero, and
amounts greater than 31; wider rotates retain their existing legalization.
0002 enables hardware loops only where the backend's existing target, literal,
call, inline-assembly and loop-layout checks permit them. This remains an
evaluation candidate, not a universally certified default. Firmware-wide,
interrupt and broader loop-safety qualification is still outstanding.

The rebuilt LLVM passes all 99 Xtensa CodeGen and MC tests (63 CodeGen, 36
MC), including `verify-machineinstrs` on the new tests and fallback controls.
A fresh private affected-source copy was reversed to the pinned SHA-256
originals, then the public applicator applied all six patches. All 18 affected
files exactly match the compiled source. This includes the scheduling model,
loop-boundary regression and refreshed full golden checks.
The [device report](../../tests/event-services-comparison/results/esp32s3-compiler-patched-2026-10-05.json)
separately records actual compiler/driver, linker, patch and firmware hashes.
It is workload evidence, not universal hardware-loop qualification.

## Apply only to a private compiler source archive

Create an archive of the exact LLVM revision below in a fresh build directory.
Do not point the applicator at an installed SDK or the upstream Git checkout.
It rejects Git checkouts, wrong revisions, changed pinned affected-source
hashes and reapplication, and records patch SHA-256 plus before/after source
hashes.

```sh
python3 tools/apply-nuttx-patches.py \
  --component rust-llvm \
  --source /absolute/path/to/private/llvm-source-archive \
  --revision 14b9f5575f37489d4c25c069d04c9ed216dadc31 \
  --record /absolute/path/to/private/llvm-patches.json
```

This command prepares source; it does not build or install a compiler. Never
replace the compiler in an installed SDK to reproduce this evaluation.

## Private compiler build used in the evaluation

Build LLVM from the pinned archive with X86 and Xtensa enabled, assertions on,
static LLVM libraries, and `llc`, `opt`, assembler/disassembler, MC and test
tools. The host C/C++ build used O1 to reduce build resources; firmware retains
its original O2 policy. Run `llvm-lit` over both Xtensa CodeGen and MC suites.

Use an exact archive of the Rust revision in `upstream.json`, including its
pinned submodules. Its private `bootstrap.toml` selects the locally rebuilt
`llvm-config`, disables downloaded CI LLVM/Rust compilers, and builds the
native Linux host. The completed bootstrap command was:

```sh
python3 x.py build --stage 1 compiler/rustc library --jobs 1
```

Package the resulting compiler and matching host libraries in a **fresh**
private sysroot. Copy the already-qualified embedded std source snapshot into
a real independent directory; do not follow the bootstrap sysroot's source
symlink back into the Rust compiler archive. This keeps NuttX's std adaptations
and core inputs byte-identical between controls. Record the actual `rustc -vV`
output, compiler/driver hashes and patch ledger. An archive bootstrap reports
an unknown commit in `-vV`; the known source pin belongs in separate provenance.

The evaluation compiled Rust in Linux and kept NuttX's original Mac final
link, kernel and C helpers. Its unpatched cross-host control changed only
timestamp/checksum bytes. Embassy's same-host unpatched control has small
host/build differences from the original Mac artifact, reported separately.
The repository build helper still uses the installed SDK by default; it does
not silently activate or install this evaluation compiler.

## Native sign extension and wide-cast follow-ups

The inactive [0022](proposals/0022-Xtensa-select-native-sign-extension.patch)
selects existing native SEXT for signed byte/halfword extension, with explicit
capability-disabled fallbacks. [0023](proposals/0023-CodeGenPrepare-guard-expanded-saturating-FP-conversion.patch)
guards expanded scalar wide casts so a conversion helper is not called for
values that will clamp. [0024](proposals/0024-CodeGenPrepare-classify-NaN-in-guarded-casts.patch)
then uses LLVM's floating-class intrinsic for the signed NaN result, avoiding
an additional software comparison where direct classification is cheaper.
[0025](proposals/0025-CodeGenPrepare-guard-soft-float-saturating-conversions.patch)
extends the evaluation to software floating operands with legal/promoted
integer results, using the promoted width's conversion helper.

These are automatic compiler decisions, not application fast paths. Native
floating conversion, vectors and size-optimized functions remain unchanged.
0023 excludes legal/custom conversion; 0025 also admits software floating
operands, and its interaction with other targets' custom hooks remains an
explicit portability limit. The [build guide](BUILDING.md#native-sign-extension-and-guarded-wide-casts)
names each selector; the [arithmetic evaluation](../../tests/arithmetic-parity/RESULTS.md)
keeps finite/clamping timing, whole-corpus qualification and code size separate.
The default six-patch chain, installed SDK and CI configuration are unchanged.

## Proven-range checked multiplication

The inactive [0030](proposals/0030-SelectionDAG-simplify-bounded-signed-multiply.patch)
uses existing SelectionDAG range analysis to replace unnecessary signed
overflow arithmetic with a product-sign test. It requires nonnegative
operands and a proven unsigned-fitting product, preserving the wrapped
product and overflow flag. Legal integer types retain native overflow
instructions; full-width unknown inputs keep the existing lowering.

Select `mul-range` through the same [local/CI builder](BUILDING.md).
The [matched Rust/C probes](../../tests/arithmetic-parity/RESULTS.md#checked-multiply-when-input-ranges-are-known)
show smaller bounded-input functions. All 610 supported compiler tests pass;
the existing full-corpus Rust linker input is byte-identical to its parent.
This is neither a new device-speed measurement nor universal profitability
or upstream-acceptance evidence. The default series and CI stay unchanged.

## Qualification before activation

1. Extend device qualification for rotate amounts and loop boundary counts, nested loops, early
   exits, large bodies, calls, assembly and interrupt-heavy execution.
2. Trace the remaining full-service response difference; the rebuilt-driver
   matrix confirms loss-free delivery and removes the repeated arithmetic
   penalty, but does not establish deadline parity.
3. Consider activation only after those checks pass, and record the patch
   manifest in measured artifacts. No application flag or target assembly
   helper is needed.

Until those steps pass, the patchset remains inactive and evaluation-only.
Source checks, LLVM tests and this bounded device matrix do not substitute
for wider firmware, interrupt and loop-safety qualification. The earlier
discarded assembly control is not compiler-patched evidence.
