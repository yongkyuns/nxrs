# Zephyr reference diagrams

All diagrams are editable D2 sources with checked-in SVGs. The local `material.d2` palette matches the OpenVela reference's Material-style colors; it is deliberately copied here so this reference has no dependency on the OpenVela directory. It is a custom palette over D2 theme 0, not a built-in Material preset.

| Diagram | Source | SVG dimensions | Height at 800 px width |
| --- | --- | --- | --- |
| Native and optional POSIX APIs | [D2](zephyr-abstractions.d2) · [SVG](zephyr-abstractions.svg) | 926 × 206 | 178 px |
| Hardware/software build selection | [D2](zephyr-build-selection.d2) · [SVG](zephyr-build-selection.svg) | 802 × 206 | 205 px |
| Proposed nxrs event delivery | [D2](nxrs-direction.d2) · [SVG](nxrs-direction.svg) | 886 × 284 | 256 px |

## Layout rule

Keep diagrams proportionate to surrounding prose. Prefer compact landscape layouts, three or four columns, short labels and minimal outer padding. Preserve readable text at the document's actual reading width; do not replace a tall stack with an excessively wide, tiny-text strip. Split complex topics into separate focused diagrams. For this note, each figure is below 320 px tall at an 800 px content width, and its 18 px source labels remain above 15 px when scaled to that width.

Arrows in the API figure are access routes, in the configuration figure build-time flow, and in the nxrs figure event delivery. The nxrs figure depicts the **proposed** separate-capacity queues and one logical selection point, not a universal single inbox or an implemented Zephyr provider. Details and limitations belong in the [research note](../README.md), not inside oversized nodes.

## Reproduce

Install [D2 v0.9.0](https://github.com/d2lang/d2/releases/tag/v0.9.0), then run from the repository root:

```sh
bash docs/references/zephyr/diagrams/render.sh
```

Set `D2=/absolute/path/to/d2` when necessary. The renderer uses ELK, base theme 0 and 16 px padding. Regenerate all three SVGs whenever their sources or palette change. A different D2 version may change layout or serialization.

The current SVGs were compiled with D2 v0.9.0, parsed as XML, and inspected in Chromium at an 800 px reading width alongside the note. Markdown links and D2 imports were checked, and the renderer passed `bash -n`. These are documentation checks, not firmware or runtime qualification.
