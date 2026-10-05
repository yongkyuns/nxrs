# Reference diagrams

The OpenVela/nxrs comparison uses D2 source plus checked-in SVGs so GitHub can display the diagrams without an external rendering service.

| Source | Rendered diagram |
| --- | --- |
| [openvela-abstractions.d2](openvela-abstractions.d2) | [OpenVela device-class path](openvela-abstractions.svg) |
| [nxrs-capability-boundary.d2](nxrs-capability-boundary.d2) | [Nxrs capability/provider boundary](nxrs-capability-boundary.svg) |

## Space-conscious layout

Keep diagrams proportionate to the surrounding text. Prefer compact landscape, left-to-right layouts with short labels and minimal outer padding. Group related implementation details rather than stretching every step into a separate column; keep detailed explanations in the prose. Check diagrams at the document's actual reading width, not only full size. Do not achieve compactness by making labels unreadably small. Split a complex diagram instead of creating a tall stack or an excessively wide strip.

These diagrams show the four main device-path stages and the three-column capability/provider boundary. Dashed provider branches mean alternative build selections, not runtime dispatch. The Material palette is unchanged; rendering uses 16 px outer padding.

## Material-style palette

Both sources import [material.d2](material.d2): a custom Material-style light palette with rounded shapes, blue application nodes, teal contracts, indigo implementations, amber platform adaptation, and neutral hardware/data sources. This is not a built-in theme named Material. D2 v0.9.0's [theme catalog](https://github.com/d2lang/d2/blob/v0.9.0/d2themes/d2themescatalog/catalog.go) does not contain that preset; the sources explicitly style nodes over base theme 0. See the official [D2 theme documentation](https://d2lang.com/tour/themes/).

## Reproduce

Install [D2 v0.9.0](https://github.com/d2lang/d2/releases/tag/v0.9.0), then run from the repository root:

```sh
d2 --version
bash docs/references/openvela/diagrams/render.sh
```

Alternatively, set `D2=/absolute/path/to/d2`. The script uses the ELK layout engine and emits both SVGs alongside their sources. Regenerate the SVGs whenever a diagram or the shared palette changes. Using another D2 version may change layout or serialization.

## Validation

Both SVGs were compiled with D2 v0.9.0, parsed as XML, and visually inspected in Chromium alongside the reference text. Their view boxes are 922 x 116 (OpenVela) and 758 x 284 (nxrs). At an 800 px reading width they occupy approximately 101 px and 300 px of height, with labels approximately 16 px and 19 px high. This checks readability at document width; the SVGs remain scalable. The renderer passed `bash -n`, and both D2 palette imports resolve locally.

The diagrams illustrate the boundaries discussed in the [reference note](../README.md); they do not claim all provider/target combinations are implemented or tested.
