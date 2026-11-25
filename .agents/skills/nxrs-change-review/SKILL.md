---
name: nxrs-change-review
description: Review an NXRS pull request, commit, branch, or local changes for requirements, correctness, portability, resource and lifecycle safety, and qualification gaps. Use before delivery or when asked to review; it does not authorize merging or rewriting the change.
---

# NXRS change review

Inspect the requested changes against their contract and NXRS architecture. Read
[AGENTS.md](../../../AGENTS.md) and the affected subsystem's current documentation.
Review does not by itself authorize code edits, commits, pushes, or merges.

## Fix the review scope

1. Start with `git status --short` and identify the requested scope from the task or
   PR. Record the actual target branch, base SHA, head SHA, and merge-base where
   relevant. Do not assume every PR targets `main` or that local refs are current.
2. For a committed branch or PR, review the diff from its merge-base to the exact
   head. For one commit, compare its specified parent, considering merge parents.
   Inspect the changed-file manifest, deletions, renames, submodule changes, and
   binary assets as well as text patches. Paginate remote file lists when necessary.
3. For local work, include staged, unstaged, and untracked files. These read-only
   commands reveal different parts of the scope:

   ```sh
   git diff --cached --name-status
   git diff --name-status
   git ls-files --others --exclude-standard -z
   ```

   Read the corresponding patches and untracked contents. For a complete proposed
   branch including working changes, compare the final tracked working tree against
   the pinned merge-base with `git diff <merge-base> --`, then add untracked files.
   A staged-only review must read index contents; a working-tree file can differ.
   `git diff <base>...HEAD` alone misses every uncommitted change. Do not stage files,
   reset changes, or rewrite commits merely to expose the review scope.
4. Inspect binary changes with suitable metadata/rendering and generated changes
   against their source. State inaccessible or deliberately excluded files. Record
   the reviewed state; if head, base, index, or worktree changes, reassess affected
   findings and checks before declaring that newer state reviewed.

## Assess requirements separately from implementation

- Recover acceptance criteria from the user request, issue, existing public API,
  and fixtures. Distinguish required behavior, non-goals, and inferred assumptions.
- Walk a normal caller and relevant failures through the change. Check observable
  values, errors, ordering, compatibility, and resource behavior. A green test is
  supporting evidence, not proof that its expectations match the requirement.
- Look for independent oracles and missing cases. Include invalid configuration,
  unavailable resources, partial starts, overflow, disconnect, cancel, restart,
  and teardown where the change makes these possible. Avoid tests that only mirror
  implementation details or mocks that bypass the affected contract.

## Assess architecture and operational correctness

Load only the relevant sources: [HAL architecture](../../../docs/hal-platform-architecture.md),
[NuttX access](../../../docs/nuttx-device-access.md),
[static devices](../../../docs/static-device-composition.md),
[build system](../../../docs/build-system.md), and
[concurrency](../../../docs/concurrency-event-communication.md).

- Preserve typed portable app/service APIs and direct `std` for ordinary OS work.
  Keep OS selection private to the owning provider, reusable chip protocols in
  drivers, Rust lifecycle/state policy in Rust, and native code as thin ABI glue.
- Check one physical instance per endpoint, aliases sharing that instance, exclusive
  sessions, resource ownership, unsafe invariants, callback lifetime, and quiescent
  teardown. No resource may be freed while a worker or callback can still use it.
- Check bounded event admission, explicit loss policy, sticky terminal faults,
  wakeable cancellation, stale-session rejection, and join dependencies. ISR work
  must remain qualified bounded notification. Nonblocking handoff must not spin
  behind a preempted lower-priority producer under fixed-priority scheduling.
- Trace profile/resource ownership through BSP mapping, effective native config,
  generated-input identity, and consumed helpers. Additive driver features and
  detached consumers must work without hardware selection in app/service code.
  A dry run does not prove native configuration or target ABI compatibility.
- Inspect documentation against changed behavior. Use existing architecture pages;
  avoid introducing a second conflicting design narrative. For camera, detector,
  radio, or security changes, read that subsystem's own contracts before judging.
- Evaluate performance claims against the actual workload, clocks, resource budget,
  memory placement, and achieved rates. Keep host tests, emulator results, and
  physical qualification separate; identify any device gate still open.

## Verify and report

Choose checks from the current workflow and fixture guide rather than inventing
commands. `python3 tools/check-architecture.py` checks dependency boundaries;
`cargo test --locked` can exercise portable contracts; focused package tests often
answer a narrower question sooner. NuttX profile builds go through `cargo nxrs build`.
Run required gates and checks that resolve concrete risks; do not modify the code or
hardware just to produce a review without appropriate task authorization.

Report actionable findings by severity, with a file/location, triggering condition,
observable consequence, supporting evidence, and a focused remedy. Separate defects
from optional preferences and unanswered questions. Do not inflate stylistic taste
into a correctness issue. If no defect is found, state the reviewed scope and limits,
not a guarantee. Include tests run, unavailable evidence, and unresolved risks.

If merging is already authorized, re-fetch the actual PR head and target state and
confirm required checks and review disposition for those exact revisions. Explain
whether evidence covers the head alone or its integration with the current base.
A moving base, stale result, pending check, or unresolved required gate needs renewed
assessment. Review invocation and green CI alone never grant merge authorization.

Adapted principles and upstream license: [attribution](../ATTRIBUTION.md).
