# Arithmetic compiler evaluation freeze

The 29-patch `mul-range` evaluation is frozen as an inactive compiler RFC.
Its tested arithmetic is correct within the suite's stated contracts. The
rebuilt driver's full 1,136-case IR, backend object and complete Rust input ELF
are byte-for-byte unchanged from the 28-patch parent, so this follow-up adds no
firmware, image-size or device-speed result. Static standalone function sizes
change from 59 to 27 B and 57 to 25 B for the proven-range probes; these are
not device-speed claims. The roughly 22% full-width checked-multiply timing
gap and the broader application, target and workload qualification limits
remain.

The older immutable sixteen-patch full-device result remains useful baseline
evidence: its 64-step multiply recurrence measured 5.21 µs for Rust versus
5.71 µs for C. It does not qualify this later proposal or establish general
application parity. There is no default SDK or CI activation. Stop compiler
dependency changes here; reopen only if profiling a real application shows a
material bottleneck or a correctness failure.

Reproduction and handoff:

- [Public patch builder and qualification instructions](../../upstream/rust-llvm/BUILDING.md)
- [Latest compiler report](results/compiler-mul-range-2026-10-07.json)
- [Real-service qualification](../service-qualification/README.md)
