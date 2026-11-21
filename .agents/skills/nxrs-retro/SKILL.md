---
name: nxrs-retro
description: Review an NXRS engineering session or repeated workflow problem and improve the specific tooling, instructions, tests, or documentation responsible. Use for requested retrospectives or observed recurring friction, not speculative process rewrites after every task.
---

# NXRS workflow retrospective

Turn observed friction into a small, verifiable improvement. Read
[AGENTS.md](../../../AGENTS.md) and keep the original task's outcome central.
A retrospective is not permission to redesign unrelated code or expand policy.

## Reconstruct the evidence

1. Identify the intended result and what actually happened. Use the commands,
   errors, diffs, review feedback, and available timings from this session.
   Distinguish product defects, environment failures, missing guidance, and mistaken
   assumptions. Do not invent recurring incidents or estimated savings.
2. Find the first avoidable divergence. Ask what evidence was available at that
   moment and which tool, source, or contract would have prevented the wrong turn.
   Separate an observed cause from a plausible explanation still needing a check.
3. Note successful steps worth preserving. Avoid rewriting a working workflow
   merely to make it more uniform or adding checks that repeat existing gates.

## Choose the smallest useful intervention

Prefer a change near the source of the problem. Compare its recurring benefit with
maintenance cost; no numeric score is needed without supporting data.

| Observed friction | Useful intervention |
| --- | --- |
| Wrong command or stale setup | Correct the owning guide or executable help using a verified invocation. |
| Repeated manual boundary checking | Extend the existing deterministic checker with a meaningful negative control. |
| Late discovery of missing tools | Improve the existing preflight/error with the dependency and recovery step. |
| Confused ownership or lifecycle | Clarify the existing architecture contract with an actual example. |
| Misleading regression test | Replace its weak oracle while preserving meaningful behavioral coverage. |
| Review missed local changes | Fix scope selection to include the intended index/worktree/untracked state. |
| Oversized or misleading skill | Tighten its trigger and load detailed references only when relevant. |

Search before adding a new tool or rule. `tools/check-architecture.py`, host tests,
fixture runners, and CI may already own the check. Improve those rather than build
an overlapping framework. A recurring machine-checkable invariant belongs in an
executable check where practical; prose should explain the remaining judgment.
Never weaken a guard or discard a test just to turn a failure green.

## Respect the operating environment

- Trace build/setup failures through [build system](../../../docs/build-system.md)
  and the current workflow. Preserve the `cargo nxrs` profile preparation path,
  pinned tools, generated-input identity, and effective native configuration checks.
  Distinguish a source save from a successful sync and use documented recovery.
- Do not fix a host limitation by silently substituting mock evidence for physical
  qualification. Record what a missing device or tool prevented and what was still
  established. Preserve fixtures that expose timing, lifecycle, or ABI failures.
- Keep changes near their owner. Use [docs/README.md](../../../docs/README.md) to
  find current documentation. Put a decision in its existing design page, setup in
  its guide, and a reusable task workflow in its skill. Keep AGENTS concise and
  directional; avoid pasting whole architecture sections or incident transcripts.
- Make instructions specific and actionable. Replace generic reminders such as
  “be careful” with a decision rule, command, example, or diagnostic tied to the
  failure. Avoid universal full-suite testing, mandatory questions, or new approval
  rituals where the task already supplies authorization.

## Apply and verify

Apply small improvements within the requested scope, preserving unrelated work.
For an unresolved product or architecture choice, describe the concrete alternatives
and continue the independent authorized work; do not invent a decision to unblock a
retro. Record larger follow-ups in the existing task tracking only when authorized.

Verify the changed workflow against the incident that motivated it. A checker needs
both a violating example and an allowed example. A command needs the documented
working directory and inputs. A skill needs a realistic trigger, correct links,
and behavior that addresses the mistake without needless work on ordinary tasks.
Documentation-only changes do not require a full firmware qualification campaign.

Report the observed problem, the change, why it should prevent recurrence, and the
verification result. Clearly distinguish implemented improvements from suggestions
and unavailable evidence. Stop when the concrete friction has been addressed.

Adapted principles and upstream license: [attribution](../ATTRIBUTION.md).
