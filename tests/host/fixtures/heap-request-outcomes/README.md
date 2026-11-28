# Native allocator-call outcome witness

The six files under `upstream/` are verbatim Apache NuttX sources from the
revision and SHA-256 inventory in `sources.json`. Original license headers,
the Apache-2.0 license and NuttX notice are retained here.

`tests/host/test_heap_request_outcomes.py` applies the registered production
patch to these files and compiles the real malloc, realloc, memalign, free and
shrink algorithms with narrow host stubs for native locks, notes and KASAN.
The tests observe allocator return values, original payload preservation,
request counts and raw emitted endpoints. They also exercise runtime filtering,
boot-global identities across heap reuse, concurrent callers and saturation.

These host tests qualify the call/return instrumentation and its disabled
build path. They do not establish NuttX target-header ABI, native timing,
interrupt/SMP execution costs, complete allocator topology or application
allocation lifetimes. The independent QEMU witness covers the native ABI.
