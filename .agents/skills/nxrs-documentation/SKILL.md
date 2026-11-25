---
name: nxrs-documentation
description: Write and improve NXRS tutorials, task guides, API explanations, README content, architecture explanations, and documentation website UI. Use for natural technical prose, runnable Rust and firmware examples, navigation, typography, diagrams, responsive layouts, themes, or accessibility of the existing NXRS website. Ground claims in current source and distinguish host, target, emulator, and physical hardware evidence.
---

# NXRS documentation

Help a reader build understanding through a working task. Treat the explanation,
example, navigation, and rendered page as one experience.

## 1. Establish the task and its sources

Read the applicable `AGENTS.md`, `website/EDITORIAL.md`, and `website/README.md`.
Inspect the current source and nearby guides before drafting. Identify the reader,
their starting knowledge, the task, and what success looks like. Infer sensible
defaults from the request; ask only when an unresolved distinction changes the
result materially. Preserve requested titles, spellings, terminology, and scope.

Use repository-root paths below as the source map:

| Need | Read or edit |
| --- | --- |
| Learning path and returning-user task map | `website/guide/`, especially `start-here.md` and `first-application.md` |
| Page titles, order, and reference groups | `website/site.json` |
| Editorial and build conventions | `website/EDITORIAL.md`, `website/README.md` |
| API catalog and build configuration | `website/api.md`, `website/api.json`, owning crates |
| Website presentation | `website/layout.html`, `website/static/`, `website/build.py` |
| Runnable documentation projects | `website/examples/basic-firmware/`, `website/examples/controller/` |
| Architecture authority | `AGENTS.md` and the focused documents it names under `docs/` |

Follow existing HAL, platform, composition, and lifecycle ownership. Link to
`docs/hal-platform-architecture.md`, `docs/nuttx-device-access.md`,
`docs/static-device-composition.md`, or `docs/peripheral-composition.md` where relevant;
do not create a competing architectural authority.

## 2. Build a useful learning sequence

Read [natural writing](references/natural-writing.md). Choose a main page purpose:
tutorial, task guide, reference, or explanation. Start with the outcome, then the
minimum setup needed to act. Explain a concept where the reader first needs it.

Preserve the general-purpose Rust firmware introduction: host setup, a minimal
independent Cargo application, then a NuttX build. Keep product examples such as
camera or navigation pipelines under their relevant examples and references.
Do not make a reader learn one product to understand the framework.

For each tutorial step, connect action → observable result → useful explanation.
Give complete code when it must run, explain the files the reader owns, and end
with a specific next task. Keep build-system explanation distinct from a target
walkthrough. Link both directions when it helps a reader arriving through search.

## 3. Ground commands, examples, and claims

Trace commands and APIs to source, manifests, profiles, owning tests, and current
guides. State working directory, pinned toolchain, configuration, output, and relevant
platform constraints. Keep source-checked tutorial markers and runnable projects
aligned. Do not hand-edit generated Rustdoc or copy its signatures into a second
catalog. Rebuild API artifacts when their recorded inputs change.

Use the existing `cargo nxrs build` workflow for NuttX instructions. Keep application
behavior in ordinary Rust and profile/platform choices at their documented boundary.
Do not invent target bindings, bypass configuration, or suggest a host stub proves
a device works.

Separate implemented behavior, host checks, target builds, emulator runs, physical
hardware evidence, and open work. State the profile and hardware when they affect a
claim. Preserve historical reports and their measured results during editorial work.

## 4. Make the site calm and readable

Read [website design](references/website-design.md) before changing layout, CSS,
templates, navigation, diagrams, or documentation controls.

Use the existing approved logo, typography, blue/orange palette, shared layout,
theme tokens, and SVG language. Make the article and next action easy to find.
Improve hierarchy, line length, and spacing before adding panels or decoration.
Keep the scope proportionate; do not swap frameworks, add a web design dependency,
or introduce a parallel design system for a documentation edit.

Preserve routes, useful fragments, grouped navigation, guide pagination, native
mobile contents, search, and no-JavaScript reading. Update `website/site.json`
deliberately so each guide has one section and references remain discoverable.

## 5. Verify the actual reading experience

Use [verification](references/verification.md) to select checks for the change.
Run required repository gates and the smallest additional checks that address real
risks. Do not add tests that only repeat an edited sentence.

For rendered content or UI changes, build and inspect the affected page and a
representative long/code-heavy page on desktop and mobile, in both themes. Follow
the task with keyboard and pointer. Inspect screenshots rather than merely saving
them. Check links, fragments, code, tables, diagrams, focus, navigation, and search
as applicable. Verify root and project-prefix paths when the renderer or links change.
State any blocked inspection and its missing prerequisite.

## 6. Finish with precise evidence

Describe the reader improvement, source files changed, commands actually run and
their results, and any remaining gap. Separate site rendering and example execution
from firmware and hardware qualification. Never report an unexecuted check as passing.
Follow existing repository rules for publishing; documentation work does not imply
an instruction to deploy the website or flash a device.

Adaptation context and upstream credit: [skill attribution](../ATTRIBUTION.md).
