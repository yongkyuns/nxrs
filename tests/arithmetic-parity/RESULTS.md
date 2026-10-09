# ESP32-S3 arithmetic assessment

The frozen sixteen-patch full-corpus run closes the measured 64-bit recurrence
gap: its 64-step Rust loop takes **5.20762 µs/input**, versus **5.70573 µs** for
matched C, down from **12.80488 µs** in the earlier
[seven-patch baseline](results/esp32s3-2026-10-06.json). This is not general
speed, size or deadline parity. The [device report](results/esp32s3-constant-hwloop-math-2026-10-06.json)
retains the three runs, paired timing samples, compiler and std identities,
and flash-restoration evidence.

## Supported measurements

The sixteen-patch report covers **1,136 cases × 64 vectors**, with 942 matched
C/Rust cases and 194 independently checked Rust-only 128-bit cases. Across
three device runs it records 398,976 oracle checks, no arithmetic errors and
six raw floating-point differences: signed-zero choices in `f32::min/max`
allowed by the contract. Each mode retains five timing repeats per case and
run. The complete diagnostic includes both implementations and about 5 MiB
of vectors; its size is not a production image comparison. See the
[qualification guide](README.md) for semantics and reproduction.

The sixteen-patch compiler changes combine native rotate lowering, eligible
hardware loops, correct loop metadata and instruction scheduling, then improve
carry/compare lowering, integer and floating selects, paired wide branches and
fitting wide constant loop counts. A separate optional std RFC routes NuttX
inverse-hyperbolic calls through its C libm. The optional comparison uses the
[conditional-move control](results/esp32s3-conditional-move-2026-10-06.json)
and [std-math candidate](results/esp32s3-std-math-2026-10-06.json); six changed
cases stay within 1 ULP, though some original formulas were faster.
The tests-only [hardening record](results/compiler-hardening-2026-10-06.json)
does not change the sixteen-patch firmware result.

## Checked multiply and later compiler candidates

The full-corpus checked `i64` multiply remains more expensive than C. In the
sixteen-patch result it takes **3,954 versus 2,637 cycles per 64 inputs**.
The 27-patch `bit-branch` device candidate, including the intervening sign-bit
lowering, reduces Rust to **3,223 cycles**, still about **22% above C**. It
also reduces the checked-multiply multiply count from eight to four in the
paired sign-bit experiment; instruction count alone does not predict time.
The [27-patch device report](results/esp32s3-bit-branch-math-2026-10-07.json)
passes the same three-run corpus with zero arithmetic errors and verified
restoration. The six permitted signed-zero differences remain.

The signed-overflow alternative avoids a runtime width guard. Its
[width-stratified control](results/esp32s3-zero-compare-widths-2026-10-06.json)
and [candidate](results/esp32s3-signbits-widths-2026-10-06.json) show about
9.6% lower Rust time in all eight groups, yet C remains faster in five; when
both operands fit signed 32 bits Rust is still 1.85× C. These are stratified
diagnostic distributions, not workload-frequency estimates.

Proposal 0029 simplifies the value-only form `checked_mul(...).unwrap_or(0)`;
it preserves separately observed overflow flags and has no new device result.
Proposal 0030 simplifies checked multiplication only when range analysis proves
nonnegative operands and an unsigned-fitting product. Its ordinary Rust/C
probes shrink from 59 to 27 B and 57 to 25 B, while an unrestricted signed
64-bit probe is unchanged. The [compiler record](results/compiler-mul-range-2026-10-07.json)
reports 610 supported compiler tests and 172,522 native product checks per
compiler with no mismatches. Crucially, its full-corpus IR, backend object and
Rust link input are byte-identical to the 28-patch parent: **0030 has no new
full-corpus device, image-size or speed result**. It does not close the
roughly 22% checked-multiply gap for arbitrary full-width operands.

An earlier [runtime-width shortcut](results/esp32s3-mul-width-candidate-2026-10-06.json)
improved one narrow-input group but regressed the others and grew code. It
remains inactive and excluded from the frozen selection. The direct bit-branch
change has mixed per-case timing results: smaller code is not universal speed
improvement. Focused compiler and width records retain those regression proofs.

## Frozen status and qualification boundary

The current arithmetic evaluation is frozen at the 29-patch `mul-range`
candidate. It is an inactive RFC; the **default remains the six-patch compiler
selection**, and no installed SDK or normal build is changed. Reopen compiler
work only for a correctness failure or a material bottleneck found in a real
application.

A separate compiler timing probe found that GNU assembly can conceal a
native-object hardware-loop alignment penalty. The retained uninstrumented
alignment service run observed longest handlers of 11.311/11.762 ms and four-
run deadline misses of 1/16. Equal arithmetic throughput does not imply equal
service response; see the retained
[native-driver report](../event-services-comparison/results/esp32s3-compiler-aligned-2026-10-05.json).

The broader suite does not cover every API, input, target or optimization
mode, unsafe or panic paths, SIMD/DSP, atomics, or floating-point context
preservation across preempting threads. Device medians are observations, not
bounds. The [build guide](../../upstream/rust-llvm/BUILDING.md) keeps all
selections opt-in and records compiler, patch and std provenance separately.
