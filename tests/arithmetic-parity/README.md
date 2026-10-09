# C/Rust arithmetic qualification

This local ESP32-S3 diagnostic checks correctness, generated code and timing
of equivalent arithmetic kernels. It is not a production dependency or proof
that every program has identical performance. The coverage manifest records
each tested operation, its input semantics and any missing C counterpart.

Both implementations run in one firmware with the same kernel, clock, timer,
input data and C measurement driver. Rust uses native compiler object output,
not assembly reassembly or application instruction workarounds. Dependency
fixes remain separately recorded compiler patches, not edits to an SDK.

See the [device findings and conclusion](RESULTS.md), including remaining
performance differences. Full numerical reports retain the
[seven-patch baseline](results/esp32s3-2026-10-06.json) and the separate
[eight-patch carry/comparison candidate](results/esp32s3-setlt-2026-10-06.json)
and [nine-patch wide-IV candidate](results/esp32s3-wide-iv-2026-10-06.json),
plus the [ten-patch exit-counter follow-up](results/esp32s3-wide-exit-2026-10-06.json).
The later reports isolate [integer conditional moves](results/esp32s3-conditional-move-2026-10-06.json),
[paired wide branches](results/esp32s3-paired-branch-2026-10-06.json) and
[std math selection](results/esp32s3-std-math-2026-10-06.json). The
[combined paired-branch/math control](results/esp32s3-paired-branch-math-2026-10-06.json)
checks their interaction. The [final hardware-loop/math report](results/esp32s3-constant-hwloop-math-2026-10-06.json)
adds floating conditional moves and fitting wide constant hardware-loop counts.
Earlier reports remain unchanged.

Correctness uses independently generated Python expectations. C must not use
undefined signed overflow, invalid shifts or invalid float-to-integer casts.
Checked operations report invalid input rather than invoking a trap. Timing
uses the same directed/random corpus, including defined checked-error and
floating domain-error results. Each kernel is warmed before timing; all five
repeats of each mode and each complete run are retained.

## Coverage

The catalog has **1,136 cases and 72,704 input vectors**, with 64 directed and
deterministic random vectors per case. The generated manifest records exact
input/expected bits, overflow contracts, accuracy budgets, source hashes and
retained C/Rust symbols.

| Group | Coverage |
| --- | --- |
| Integers: 1,000 cases | Signed/unsigned 8, 16, 32, 64, 128 bits and 32-bit `usize`/`isize`; add/subtract/multiply/divide/remainder, power and negation; wrapping, checked, saturating and overflowing APIs where available. |
| Integer utilities | Shifts, variable/constant rotates, Boolean bit operations, bit/byte reversal, bit counts, comparisons, min/max, absolute difference, logarithms/square roots, Euclidean division, sign/absolute value, multiples and powers of two; all integer-width casts. |
| Conversions: 52 cases | Integer↔`f32`/`f64`, float-width conversion and bounded integer-exponent power. C float-to-int comparisons reproduce Rust's saturating casts without C undefined behavior. |
| Floating: 84 cases | `f32`/`f64` arithmetic/remainder, reciprocal, square root, explicit fused multiply-add, rounding/fractional part, min/max/sign operations, exponential/logarithmic, trigonometric/hyperbolic functions, power, cube root and hypotenuse. |
| Mixed code generation | Rotate/multiply/add recurrence and loop lengths around zero, one and larger boundaries. No application assembly workaround. |

There are **942 matched C/Rust cases** and **194 Rust-only cases**: Xtensa GCC
has no native 128-bit integer type. Rust-only cases still check independent
expectations, but do not manufacture C timing/size ratios. The host additionally
exhausts all 65,536 8-bit operand pairs for selected wrapping arithmetic,
checked division/remainder, shifts, rotates and bitwise operations. That
exhaustive extension is host-only, not an ESP32-S3 execution result.

Integer expectations use Python arbitrary-precision integers. Basic floating
operations use exact fractions and IEEE round-to-nearest-even encoding; math
functions use pinned `mpmath` at 100 decimal digits. Integer/basic floating
results require exact agreement; square root allows 1 ULP, libm 2 ULP and
bounded `powi` 4 ULP. These are qualification budgets, not Rust accuracy
guarantees. NaN payloads need not match; zero sign is required except where
Rust min/max permits either zero sign. Raw C/Rust bit differences are retained
separately, so a semantic pass does not imply bit-identical output.

This is broad coverage, **not every primitive method or arithmetic program**.
It does not qualify unsafe unchecked APIs, panic/trap paths, nightly carry/
widening intrinsics, SIMD/DSP, atomics, complex/decimal/fixed-point libraries,
every transcendental input, alternative rounding modes, or every target and
optimization configuration. Float classification, `total_cmp`, adjacent-float
stepping, degree/radian conversion, float Euclidean helpers and application
expressions remain possible extensions. Floating-point context preservation
between preempting worker threads is a separate RTOS qualification, not tested
by this single-thread suite. Assignment/operator spellings are not
separately enumerated from their operation. Passing is not universal C/Rust
correctness, speed or firmware-size parity.

## Local reproduction

Host checks require native C/Rust compilers on `PATH`. The Python dependency
is test-host-only, not a firmware dependency:

```sh
python3 -m venv target/arithmetic-python
target/arithmetic-python/bin/pip install -r tests/arithmetic-parity/requirements-test.txt
target/arithmetic-python/bin/python -m unittest discover -s tests/arithmetic-parity
target/arithmetic-python/bin/python tests/arithmetic-parity/generate.py --out target/arithmetic-corpus
target/arithmetic-python/bin/python tests/arithmetic-parity/host_check.py \
  --generated target/arithmetic-corpus --exhaustive-8
```

Every generated/build/capture output must be fresh. `--integers-only` avoids
floating dependencies; repeated `--case` substring filters allow small shards
for boards with less flash. The complete diagnostic's vectors alone need
roughly 5 MiB of flash; they are not production code or persistent runtime RAM.

Use the independent compiler package and NuttX std snapshot described in
[the compiler build notes](../../upstream/rust-llvm/BUILDING.md). The measured
baseline includes the seventh alignment proposal (`--proposal-set alignment`);
the carry/comparison follow-up adds the eighth (`--proposal-set setlt`), and
the wide-IV follow-up adds the ninth (`--proposal-set wide-iv`). The exit-counter
follow-up adds the tenth (`--proposal-set wide-exit`).
Later selections are `conditional-move` (13 patches), `paired-branch` (14),
`fp-select` (15) and `constant-hwloop` (16). The optional std math RFC is prepared
and bound separately; see its [local/CI setup](../../upstream/rust-llvm/BUILDING.md#optional-std-math-rfc).
`hardening` (17) appends only compiler regression tests; it does not change
generated firmware or replace the sixteen-patch device results. Its
[test evidence](results/compiler-hardening-2026-10-06.json) is separate.
`mixed-mul` (18) adds automatic mixed signed/unsigned widening multiplication
lowering; its setup also gates the affected x86 overflow tests. It keeps the
same application and std snapshot; it does not add application fast paths.
Its [device report](results/esp32s3-mixed-mul-math-2026-10-06.json) passes the
full corpus but finds no meaningful checked-multiply speedup on the S3. It is
an inactive code-generation RFC, not a claim that the residual gap is closed.
`mul-width` (19) evaluates an automatic runtime-width fast path for checked
signed `i64` multiplication. It excludes size-optimized functions and keeps
the full-width fallback. Use the separate eight-group corpus to distinguish
small-operand wins from fallback costs; it does not replace the full catalog:

```sh
python3 tests/arithmetic-parity/mul_widths.py --out target/arithmetic-mul-widths
```

Pass that generated directory to the same build, measure and report commands
below. The kernels still use ordinary Rust `checked_mul` and C's overflow
builtin. This single-process generator temporarily substitutes the catalog
and vector provider, restoring both even on failure; original generator and
full-corpus source hashes remain unchanged.
The [paired device assessment](RESULTS.md#runtime-operand-width-experiment)
finds faster both-narrow inputs but slower wide/mixed groups; 0019 remains
inactive and is not a general speed-parity fix.
`zero-compare` is a separate 19-patch selection: the `mixed-mul` parent plus
0020, excluding 0019. It removes redundant XOR-with-zero instructions through
ordinary target selection. Use the same unchanged full and operand-width
corpora; successful assembly checks alone do not establish device speed parity.
`signed-overflow` extends `zero-compare` with proposal 0021 (20 patches in
total). It replaces the generic double-width signed overflow expansion where
native half-width leading-zero counts are available. It preserves the wrapped
product on overflow and adds no application fast path or runtime-width branch.
The [standalone model](signbits_model.py) tests its mathematical identity;
compiler regressions and actual device results remain separate evidence.
`native-sext` adds 0022 (21 patches): use the existing native instruction for
in-register signed byte/halfword extension, with unsupported cores unchanged.
`guarded-casts` adds 0023 (22 patches): avoid executing an expanded wide
float-to-integer helper for inputs that will clamp. Native/custom casts,
vectors and size-optimized functions keep their previous lowering.
`fpclass-casts` adds 0024 (23 patches), classifying NaN directly in the
signed low-result block instead of requesting another floating comparison.
This keeps f32's native comparison while allowing software f64 classification
without the extra helper call. Measure it against `guarded-casts` separately.
`soft-casts` adds 0025 (24 patches), extending the guard to software floating
operands with legal/promoted integer results. Native f32 casts on ESP32-S3
remain controls. Other targets' custom conversions and helper inventories
need separate qualification; this is not a universal profitability claim.
`sign-mask` adds 0026 (25 patches), reusing an existing native sign extension
for the upper words of a widened integer. It introduces no new extension for
lone masks and keeps cores without SEXT unchanged. Evaluate with the full
corpus; the x86 cast checks below remain controls, not execution of this
Xtensa-specific combine.

`bit-branch` adds 0027–0028 (27 patches), selecting direct single-bit
branches with complete inversion/relaxation support and removing a dead
generic simplification that hid target one-use opportunities. Keep the
`sign-mask` control, frozen std snapshot and full corpus unchanged. Native
branch tests include small/large masks, both polarities, live shared values
and far targets; host casts remain a supplementary generic regression check.

`zero-select` adds 0029 (28 patches). A generic zero-defaulting select fold
uses LLVM `freeze` to make a newly unconditional predicate safe. It keeps
independently observed flags and nonzero sentinels unchanged. This benefits
value-only checked arithmetic; it is not a speed-parity claim for the full
corpus, which also reports overflow. Its native execution check reads the
IR fixture installed by the patch; run it on Linux x86_64 with both backends:

```sh
python3 tests/arithmetic-parity/compiler/check_zero_select.py \
  --candidate "$AQ_LLVM_BUILD/bin/llc" --control "$AQ_PARENT_LLC" \
  --source "$AQ_LLVM_SOURCE/llvm/test/CodeGen/Xtensa/zero-select-condition.ll" \
  --out "$AQ_ZERO_SELECT_CHECK"
```

These are host correctness checks, including defined zero-result inputs
that mask poison-producing predicates. They are not a formal proof of LLVM
semantics or an ESP32-S3 latency measurement. The pinned builder runs all
parent gates and the new Xtensa/x86 fixtures before compiling the Rust driver.

For the [small Rust reproducer](compiler/zero-select.rs), keep the target
JSON and previously built core/compiler-builtins archives identical between
the parent and candidate. Repeat this command with each qualified sysroot:

```sh
"$AQ_SYSROOT/bin/rustc" tests/arithmetic-parity/compiler/zero-select.rs \
  --crate-name zero_select_probe --edition=2021 --crate-type=rlib \
  --target "$AQ_TARGET" --sysroot "$AQ_SYSROOT" \
  --extern "core=$AQ_CORE_RLIB" --extern "compiler_builtins=$AQ_BUILTINS_RLIB" \
  -L "dependency=$AQ_CORE_DEPS" -C opt-level=2 -C panic=abort \
  --emit=llvm-ir,asm,obj --out-dir "$AQ_VALUE_PROBE_OUT"
```

Create separate output directories and compare their objects with the same
Xtensa `nm -S`/`objdump -d`. `no_std` keeps this a three-function compiler
reproducer; it is not a proposed firmware configuration or footprint test.

`mul-range` adds 0030 (29 patches), using proven nonnegative operand ranges
and unsigned product bounds to simplify signed overflow. Unknown full-width
inputs and native legal-width overflow instructions remain unchanged. Run
its independent exact-product checks on Linux x86_64:

The compiler evaluation is now frozen; see the [handoff and stop rule](FROZEN.md)
for its scope, unchanged full-corpus output, and real-application qualification
gate.

```sh
python3 tests/arithmetic-parity/compiler/check_mul_ranges.py \
  --candidate "$AQ_LLVM_BUILD/bin/llc" --control "$AQ_PARENT_LLC" \
  --source "$AQ_LLVM_SOURCE/llvm/test/CodeGen/X86/smulo-known-range.ll" \
  --out "$AQ_MUL_RANGE_CHECK"
```

To compare function bytes, repeat the Rust command above with
`compiler/mul-range.rs`, crate name `mul_range_probe`, and separate output
directories for the `zero-select` parent and `mul-range` candidate. Compile
the [same-contract C probe](compiler/mul-range.c) with the same Xtensa GCC:

```sh
"${AQ_GNU_PREFIX}gcc" -O2 -c tests/arithmetic-parity/compiler/mul-range.c \
  -o "$AQ_C_RANGE_OBJECT"
```

Both languages return zero on overflow and separately store the overflow
flag. Compare symbol bytes/instructions, not whole binaries or shared-helper
totals. The [results](RESULTS.md#checked-multiply-when-input-ranges-are-known)
separate these static-range probes from unchanged full-corpus device timing.

For casts, evaluate ordinary finite values separately from clamping inputs:

```sh
python3 tests/arithmetic-parity/conversion_ranges.py --out "$AQ_CAST_CORPUS"
```

This emits 16 groups (four conversion families × finite, low, high and NaN
ranges), each with 64 inputs. Use both parent and candidate compilers with the
same corpus and the build/measure/report commands below. The finite groups
include small fractions, large values and adjacent valid threshold values.
They are a diagnostic distribution, not a workload frequency model.

The small-width companion uses the same generator and independent oracle:

```sh
python3 tests/arithmetic-parity/conversion_small_ranges.py --out "$AQ_SMALL_CAST_CORPUS"
```

It emits 48 groups: f32/f64 to signed/unsigned 8-, 16- and 32-bit integers,
separated into finite, low, high and NaN ranges. Directed values include exact
small-width maxima and fractions between a maximum and the next integer.
Evaluate both `fpclass-casts` and `soft-casts` with identical generated inputs;
the full corpus remains a separate broader correctness check.

Supplementary generic lowering execution on Linux x86_64 checks 270,592
inputs per compiler, across signed/unsigned i64/i128 destinations:

```sh
python3 tests/arithmetic-parity/compiler/check_fp_casts.py \
  --candidate "$AQ_LLVM_TOOLS/llc" --control "$AQ_PARENT_LLC" \
  --out "$AQ_NATIVE_CAST_CHECK"
```

The i64 native casts are controls on that host; expanded i128 casts exercise
the generic transformation. These checks do not replace ESP32-S3 execution.
The normal six-patch helper does **not** activate these proposals. The builder binds actual
compiler/driver hashes, std inventory and ledger to package provenance.
A patch list alone is insufficient. On the Linux compiler-build host:

```sh
python3 tests/arithmetic-parity/build.py rust \
  --generated "$AQ_GENERATED" --sysroot "$AQ_SYSROOT" \
  --cargo "$AQ_CARGO" --target "$AQ_TARGET_JSON" --ld "$AQ_XTENSA_LD" \
  --ledger "$AQ_PATCH_LEDGER" --compiler-provenance "$AQ_COMPILER_PROVENANCE" \
  --cargo-target "$AQ_CARGO_CACHE" --out "$AQ_RUST_BUNDLE"
```

Transfer the immutable corpus and Rust bundle to the final-link host. The tree
must be an isolated, prepared NuttX SDK with working local symlinks and the
existing `nxrs_std_app` registration helper. If copying it refreshes board
objects, prime it first; qualification rejects changed kernel archives.

```sh
python3 tests/arithmetic-parity/build.py link \
  --generated "$AQ_GENERATED" --bundle "$AQ_RUST_BUNDLE" \
  --tree "$AQ_PRIVATE_NUTTX_TREE" --prefix "$AQ_GCC_PREFIX" --out "$AQ_LINKED"
python3 tests/arithmetic-parity/measure.py \
  --image "$AQ_LINKED/image.bin" --coverage "$AQ_GENERATED/coverage.json" \
  --backup "$AQ_FULL_PRIVATE_BACKUP" --port "$AQ_SERIAL_PORT" \
  --flasher "$AQ_ESPTOOL" --out "$AQ_PRIVATE_CAPTURE" --runs 3
python3 tests/arithmetic-parity/report.py \
  --generated "$AQ_GENERATED" --linked "$AQ_LINKED" \
  --captured "$AQ_PRIVATE_CAPTURE" --out "$AQ_PUBLIC_REPORT"
```

Run measurement only on a board intended for flashing, with a fresh full
16 MiB backup. The collector restores and verifies it in `finally`, including
failure paths. Backups/raw logs remain private. Export reparses every run and
verifies identities before producing numeric evidence. No CI dispatch,
installed-toolchain replacement or automatic activation is part of this suite.

## Reading size and timing

### Compiler strength-reduction diagnosis

[The minimal IR reproducer](compiler/strength-reduction.ll) isolates the
retained `counter * 0x7f4a7c15` term without application or std machinery.
With the pinned compiler tools, run only the loop-strength-reduction pass:

```sh
"$AQ_LLVM_TOOLS/opt" -mcpu=esp32s3 -passes=loop-reduce -S \
  tests/arithmetic-parity/compiler/strength-reduction.ll -o "$AQ_LSR_OUTPUT"
```

With the eight-patch control, the native-width multiply becomes an incrementing
induction variable, but the 64-bit multiply remains. At the pinned LLVM revision, `IVUsers.cpp` rejects
non-native integer widths before LSR considers its candidates. The same miss
occurs in the actual Rust kernel's IR. An analysis-only native-width declaration
of `n32:64` admits the wide recurrence and removes its multiply; this diagnoses
the eligibility guard, not an application fix. **Do not change Xtensa's real
data layout or compile that counterfactual into firmware.** The `SALT`/`SALTU`
proposal separately addresses carry/comparison code generation, not this guard.

The [wide-IV proposal](../../upstream/rust-llvm/proposals/0009-IVUsers-consider-existing-non-native-induction-variables.patch)
removes the wide multiply with the real `n32` layout. It only admits an existing
affine loop-header integer PHI wider than the declared native widths, and
same-width users; casts from narrow counters and widths above 64 retain their
guard. LLVM's existing cost model still decides which recurrence to use.
The patch includes bounded/wrapping recurrences and exclusion controls. The
[device follow-up](RESULTS.md#compiler-follow-up-existing-wide-induction-variables)
keeps this optimizer change separate from instruction selection and std costs.

Removing that multiply alone does not establish a speed improvement: the
nine-patch candidate retains a separate 64-bit counter and is slower on the
device. `IndVarSimplify::FindLoopCounter` independently excludes non-native
widths, preventing its ordered exit comparison from becoming an equality
test that LSR can rescale to the product counter. The
[exit-counter proposal](../../upstream/rust-llvm/proposals/0010-IndVarSimplify-reuse-existing-wide-exit-counters.patch)
admits only an existing directly tested wide integer unit-step counter, with
the trip-count and poison/expansion safeguards unchanged. Run `indvars` before
`loop-reduce` when inspecting this combined transformation; LLVM's normal
optimization pipeline already runs these passes. No application source,
special-case literal or target-layout override is needed. See the
[combined device findings](RESULTS.md#compiler-follow-up-reusing-the-wide-exit-counter).

### Measurement scope

Both applications use `-O2`; Rust retains the qualified size-built std, fat LTO
and aborting panics. C disallows fast-math and implicit floating contraction;
math `errno` is outside the contract. Every batch includes call, load and store
costs. Power, integer square root/logarithm and mixed-loop cases do much more
work than one add; averaging all ratios is not a meaningful language ranking.
Normal timings include interrupts/preemption. Masked timings describe warm
execution at this flash placement, not cold-cache or scheduling guarantees.

Per-function bytes exclude shared helpers and literal pools; symbols may also
alias. The combined image contains **both** languages and test vectors.
Neither its size nor a sum of symbol sizes is the production C/Rust binary
delta. That still requires separate matched application links and shared-helper
accounting. Output buffers occupy a fixed 3,072 bytes, not per-case heap.
