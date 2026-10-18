# NuttX resource-failure qualification

This separate test binary uses ordinary Rust `main()` and std. It is not a
production app, browser test, framework, or default memory/stack recommendation.
The shared browser/thread probe, both SDK patches, root toolchain and external
revisions are unchanged. Each invocation uses a fresh single-CPU RV32 kernel.
The build asserts the pinned 32-MiB RAM configuration; QEMU supplies 128 MiB.

## Required observations

- Two fresh resource-test boots. Each executes two bounded heap-pressure cycles:
  acquire/touch/check one-MiB buffers until a fallible allocation is rejected,
  release the held buffers, and successfully allocate/check a fresh buffer.
  Stack bookkeeping is fixed at 160 entries. Exceeding that limit is failure,
  not a reason to consume more memory. No formatting or infallible allocations
  are requested while pressure is held. These legal `Vec<u8>` layouts distinguish
  allocator rejection from capacity arithmetic overflow.
- Three legal 256-MiB thread-stack requests per resource boot must be rejected
  as OutOfMemory without executing their closures. A 64-KiB thread, bounded
  channel transfer and join must work after each rejection.
- Eight fresh boots cover every closed subset of descriptors 0, 1 and 2,
  including a no-close baseline. A test-only C fixture closes/verifies the
  requested descriptors BEFORE rustc's generated C-ABI main/std initialization.
  Rust verifies that a new open cannot steal a standard descriptor, closed stdin
  reads EOF, and repaired stdout/stderr accept writes. A separate console handle
  reports observations only AFTER those checks. The fixture verifies descriptor
  restoration after Rust returns, then restores its console for task teardown.
- One deliberate missing-close setup must be rejected BEFORE Rust entry. This
  guards against recording a clean start as successful closed-descriptor testing.
- All positive cases require exact reports, successful NSH return, and no panic;
  reports followed by a crash are failures. Existing external deadlines apply.

The pre-start C fixture is test fault injection, not an app launcher or a new
entry contract. It calls the compiler-generated Rust startup once; it never
calls the private handwritten Rust function or manually initializes std.

## Run

Use the prerequisites and explicit SDK-fix setting in `docs/nuttx-std.md`:

```sh
python3 tests/nuttx-std/run-resources.py --self-test
NUTTX_STD_COMPAT_FIXES=1 bash tests/nuttx-std/build.sh resources
python3 tests/nuttx-std/run-resources.py --image target/nuttx-resources/nuttx/nuttx
```

The original `build.sh` invocation still builds the shared threading probe.
The resource profile has separate temporary sources, SDK copy, Cargo artifacts,
ABI evidence, kernel and transcripts under `target/nuttx-resources`.
CI retains these as `nuttx-resource-qualification`. Runtime success must identify
an exact commit/run; adding these tests or compiling them is not qualification.

This is bounded finite-heap pressure and oversized-stack rejection, not arbitrary
heap fragmentation, leak freedom, total process-slot exhaustion, infallible-OOM
abort behavior, browser resource-failure coverage, or physical-board timing.
Rust `poll` is enabled only when its poll-specific observations among the 77
match the exact Cargo libc artifact; older SDKs without the fix do not qualify
poll. `ppoll` remains forbidden because its signal ABI is unqualified. Raw
`SIG_IGN` is still mismatched and contained by the std startup correction. The
`fcntl` descriptor-sanitization fallback remains in use; this remains
patched-SDK qualification.
