# Comparison and decision diagrams

These figures sit beside the sections they explain in the [overview](../README.md) and [developer guide](../developer-guide.md). They are designed for **800 CSS pixels of reading width**, not as thumbnails of the larger architecture maps. Each one answers a focused question; its caption states what is shown and what is omitted.

## Views and sources

| Section | Diagram | What it explains and supporting evidence |
| --- | --- | --- |
| Collection | [Compare the same questions at different software levels](comparison.svg) · [D2](comparison.d2) | The cards describe different kinds of software. Dotted arrows below express design lessons, not dependencies or a chronology. [OpenVela overview](../openvela/README.md#1-overview) · [Zephyr overview](../zephyr/README.md#1-overview) · [PX4 overview](../px4/README.md#1-overview) |
| Buffer sizing | [Enough storage does not mean a timely answer](overload-budget.svg) · [D2](overload-budget.d2) | Compare storage for a bounded burst/blackout with the independent deadline requirement. [Worked assumptions](../developer-guide.md#how-much-buffering-is-enough). |
| Shutdown | [Why shutdown can hang](progress-deadlock.svg) · [D2](progress-deadlock.d2) | A wait cycle involving join, blocked send and queue draining. [Protocol explanation](../developer-guide.md#what-must-keep-making-progress). |

## Reading the arrows

Blue solid arrows are calls or local execution steps. Teal solid arrows carry records, copies or explicitly described item transfers. Orange dashed arrows are wakeups or requests for later execution. Grey dotted arrows are configuration/dependency, comparison or test-correlation relationships, as named in the caption. They do not imply runtime data transport.

A box is not automatically a thread, process, memory region or queue. Container titles and captions identify the intended boundary. Readiness is not a payload, a copied pointer does not copy its referent, and a lifecycle checklist is not a universal OS API.

## Reproduce and check

Use the **official D2 v0.9.0 binary with bundled TALA**, Bash and Python 3.9+. The Linux amd64 release archive SHA-256 is `5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9`. [Official release](https://github.com/d2lang/d2/releases/tag/v0.9.0).

```sh
D2=/path/to/d2 bash render.sh
python3 check_browser.py --chromium /path/to/chromium --screenshots-dir /tmp/inline-previews
```

Run these commands in this directory. Playwright and Chromium are optional documentation-validation dependencies for the second command, not firmware dependencies.

The `.d2` files own wording, node dimensions, placement and graph topology. `routes.json` owns boundary ports, orthogonal corridors and edge-label offsets. `route_svg.py` applies those routes without moving nodes. The complete input is D2 plus its palette, pinned renderer and routing file; D2 pasted alone into a playground does not reproduce the published routes.

`render.sh` performs two independent raw and routed renders and compares their bytes. It rejects node intersections, wire crossings, shared collinear wire segments, inconsistent topology and incorrect endpoint directions, and runs the seven existing routing regression tests. No crossing is hidden with a painted overlay.

`check_browser.py` loads the SVG's embedded fonts and checks actual text bounds, node containment, text overlaps, clipping and a 3px text-to-connector-centerline margin. It checks that the smallest text stays at least **15px at 800px width**, then exports images at natural and document reading size. See `browser-checks.json` for exact per-figure dimensions, font sizes and the browser version. These measurements do not prove architectural correctness.

The review data is in `routing-checks.json` and `browser-checks.json`. `manifest.json` maps each source to its section, scope and evidence. All files are local to this diagram set; no sibling RTOS reference is needed to render it.
