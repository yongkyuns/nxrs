---
name: nxrs-debug
description: Investigate and fix NXRS bugs, regressions, failing tests, build or profile failures, acquisition faults, and timing or memory problems using symptom-specific reproduction and qualification evidence.
---

# NXRS debugging

Find the first violated contract, demonstrate its cause, and verify a focused fix.
Read [AGENTS.md](../../../AGENTS.md) before changing code. Paths in commands are
relative to the repository root; derive task-specific arguments from the checkout.

## Establish the failure

1. Record expected and observed behavior, the actual failing command or workload,
   revision and local changes, toolchain, app, profile, target, and relevant hardware.
   Preserve the first useful error and its context; a downstream failure may hide it.
2. Read the owning implementation, nearby tests, and its existing design document.
   For HAL/provider work use [HAL architecture](../../../docs/hal-platform-architecture.md)
   and [NuttX device access](../../../docs/nuttx-device-access.md). For profiles and
   generated inputs use [build system](../../../docs/build-system.md) and
   [static devices](../../../docs/static-device-composition.md). For delivery and
   shutdown use [concurrency](../../../docs/concurrency-event-communication.md).
3. Reproduce the reported symptom with the smallest workload that retains its cause.
   Choose evidence suited to the problem: a public-API test, failing build, recorded
   trace, target-header ABI check, repeated workload, or physical-device observation.
   Compilation alone cannot reproduce a runtime bug; a host fake cannot establish
   interrupt timing, DMA behavior, electrical wiring, or actual device readiness.
4. Define an independent oracle before editing: a public contract, known fixture,
   protocol specification, independently calculated value, or observed device event.
   Avoid expectations copied from the implementation or assertions that merely
   repeat its branching. If hardware is unavailable, isolate what host evidence can
   establish and state the remaining device gate without claiming the symptom fixed.

## Isolate the cause

- Keep a short set of plausible causes and one observation that would distinguish
  each. Trace the first bad input, state transition, ownership transfer, or output.
- Change one causal variable at a time. Minimize inputs, concurrency, and enabled
  components only while preserving the symptom. Record intermittent failure counts,
  sample duration, and load; do not impose a universal seconds-only reproducer.
- Instrument the owning layer with bounded, relevant diagnostics. Consider whether
  logging changes timing or allocation. Reuse existing probes and fixture controls.
- Separate environment/setup failure from product failure. Check pinned dependencies,
  native headers, target identity, generated-input freshness, and first-error output
  before adding retries, weakening validation, or changing unrelated production code.

## Preserve NXRS boundaries

- Use qualified Rust `std` directly for ordinary OS facilities. Keep reusable chip
  protocols in `driver/`, capability policy in its HAL, and OS code in that provider.
  Rust owns validation, state, queues, synchronization, cancellation, and recovery;
  native bindings translate target ABI and required SDK calls mechanically.
- Respect one canonical physical device with exclusive acquisition sessions. Check
  partial-start rollback, sticky fault state, wake-on-cancel, callback quiescence,
  worker joins, and resource release order. Aliases must not create second owners.
- Keep ISR work bounded to qualified notification. No allocation, blocking channel
  sends, or service processing in ISR context. Queue overflow, stale-session data,
  terminal-fault visibility, and teardown under a full queue need explicit behavior.
- Use `cargo nxrs build` for NuttX profile builds; ordinary portable tests and explicit
  host fixtures can use Cargo directly. Follow the selected profile and BSP ownership
  rather than bypassing checks with hand-edited generated headers or receipts.
- Distinguish requested configuration, resolved sources, and effective native config.
  `--dry-run` is planning evidence only. A configuration save whose sync failed has
  already changed sources; diagnose that state before retrying. Follow the documented
  recovery path for interrupted publication instead of deleting guards or caches.

## Fix and qualify

1. Make the smallest change that restores the contract across affected callers.
   Keep useful regression tests. Replace a misleading test only with equivalent or
   stronger coverage; do not delete tests because a refactor made them fail.
2. Where feasible, show that the new regression evidence fails on the old behavior
   and passes after the fix. Exercise the original scenario again and a relevant
   failure/cancellation boundary. For nonrepeatable hardware failures, report the
   exact observation window and uncertainty rather than manufacturing certainty.
3. Select checks from current workflows and the owning fixture guide. Examples:

   ```sh
   python3 tools/check-architecture.py
   python3 -m unittest discover -s tests/host -p test_hal_boundaries.py -v
   cargo test --locked -p nxrs-build-support -p nxrs-build
   ```

   The build-package tests are relevant to build changes, not every bug. Use the
   affected crate's real manifest/features for runtime tests; expand only for a
   concrete remaining risk or a repository-required gate.
4. For timing/memory claims, hold workload and platform settings constant. Report
   actual cadence, loss, CPU versus wall time, allocations, stack reservation versus
   measured use, clocks, and relevant memory/cache placement. Requested budgets are
   not achieved throughput. Keep host, emulated, and physical evidence distinct.
5. Recheck the diff for accidental profile changes, debug leftovers, and displaced
   documentation. Update the existing contract or troubleshooting page when needed.

## Report

Lead with the cause and changed behavior. Include the reproducer, independent oracle,
exact checks and outcomes, relevant revision/profile, and anything still unqualified.
Separate a proven fix from a plausible mitigation or an unavailable hardware check.

Adapted principles and upstream license: [attribution](../ATTRIBUTION.md).
