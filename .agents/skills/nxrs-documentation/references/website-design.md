# Design and review the NXRS documentation website

## Diagnose the existing experience

Open the current page and read `website/README.md` and `website/EDITORIAL.md` before
changing presentation. Identify the reader's actual obstacle and define a visible
improvement. Capture a baseline when comparison will help. Preserve the site's
approved identity and the established reading path.

| Surface | Existing owner |
| --- | --- |
| Shared shell and semantic structure | `website/layout.html` |
| Colors, typography, columns, code, tables, responsive layout | `website/static/site.css` |
| Generated Rust API styling | `website/static/rust-api.css` |
| Search, navigation, theme | `website/static/site.js`, `website/static/theme.js` |
| Page titles, guide groups, reference placement | `website/site.json` |
| Markdown rendering and site generation | `website/build.py` |
| Architecture artwork and design rules | `docs/assets/README.md`, `docs/assets/render-architecture.mjs` |

Use the existing `--bg`, `--surface`, `--surface-soft`, `--ink`, `--muted`, `--line`,
`--blue`, `--sky`, and orange accent tokens, including their dark-theme definitions.
Keep the approved logo and current font stacks. Extend shared components rather
than introducing arbitrary per-page CSS, a remote font dependency, an icon kit,
or a new framework for a documentation improvement.

## Give the article priority

Make the title, purpose, first action, and next step easy to identify. Use the
existing reading measure, type scale, line height, and spacing as a coherent system.
Keep the topic sidebar and page contents quieter than the article. Align controls,
captions, code, and tables with the reading column.

Use prose for explanation and compact tables for mappings. Reserve cards for a
real choice among independent destinations. Avoid turning a guide into a dashboard
of bordered panels, decorative metrics, large heroes, and repeated calls to action.
Use the actual code or one helpful diagram to establish technical character.

Keep code legible and copyable. Long code and tables may scroll in their own
containers; surrounding prose should reflow. Do not reduce text to tiny sizes or
hide overflow across the page. Check captions, long tokens, error text, table
headings, and copy controls at the smallest width. Preserve whitespace where it
changes code, and let prose and descriptive table cells wrap.

## Preserve the learning path and accessible controls

Keep guide groups, current-page indication, breadcrumbs, whole-guide pagination,
and the mobile contents index consistent. A guide belongs to one section in
`website/site.json`; core references remain discoverable without flooding the
sidebar. Preserve routes and useful heading fragments when wording changes.

Use native links, buttons, labels, headings, lists, and disclosure elements. Keep
the skip link and the no-JavaScript reading path useful. Mobile navigation should
have a clear label, visible state, sensible focus order, and comfortable touch
spacing. Search should support keyboard opening, Escape dismissal, focus return,
useful results, and an understandable empty state.

Check both website search and the generated Rustdoc navigation when touching API
integration. Website search finds guides and catalog entries; Rustdoc search finds
API items. Preserve the route back from API pages to the documentation. Verify
theme persistence and loading behavior without unreadable flashes.

## Make diagrams answer one question

Use `docs/assets/render-architecture.mjs` for the shared architecture artwork;
follow `docs/assets/README.md`. Maintain rounded containers, short labels, muted
connectors, and the approved blue/orange accents. Keep light and `-dark.svg` outputs
in sync and use the existing theme-specific markup.

Choose diagrams for ownership, lifecycle, flow, or composition. Use a table for a
small inventory or one-to-one mapping. Give each figure a scoped caption, useful
alternative text, and a full-size SVG link when detail needs more space. Inspect
labels, connector direction, crossings, and clipping at both full size and the
reading-column width. Regenerate from editable source; do not hand-edit generated
SVGs or use AI-generated raster images for exact technical relationships.

Measurement-specific artwork under `docs/assets/rtos-comparison/` has its own
recorded data and renderer. Presentation changes must preserve its historical facts.

## Inspect both themes and narrow screens

Inspect actual rendered pages at desktop and phone widths, including 320 CSS pixels
for reflow. Use representative long prose, a heading, code, a table, and a diagram.
Keep an eye on sticky headers obscuring focused headings, navigation scroll traps,
and controls disappearing at breakpoints. Do not infer visual quality from a
successful build or from a screenshot file you have not opened.

Check keyboard focus and order, labels, pointer/touch operation, reduced motion,
and meaningful light/dark states. Measure contrast for body text, muted captions,
code highlighting, links, and controls in normal, hover, focus, and selected states.
WCAG's minimum text contrast is 4.5:1 for ordinary text and 3:1 for qualifying large
text; see [the W3C explanation](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).

Check reflow at 320 CSS pixels, or the equivalent 1280-pixel viewport at 400% zoom.
Prose should not require horizontal scrolling. Intrinsically two-dimensional
content such as a data table can retain its own scrollable region; see
[W3C reflow guidance](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html).
Screenshots and limited checks do not establish full accessibility conformance.
Record exactly what was inspected and use [verification](verification.md) for the
existing site, link, browser, and example checks.
