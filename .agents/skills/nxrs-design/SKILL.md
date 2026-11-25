---
name: nxrs-design
description: Design or compare consequential NXRS APIs, capability boundaries, device composition, build interfaces, concurrency, and lifecycle changes. Use when ownership, invariants, errors, ordering, or resource costs need a decision; avoid expanding a small fix into a redesign.
---

# NXRS design decisions

Make the public contract and operating costs concrete before committing to a design.
Read [AGENTS.md](../../../AGENTS.md), the current implementation and callers, and the
existing document that owns the decision. Continue routine work without repeated
approval requests; ask only when an unresolved choice materially changes the result.

## Establish the contract

1. State the problem in terms of an observable failure, missing capability, or
   repeated maintenance cost. Identify the callers, supported targets, current
   behavior, and what success must preserve. Keep non-goals explicit and short.
2. Collect the existing invariants and vocabulary. Separate measured facts,
   established requirements, assumptions, and still-open decisions. Use source and
   existing evidence rather than treating an attractive abstraction as a requirement.
3. Write a small caller example and failure example before choosing internals.
   Specify values/types, ownership, lifetimes, initialization, error semantics,
   ordering, cancellation, retries, timeouts, and compatibility where relevant.
4. Make resource obligations explicit: physical endpoints, buffers, allocations,
   queues/capacity, workers, stack, execution priority, deadlines, and cleanup.
   Distinguish a budget from a measurement and a capability from its qualification.

## Explore concrete alternatives

Compare two or three meaningfully different designs when the tradeoff is real.
Include extending the existing design; do not create artificial alternatives for an
obvious local change. For each option show the same normal caller, failure path,
and teardown sequence, with enough types or pseudocode to expose the differences.

| Compare | Questions to resolve |
| --- | --- |
| Contract | Which states and invalid operations can callers express? |
| Ownership | Who holds each resource, and when can it be reused? |
| Errors/order | What is accepted, completed, lost, cancelled, or retried? |
| Cost | Which memory, workers, wakeups, copies, or waits does it add? |
| Portability | Which logic is shared, and which boundary is platform-specific? |
| Change | What migrates, what stays compatible, and how is failure diagnosed? |
| Evidence | Which independent checks can distinguish correct behavior? |

Choose based on these consequences and present the decisive tradeoff plainly.
A useful boundary can have one implementation when ownership or ABI separation
justifies it. Multiple implementations alone do not justify a generic framework.
Do not introduce an executor, registry, universal driver layer, or dynamic dispatch
without a concrete need and a qualified cost model.

## Apply NXRS's established decisions

- Use [HAL architecture](../../../docs/hal-platform-architecture.md) and
  [NuttX access](../../../docs/nuttx-device-access.md): capability APIs remain typed
  and portable; ordinary OS work uses `std`; reusable chip protocols live in drivers.
  Rust owns behavior and lifetime. C/Objective-C translates target ABI or required
  SDK operations, not a parallel state machine or cross-platform HAL.
- Follow [static device composition](../../../docs/static-device-composition.md)
  and [peripheral composition](../../../docs/peripheral-composition.md). Profiles
  bind physical resources; catalogues import driver declarations through Cargo;
  features are additive. One permanent physical instance may have aliases, while
  acquisition has an exclusive session. Do not recreate ownership per alias.
- Follow [build system](../../../docs/build-system.md): shared host preparation owns
  validated generated inputs; Cargo adapters consume that handoff; native profile
  builds use `cargo nxrs build`. Preserve target/configuration identity, freshness,
  effective Kconfig checks, and detached-consumer behavior. Native board code owns
  electrical setup; do not add a competing pin catalogue or guessed registrations.
- Follow [concurrency](../../../docs/concurrency-event-communication.md): bounded
  typed delivery, explicit overload/loss, terminal state independent of queue
  admission, cancellation that wakes waits, and quiescent teardown. Distinguish
  accepted, ready, completed, stopped, and joined. Keep ISR work to qualified
  notification and protect fixed-priority execution from hidden blocking/spinning.
- Read subsystem-specific decisions when relevant, including
  [camera clients](../../../docs/client-camera-architecture.md) and
  [security](../../../docs/security.md). Do not extrapolate an unqualified provider
  or physical capability from a passing portable build.

## Preserve and implement the decision

Update the existing owning architecture/design page with the selected contract,
why it was chosen, meaningful rejected alternatives, compatibility/migration, and
remaining qualification. Use the source map in [docs/README.md](../../../docs/README.md).
Keep historical plans clearly historical; do not create a competing architecture
page or a separate decision ledger for a choice already owned elsewhere. Put terms
and rationale beside the contract so later changes can find them.

Define acceptance evidence proportionate to the change: independent public-contract
fixtures, negative configuration controls, partial-start/overflow/cancel/teardown
cases, target-header checks, and physical qualification as applicable. Derive actual
commands from current workflows and fixture guides. Tests must observe behavior,
not merely mirror the chosen implementation. Keep host and device claims distinct.

Implement the agreed scope when requested, including affected callers and existing
documentation. Report the chosen behavior, tradeoff, checks, and unresolved evidence.
Do not turn ordinary implementation choices into repeated user approval gates.

Adapted principles and upstream license: [attribution](../ATTRIBUTION.md).
