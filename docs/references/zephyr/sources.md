# Evidence and snapshot index

Reviewed **2026-09-30**. Only primary project documentation and repository sources were used. The `latest` documentation links in the [research note](README.md) are rolling pages, not a pinned release manual. The repository snapshots below were inspected independently; they are **not** a tested Zephyr/Rust release combination.

| Source | Inspected snapshot | Scope |
| --- | --- | --- |
| Zephyr | [`fa4f8fb0e470210aee0ae6fb069a281bc6ac887a`](https://github.com/zephyrproject-rtos/zephyr/tree/fa4f8fb0e470210aee0ae6fb069a281bc6ac887a) | Accelerometer sample: public device/kernel APIs, devicetree alias selection, readiness and sensor calls. |
| Official Rust module | [`b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b`](https://github.com/zephyrproject-rtos/zephyr-lang-rust/tree/b7c19a642f2a433726cf0ea2c4ec2f205c6cee7b) | Documented no_std/alloc integration; not an audit of all possible Rust ports. |
| nxrs main sample | [`bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943`](https://github.com/yongkyuns/EmbeddedRust/tree/bb3f86a6ac78dfb42e256d3220cfbdcfd4af5943) | HAL architecture and README execution/build/qualification boundaries. |
| nxrs concurrency discussion | [`7a98e1862fee4a286c4d60869803ef51a67c75bb`](https://github.com/yongkyuns/EmbeddedRust/blob/7a98e1862fee4a286c4d60869803ef51a67c75bb/docs/concurrency-event-communication.md) | PR #7 was draft/unmerged when reviewed. The document supersedes the earlier single-physical-inbox default with isolated queues and one logical selection point. |

## Evidence map

The note links sources next to the claims they support. Its main distinctions rest on:

- **OS/API boundary:** Zephyr's POSIX implementation and overview, device model, system-call documentation, and the pinned accelerometer sample. A public OS-specific API is not necessarily a private-kernel dependency.
- **Hardware and build boundary:** devicetree HOWTOs, devicetree-versus-Kconfig and CMake build documentation. A hardware node, compiled driver, device pointer and initialized device are distinct states.
- **Acquisition and concurrency semantics:** Fetch/Get, Read/Decode, message-queue and polling documentation. Ready does not mean acquired; a bounded byte queue does not establish Rust ownership or scheduler guarantees.
- **Portability limits:** official Rust integration/allocator documentation and native_sim/peripheral-emulation documentation. Rust syntax, host execution and whole-application cross-OS portability are different claims.
- **nxrs direction:** the pinned normative HAL architecture and README plus the separately labeled, unmerged concurrency proposal. Proposed Zephyr providers, execution adapters and conformance work are recommendations, not existing support.

## Validation boundary

This is a targeted architecture review, not a whole-repository dependency audit, kernel benchmark or target port. No Zephyr firmware, nxrs runtime, physical-device or browser test was executed for this reference. Diagram rendering and document-link/layout checks validate the documentation only. Source examples are discussed, not represented as new executable fixtures.

All new reference material, diagram sources, the local Material-style palette and reproduction instructions stay under `docs/references/zephyr/`. No references are inserted into nxrs's core architecture documents, and no OpenVela files are changed.
