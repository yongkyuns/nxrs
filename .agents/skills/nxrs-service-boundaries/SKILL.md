---
name: nxrs-service-boundaries
description: Design or review NXRS service ownership, dependency boundaries, and firmware footprint during service additions and refactors.
---

# NXRS service boundaries

Read the [architecture rules](../../../docs/architecture.md#service-boundaries-and-footprint)
and trace the existing owners, lifecycle and real product callers. Use
`cargo tree` with the affected app's feature set to identify newly reachable
packages. Look at shared behavior and resource ownership before deciding
whether another type, crate or worker is needed.

- Keep one owner for each lifecycle and policy. Reuse identical operations
  through the owning crate. Keep distinct protocols and scheduling policies
  explicit. Use a portable HAL API or Rust `std` directly for clear app-owned
  operations. Keep device waits, retries, acquisition and cleanup in the HAL.
  Keep single-product behavior in app modules and extract services for
  independently reusable policy or protocol. Keep a service's stable protocol
  with its lifecycle and policy when they describe the same capability. Peers
  can use an optional protocol module without starting the service or its HAL.
- Put reusable native capability adapters in their HAL and fixture-only adapters
  in `tests/`. Leave the root `platform/` tree to profiles, BSP inputs and
  composition. Keep optional diagnostic code behind the existing `probe` feature.
- Reuse the owner's wait point and bounded storage where possible. Explain
  each new task, queue, buffer, allocation and dependency, including its
  lifecycle, failure behavior and memory cost.
- Run `python3 tools/check-architecture.py` and relevant failure controls. For
  embedded paths with changed code or resource costs, compare the same
  `cargo nxrs build` profile and toolchain. Report linked flash, unpadded
  image and static RAM separately from runtime heap and stack. Reuse the
  existing tracker footprint check or extend the nearest existing target
  build when a different product needs coverage.
