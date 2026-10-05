# Openvela architecture diagrams

Read the [architecture atlas](../architecture-atlas.md) for the design brief, semantics, assumptions and primary sources behind each figure. The [original reference](../README.md) supplies the broader nxrs comparison.

## Views and reading size

These are **detailed reference maps**, not thumbnail diagrams. Open an SVG at **1400 CSS pixels wide** (or its natural width) to read it. All views remain landscape; linked SVGs retain resolution. At 800px document width the preview is an overview only, not a qualified reading size. Do not shrink labels to force detail into a thumbnail.

| Editable source | Rendered output | Canvas | Smallest text at 1400px |
| --- | --- | --- | --- |
| [openvela-abstractions.d2](openvela-abstractions.d2) | [SVG](openvela-abstractions.svg) | 1510 × 1008 | 16.69 px |
| [memory-model.d2](memory-model.d2) | [SVG](memory-model.svg) | 1510 × 1015 | 16.69 px |
| [sensor-execution.d2](sensor-execution.d2) | [SVG](sensor-execution.svg) | 1510 × 948 | 16.69 px |
| [bluetooth-boundaries.d2](bluetooth-boundaries.d2) | [SVG](bluetooth-boundaries.svg) | 1565 × 1008 | 16.10 px |
| [nxrs-capability-boundary.d2](nxrs-capability-boundary.d2) | [SVG](nxrs-capability-boundary.svg) | 1565 × 1018 | 16.10 px |

## Visual grammar and layout decisions

The Material-style palette distinguishes logical responsibilities, execution ownership, retained memory, hardware, and red access/protection gates. A colored region is **not automatically a process**. Scope is stated in every title/subtitle and explained in the atlas. Blue solid lines are API calls, teal solid lines are data/ownership, orange dashed lines are scheduling/readiness, and grey dotted lines are build/configuration dependencies.

Aligned cards provide repeatable comparison points. More complex relationships have reserved inter-column routing lanes and distinct boundary ports. API requests and returned data never share a collinear wire. Arrow labels sit off the lines; left-side return/selection corridors receive an explicit margin rather than cutting through intermediate cards. Memory alternatives and ownership models are split into separate figures rather than squeezed into one large stack chart.

## Exact reproduction

Requires the **official D2 v0.9.0 binary with bundled TALA**, Bash and Python 3.9+. [Official release](https://github.com/d2lang/d2/releases/tag/v0.9.0). Linux amd64 release archive SHA-256: `5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9`.

```sh
D2=/absolute/path/to/d2 bash docs/references/openvela/diagrams/render.sh
```

**Node geometry is explicitly authored in D2** (`top`, `left`, widths and heights). The pinned TALA renderer interprets those coordinates. [routes.json](routes.json) separately specifies endpoint ports, orthogonal corridor choices and label offsets. [route_svg.py](route_svg.py) applies those routes, preserves D2 node groups/wording/styles and arrow direction, and trims obsolete automatic-routing margins. It does not move nodes or silently hide crossings with painted underlays.

Copying only the D2 file into a playground is therefore **not the complete reproduction procedure**: import `material.d2`, use the matching renderer, and apply the routing pass for the published connector positions. Another layout engine or a missing sidecar is expected to differ. This is an explicit authored layout with checked routing, not a claim of a new general-purpose automatic layout optimizer.

`render.sh` renders every source twice into independent temporary directories and compares both raw and routed SVG bytes. It then checks topology coverage, SVG hashes, landscape aspect, node-interior clearance, boundary ports and wire intersections, and runs the seven-test routing regression suite. Unexpected geometry fails the command instead of being relabeled as acceptable. The reference remains self-contained: local copies of the same tooling intentionally avoid a dependency on the PX4 or sibling reference directory.

## Browser qualification and previews

Install Playwright and a Chromium browser for optional documentation-only qualification:

```sh
python3 -m pip install playwright
python3 docs/references/openvela/diagrams/check_browser.py \
  --chromium /path/to/chromium \
  --screenshots-dir /tmp/openvela-atlas-previews
```

The check loads each SVG with its embedded fonts, measures actual glyph extents using Canvas ascent/descent and SVG character positions, checks distinct text overlaps, leaf/container label containment, edge-label intrusion into blocks, canvas clipping and a 3px text-to-connector-centerline margin, then exports natural-size and 1400px PNGs. This does not replace visual inspection or prove semantic correctness.

The checked results are in [routing-checks.json](routing-checks.json) and [browser-checks.json](browser-checks.json). The current set covers **5 figures, 42 connectors, 84 boundary endpoints, and 211 rendered text lines**, with no reported block/label collisions, wire crossings or shared collinear segments. Browser version is recorded rather than implied to be renderer-independent.

## Scope

The SVGs and scripts are documentation assets only. No runtime dependency, RTOS configuration, board setup or firmware workflow is introduced. Layout checks are not firmware/performance qualification. The PX4 subtree is unchanged.
