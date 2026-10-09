# Reproducible Linux evaluation build

This opt-in build packages the pinned Xtensa patches and Espressif Rust stage 1
compiler in a new private output directory. It does not change the repository's
active Rust toolchain or install anything into an SDK. The patchset remains
`draft-unqualified`.

Use the same command locally and in Linux CI. Provide clean Git checkouts of
the two pinned revisions in `upstream.json`, with every pinned submodule
initialized recursively. `--std-source` must name an independent directory
containing the already-qualified Rust library source (either that `library`
directory itself or its parent), including `core/src/lib.rs`. The builder
checks that snapshot before starting any build. The source checkouts and std
snapshot are read-only inputs.

Prepare the inputs in dedicated directories (not an installed SDK checkout):

```sh
git clone https://github.com/espressif/llvm-project "$LLVM_SOURCE"
git -C "$LLVM_SOURCE" checkout --detach 14b9f5575f37489d4c25c069d04c9ed216dadc31
git clone https://github.com/esp-rs/rust "$RUST_SOURCE"
git -C "$RUST_SOURCE" checkout --detach abf50ae2e46066e67e29fe856f9764c23aa9a3ca
git -C "$RUST_SOURCE" submodule update --init --recursive
```

For the NuttX-qualified std input, use the existing blob/checksum-pinned
[std patch generator](../../tests/nuttx-std/prepare-std.py) against an original
`esp-1.90.0.0` SDK source:

```sh
python3 tests/nuttx-std/prepare-std.py \
  --source "$ORIGINAL_ESP_SDK" --sdk esp-1.90.0.0 \
  --output "$FRESH_STD_PREPARATION"
```

Set `STD_SOURCE` to that output's
`toolchain/lib/rustlib/src/rust/library`. Retain its `std-patch.json` and
generated `.patch` files alongside compiler provenance. The generator rejects
different source blobs; copying an arbitrary unpatched `library` directory
is not equivalent to the qualified NuttX std input. See the
[std qualification notes](../../docs/nuttx-std.md) for the SDK prerequisites.

```sh
python3 tools/build-rust-llvm.py \
  --llvm-source "$LLVM_SOURCE" \
  --rust-source "$RUST_SOURCE" \
  --std-source "$STD_SOURCE" \
  --out "$EVALUATION_OUT" \
  --jobs 1
```

Run the same command with `--plan` first to check pins and print the build
sequence without creating the output. The output path must not already exist.
The host needs Linux x86_64, Git, Python 3.12 or newer, CMake, Ninja, a C/C++
toolchain and enough disk space for LLVM and Rust builds. Python's safe tar
extraction filter protects the local recursive Git archive export. Export
does not fetch submodules or need network access.

### Explicit proposal evaluation

The default remains six patches and a 99-test gate. To reproduce a proposal
candidate, add `--proposal-set alignment` (seven patches, 102 Xtensa tests),
`--proposal-set setlt` (eight patches, 104 Xtensa tests), or
`--proposal-set wide-iv` (nine patches, 104 Xtensa tests plus the
LoopStrengthReduce/IVUsers suites). `--proposal-set wide-exit` adds the tenth
patch and the IndVarSimplify suite. `--proposal-set branch-analysis` adds an
independent branch-analysis correctness fix (eleven patches, 105 Xtensa tests
plus the same optimizer suites). Add the selection to the same command,
including its `--plan` preflight. The [named manifest](proposals/series.json)
defines the ordered additions; arbitrary patch paths are not accepted.
These selections include the final-layout alignment proposal and the ESP32-S3
compare-to-integer instruction proposal. `wide-iv` additionally admits eligible existing wide
induction variables to loop strength reduction without changing target layout.
`wide-exit` also lets an existing wide unit-step exit counter participate in
the ordered-comparison-to-equality transformation. These are evaluation
candidates; removing a multiply alone regressed the measured wide loop.
This is a compiler-build selector, not an application
feature or a change to the active SDK.

All selections record the complete patch chain, proposal markers, selected
name and expected compiler-test count in the ledger and package provenance.
The composite action accepts the same optional `proposal-set` input. No CI
compiler build is dispatched by selecting or documenting a candidate.

The `wide-iv` selection also checks pinned Analysis source preimages and runs
the complete LoopStrengthReduce/IVUsers component suites before Rust bootstrap.
At these pins with X86/Xtensa built, exactly 161 supported tests must pass;
36 other cases are unsupported because their required backends are absent,
and one is an unchanged expected failure. The builder requires a successful
lit exit and records those discovery counts separately. It does not remove
tests or weaken existing assertions to obtain a passing count.

The `wide-exit` selection additionally pins the IndVarSimplify source preimage.
Its three complete optimizer suites require 399 supported passes; 43 cases
need absent backends and three are unchanged expected failures. These counts
are separate from the 104-test Xtensa gate. The public ten-patch chain has
also been reproduced against all 32 affected files in the locally rebuilt
compiler; the new exit-counter regression fails against the previous optimizer
and passes with the candidate. This local incremental qualification is not a
fresh CI compiler build or permission to replace the installed SDK.

The `branch-analysis` selection preserves distinct conditional predicates
instead of treating matching branch opcodes as duplicate conditions. Its
public eleven-patch chain reproduces all 33 affected files; 105 Xtensa and the
same 399 supported optimizer tests pass. The MIR regression covers differing
register pairs, differing opcodes and an identical-condition positive control.
A hash-verified saved six-patch compiler with the original analyzer fails the
regression by dropping a distinct predicate. This follow-up is source/compiler
qualification only: no eleven-patch Rust driver or device measurement is
claimed. The measured ten-patch arithmetic kernel remains byte-identical
when compiled with the rebuilt backend.

The builder exports both pinned sources into the output, applies the existing
ordered patch series to the LLVM export, builds LLVM with X86 and Xtensa enabled,
assertions on and static linking, and runs the Xtensa CodeGen and MC lit suites
before starting the Rust stage 1 build. It then copies the built sysroot and
detaches copied links back to the Rust source archive before adding the
supplied independent source tree. `build-provenance.json` records the pins,
patch ledger, executed commands, actual `rustc -vV`, `rustc` and
`librustc_driver` hashes, LLVM tool hashes, and std-source inventory. Archive
builds may report an unknown commit in `rustc -vV`; the separate pinned
revision in the provenance is authoritative.

This output is a compiler evaluation sysroot, not a complete Rust SDK. The
stage 1 configuration does not build Cargo or package `rustdoc` and a bundled
linker. Application builds that need Cargo and a target linker, including
Embassy evaluation, must supply those tools from separately pinned inputs.
Nothing is installed into an SDK.

## CI integration

The [composite action](action.yml) calls that same builder. In an explicitly
enabled Linux job, check out nxrs, prepare the clean pinned inputs and
independent std snapshot, then use:

```yaml
- uses: ./upstream/rust-llvm
  with:
    llvm-source: /absolute/path/to/pinned-llvm-checkout
    rust-source: /absolute/path/to/pinned-rust-checkout
    std-source: /absolute/path/to/qualified-library-snapshot
    out: /absolute/path/to/fresh-evaluation-output
    jobs: '1'
```

Normal portable CI runs the fast applicator and setup-guard tests, not this
expensive compiler build. No self-hosted runner registration, event trigger,
or installed toolchain is changed by this package. Preserve
`llvm-patches.json` and `build-provenance.json` with any firmware measurements.
Compiled applications must record which evaluation sysroot they actually use;
a patched source checkout alone is not evidence of patched firmware.

The public helper completed a fresh local Linux build at these pins on
2026-10-05: all six evaluation patches applied, all 99 Xtensa CodeGen/MC tests
passed, Rust stage 1 built, and the independent std snapshot was packaged.
The compiler/driver hashes and complete std inventory were checked against
`build-provenance.json`; the packaged tree has no source links back into the
build checkout. Guard and sequencing tests also pass. No CI compiler build
has been dispatched, and this does not qualify the separate alignment proposal
or activate this compiler in normal builds.

## Qualification scope

The `call-frame` selection adds the call-frame metadata correctness fix and
requires 107 Xtensa tests. `conditional-move` adds tied integer conditional
move selection and requires 108. Both retain the 399-pass optimizer gate.
These build-time evaluation selections do not add application feature flags.

`paired-branch` adds direct word-by-word wide inequality branches (fourteen
patches, 109 Xtensa tests). `fp-select` adds Boolean conditional moves for
floating predicates selecting integer values (fifteen patches, 110 tests).
Both retain the 399-pass optimizer gate. `constant-hwloop` additionally admits
wide constant trip counts that fit the hardware counter (sixteen patches,
111 Xtensa tests). It adds the complete HardwareLoops suite: 407 supported
optimizer passes, 50 unsupported cases requiring absent backends, and three
unchanged expected failures. Dynamic wide trip counts retain their fallback.

Use `--proposal-set hardening` to append the tests-only seventeenth patch.
It retains the exact sixteen-patch compiler changes, but requires 113 Xtensa
and 409 optimizer passes across the same four optimizer paths. In the composite
action, set `proposal-set: hardening` for the same selection. Earlier selections
and the six-patch default keep their original gates. The additional checks
cover carry/borrow operands and consumers, ordered FP predicate inversion and
false-input liveness, wide-counter live-outs, and 32-bit hardware-count
overflow/high-bit edges. The [local test evidence](../../tests/arithmetic-parity/results/compiler-hardening-2026-10-06.json)
uses the unchanged qualified compiler; no new speed or image-size result is
claimed for a test-only patch.

Use `--proposal-set mixed-mul` for the eighteenth patch, or `proposal-set:
mixed-mul` in the composite action. It replaces multiplication-based sign
correction in mixed signed/unsigned widening products with a mask and
subtraction. The gates are 114 Xtensa, 409 optimizer and five affected x86
overflow tests, all before Rust stage 1 is built. Previous selections are
unchanged. The optimization applies automatically when operand-width facts
and native high-multiply support permit it; there is no application switch.

Use `--proposal-set mul-width` to reproduce the nineteenth, inactive
runtime-width experiment. The gates are 115 Xtensa, 409 optimizer and the same
five x86 overflow tests before Rust stage 1. It adds an automatic signed-i64
small-operand branch only on supported S3 speed-optimized functions; size
optimization retains the existing lowering. Generate the independent
operand-width diagnostic with `tests/arithmetic-parity/mul_widths.py`, then
use the usual local build/flash/restore tools. Do not treat a fast-path win as
proof that wide or mixed inputs become faster.

Use `--proposal-set zero-compare` for proposal 0020, or `proposal-set:
zero-compare` in the composite action. This selection is the eighteen-patch
`mixed-mul` parent plus 0020, **not** the regressing 0019 runtime-width pass.
It uses existing S3 instruction-selection patterns to remove XOR-with-zero
from integer equality/inequality tests. It adds no application feature or
compiler IR pass. The gates remain 115 Xtensa, 409 optimizer and five x86
overflow tests before Rust stage 1. The installed SDK and default chain are
unchanged.

All are inactive, explicitly named evaluation selections. Choose a selection
with `--proposal-set`, not an application feature. The public applicator checks
the complete ordered chain against pinned originals; package provenance binds
the actually compiled LLVM tools, Rust driver and independent std snapshot.
See the [matched arithmetic results](../../tests/arithmetic-parity/RESULTS.md)
for device qualification and limits. Local incremental qualification is not a
fresh CI build, SDK installation or general firmware qualification.

Use `--proposal-set signed-overflow` for proposal 0021, or `proposal-set:
signed-overflow` in the same composite action. This is the `zero-compare`
parent plus a generic SelectionDAG alternative, not the 0019 runtime-width
pass. It requires 116 Xtensa, 409 optimizer and six x86 tests before rebuilding
Rust stage 1. Native half-width `CTLZ` availability chooses the lowering;
ordinary application `checked_mul`/`overflowing_mul` and std remain unchanged.
The [mathematical model](../../tests/arithmetic-parity/signbits_model.py)
independently checks signed-range and wrapped-result boundaries. Run it with
`python3 tests/arithmetic-parity/signbits_model.py`; this model is supplementary,
not device execution or performance qualification. Use both unchanged arithmetic
corpora and the same local build/measure/restore commands for device evaluation.

### Native sign extension and guarded wide casts

`--proposal-set native-sext` adds 0022 to `signed-overflow`: 21 patches,
117 Xtensa CodeGen/MC passes, 409 optimizer passes and six x86 regressions.
It tests both capability-enabled native SEXT and capability-disabled shift
expansion, including signed loads and divide/remainder consumers.

`--proposal-set guarded-casts` adds 0023: 22 patches, 118 Xtensa passes,
467 supported optimizer passes and seven x86 regressions. The extra optimizer
gate includes CodeGenPrepare; 104 unsupported tests and three existing
expected failures remain separate from the pass count. Both selections use
the same local builder and optional composite-action input, without changing
the default six-patch chain.

0023 guards an expanded scalar conversion libcall with range checks. Positive
thresholds round upward; NaN returns zero. Potentially poison inputs are
frozen before branching. Native/custom conversions, vectors, optsize and
minsize are excluded. The full-corpus and stratified finite/clamping device
checks described in the [arithmetic guide](../../tests/arithmetic-parity/README.md)
are required before making performance claims. Host execution is supplementary.

`--proposal-set fpclass-casts` adds 0024 (23 patches) with the same 592-pass
gate. It replaces the signed low-result NaN predicate with LLVM's existing
floating-class intrinsic. On ESP32-S3 this retains the native f32 unordered
comparison and avoids an extra software f64 comparison helper. It does not
change application casts or add a target-specific flag. Keep the preceding
`guarded-casts` control when evaluating this separate follow-up.

`--proposal-set soft-casts` adds 0025 (24 patches): a softened floating
operand also needs a conversion helper when its integer result is legal or
promoted. Query the helper at the promoted integer width, and reuse the same
guards. The gate is 119 Xtensa, 467 optimizer and seven x86 passes (593 total).
Use the additional small-width finite/clamping corpus against `fpclass-casts`.
This remains a profitability RFC: other targets' custom conversion hooks and
wider-only helper configurations need separate portability qualification.

`--proposal-set sign-mask` adds 0026 (25 patches): after DAG legalization,
reuse an already existing native byte/halfword extension to derive an i32
sign mask. Do not create a new extension for isolated masks. The gate is
120 Xtensa, 467 optimizer and seven x86 passes (594 total). Unsupported
cores retain shift expansion. All these selections exclude the regressing
0019 width branch and leave the installed compiler/default series unchanged.

`--proposal-set bit-branch` adds 0027–0028 (27 patches): use core BBCI/BBSI
for a branch-only single-bit test, with both polarities and long-range
relaxation handled. Discard an unused generic comparison simplification
before it interferes with target one-use checks; live shared expressions
remain intact. The gate is 122 Xtensa, 467 optimizer and 12 x86 passes
(601 total), with unsupported/expected failures recorded separately.
Evaluate against the frozen `sign-mask` package with identical C, std,
kernel and corpus inputs. No application feature or inline assembly is needed.

`--proposal-set zero-select` adds 0029 (28 patches), simplifying nested
zero-defaulting selects while freezing a newly unconditional predicate.
It preserves separately observed flags and nonzero fallback values. The
gate is 123 Xtensa, 467 optimizer and 18 x86 passes (608 total), retaining
all parent suites. The measured full-corpus Rust linker input is unchanged;
standalone value-only code gets smaller, not a new device-speed result.
Use the [host probe instructions](../../tests/arithmetic-parity/README.md)
for supplementary execution against the frozen parent. This remains opt-in.

`--proposal-set mul-range` adds 0030 (29 patches). For nonnegative operands
whose product is proven to fit the unsigned width, signed overflow is just
the product's sign bit. This preserves both returned values and excludes
legal integer types that may have native overflow instructions. Unknown
ranges and the rejected 0019 runtime shortcut are unchanged/excluded.
The gate is 124 Xtensa, 467 optimizer and 19 x86 passes (610 total).
The [matched Rust/C probe and native execution instructions](../../tests/arithmetic-parity/README.md)
separate function-size improvements from the unchanged full-corpus firmware.
It remains an opt-in profitability RFC, not SDK or CI activation.

### Optional std math RFC

The inverse-hyperbolic RFC is separate from the LLVM patches. Archive a copy
of the qualified library directory, then prepare another independent copy:

```sh
tar -cf "$STD_ARCHIVE" -C "$STD_SOURCE" .
python3 tools/apply-rust-std-proposals.py \
  --snapshot-archive "$STD_ARCHIVE" --output "$STD_RFC_PREPARATION" \
  --proposal nuttx-libm-inverse-hyperbolic-rfc \
  --source-revision abf50ae2e46066e67e29fe856f9764c23aa9a3ca
```

For that evaluation only, pass `--std-source "$STD_RFC_PREPARATION/snapshot"`
and `--std-provenance "$STD_RFC_PREPARATION/provenance.json"` to the same
compiler builder. The composite action accepts the equivalent optional
`std-provenance` input. The builder checks the public patch against its pinned
preimages, the actual patched files and the complete packaged inventory.
The ledger travels separately from LLVM provenance into arithmetic reports.

The RFC makes NuttX's `f32`/`f64` `asinh`, `acosh` and `atanh` use its C libm.
Other targets retain the original formulas. The selected NuttX libm must
provide all six symbols; math `errno` is outside the arithmetic contract.
The normal std preparation and installed SDK are unchanged. This remains
an optional evaluation RFC, not a general precision or portability guarantee.

The test gate uses the exact Xtensa CodeGen/MC test count in `upstream.json`,
or the named proposal manifest for an explicitly selected proposal set.
Passing it and producing this sysroot reproduce the compiler evaluation; they
do not qualify hardware-loop behavior for general firmware. Applications
continue to use the repository's existing toolchain unless a separate,
explicitly reviewed activation change is made.
