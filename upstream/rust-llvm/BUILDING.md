# Private LLVM evaluation build

This opt-in builder creates a Rust stage 1 compiler package in a new private
output directory. It does not install a toolchain, modify an SDK or change the
compiler used by normal application builds. Keep source trees, std snapshots,
ledgers and outputs separate from installed SDK directories.

## Prepare pinned inputs

Use clean Git checkouts at the revisions in [`upstream.json`](upstream.json),
including Rust's recursive submodules, and an independent copy of the already
qualified NuttX Rust library source. The builder treats all three as
read-only. For example:

```sh
git clone https://github.com/espressif/llvm-project "$LLVM_SOURCE"
git -C "$LLVM_SOURCE" checkout --detach 14b9f5575f37489d4c25c069d04c9ed216dadc31
git clone https://github.com/esp-rs/rust "$RUST_SOURCE"
git -C "$RUST_SOURCE" checkout --detach abf50ae2e46066e67e29fe856f9764c23aa9a3ca
git -C "$RUST_SOURCE" submodule update --init --recursive
```

Prepare the std input using the repository's checksum-pinned generator and an
original `esp-1.90.0.0` SDK source:

```sh
python3 tests/nuttx-std/prepare-std.py \
  --source "$ORIGINAL_ESP_SDK" --sdk esp-1.90.0.0 \
  --output "$FRESH_STD_PREPARATION"
export STD_SOURCE="$FRESH_STD_PREPARATION/toolchain/lib/rustlib/src/rust/library"
```

Keep `std-patch.json` and the generated patch files with the compiler package.
The generator checks exact source blobs; an arbitrary or unpatched `library`
directory is not an equivalent input. See [NuttX std prerequisites](../../docs/nuttx-std.md).

## Plan and build

Use Linux x86_64 with Git, Python 3.12+, CMake, Ninja, a C/C++ toolchain and
enough disk for LLVM and Rust. Choose a new output path for each build. A plan
checks pins and prints the work without creating output; then run the same
arguments without `--plan`:

```sh
python3 tools/build-rust-llvm.py \
  --llvm-source "$LLVM_SOURCE" \
  --rust-source "$RUST_SOURCE" \
  --std-source "$STD_SOURCE" \
  --out "$EVALUATION_OUT" --jobs 1 --plan

python3 tools/build-rust-llvm.py \
  --llvm-source "$LLVM_SOURCE" \
  --rust-source "$RUST_SOURCE" \
  --std-source "$STD_SOURCE" \
  --out "$EVALUATION_OUT" --jobs 1
```

The default selection applies the six patches registered by the applicator.
An additional named selection is opt-in via `--proposal-set`, for example
`alignment`, `constant-hwloop`, `sign-mask`, `bit-branch` or `mul-range`.
Use a name present in [`proposals/series.json`](proposals/series.json); the
builder rejects arbitrary patch paths and records the complete ordered chain.
Proposal selection does not trigger a CI build or activate a toolchain.

The builder exports the pinned source inputs into the output, applies the
registered LLVM patches, builds LLVM with X86 and Xtensa, assertions and
static libraries enabled, then runs the selected Xtensa CodeGen and MC gates
before Rust stage 1. Proposal sets can add their declared optimizer or x86
tests. It packages the compiler and matching host libraries with a separate
copy of the supplied std source; copied source links back to the Rust checkout
are detached. The result is a compiler evaluation sysroot, not a complete Rust
SDK: Cargo, rustdoc and a bundled target linker are not included.

The output records pins, patch ledger, commands, actual `rustc -vV`, compiler
and `librustc_driver` hashes, LLVM tool hashes and the complete std inventory
in `build-provenance.json`. Preserve it with `llvm-patches.json` and any
application measurement. An archive-built rustc may report an unknown commit;
the pinned source revision in provenance remains authoritative. A patched
source checkout alone does not identify the compiler used by an application.

## Optional std math RFC

The inverse-hyperbolic change is a separate Rust library patch. To prepare an
independent RFC snapshot, archive the qualified library tree and apply the
named proposal to the archive:

```sh
tar -cf "$STD_ARCHIVE" -C "$STD_SOURCE" .
python3 tools/apply-rust-std-proposals.py \
  --snapshot-archive "$STD_ARCHIVE" --output "$STD_RFC_PREPARATION" \
  --proposal nuttx-libm-inverse-hyperbolic-rfc \
  --source-revision abf50ae2e46066e67e29fe856f9764c23aa9a3ca
```

For that build only, pass `--std-source "$STD_RFC_PREPARATION/snapshot"`
and `--std-provenance "$STD_RFC_PREPARATION/provenance.json"`. LLVM and std
ledgers remain separate. The RFC requires the selected NuttX libm to provide
`asinh`, `acosh` and `atanh` in both float widths; other targets retain their
existing formulas. Details and device limits are in the
[std RFC notes](../rust-std/README.md).

The composite action in this directory accepts the same pinned source paths,
output, job count and optional proposal/std inputs. Normal portable CI runs
the applicator and setup guards only; it does not build or install this
compiler. No self-hosted runner, event trigger or SDK is changed here.
