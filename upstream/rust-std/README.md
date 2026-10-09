# Optional NuttX std math RFC

This is an **inactive evaluation patch**, not a replacement for the qualified
std preparation or an installed SDK. The
[proposal manifest](proposals/series.json) pins the Rust revision, three source
preimages and patch bytes. The change belongs in std, not an application
wrapper or Cargo feature.

For `target_os = "nuttx"`, `f32`/`f64` `asinh`, `acosh` and `atanh` call the
corresponding C libm routines. Other targets retain their original formulas.
NuttX's selected libm must provide all six symbols. This makes C and Rust use
the same math implementation; it does not promise correctly rounded results
for every input, stable NaN payloads, or identical `errno` behavior.

The [matched device report](../../tests/arithmetic-parity/results/esp32s3-std-math-2026-10-06.json)
contains three full ESP32-S3 runs, with unchanged compiler, kernel, inputs and
C flags. All six changed math cases agree bit-for-bit and stay within 1 ULP
of their oracle in that corpus. There are no arithmetic errors. The remaining
six raw differences across all three runs are signed-zero choices in
`f32::min/max`, which the suite's contract permits.

The new wrappers are 49 B (`f32`) and 51 B (`f64`), versus 99–184 B for the
old formulas. These are symbol sizes, not full dependency or firmware deltas.
`f64::asinh` becomes faster, but some old formulas were faster than the chosen
libm implementation. Consistent implementation and fewer wrapper instructions
do not mean every math function is faster.

Use the [same local/CI preparation](../rust-llvm/BUILDING.md#optional-std-math-rfc)
to archive an independent qualified std snapshot, prepare a fresh patched
copy and pass its provenance to the compiler builder. The applicator rejects
wrong pins, unsafe archives and reapplication; the builder reverses the
public patch to verify its original preimages, then checks the complete
packaged source inventory. The std ledger is recorded separately from LLVM
in compiler, arithmetic build and public measurement provenance.

No normal build, SDK or CI job is activated by adding this RFC.
