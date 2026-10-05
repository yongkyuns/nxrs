# Comparison figures

These figures belong to [the independent RTOS analysis](../../rtos-comparison.md).
The D2 sources describe the service architecture and timing definitions. The
three charts are generated directly from the committed public JSON reports;
they do not pool the full-capacity, timing-control and scheduling cohorts.

The architecture diagrams import `material-theme.d2`: a shared light palette
using the [Material palette values](https://github.com/angular/material/blob/master/src/core/services/theming/theme.palette.js),
rounded 12 px cards and 18 px labels. Blue and teal cards pair shade 50 fills
with shade 100 outlines; nested containers use blue-grey 50/100 and outer
surfaces use grey 50/300. Each 1 px outline accents its own fill's hue, rather
than adding an unrelated border color. Dark blue-grey text and rounded
connectors remain legible against the light fills.

D2 has no built-in Google Material preset; these explicit styles override its
neutral base theme. ELK lays out the service flow left-to-right, the execution
models in aligned horizontal lanes, and the timing intervals in three panels.
The tests enforce landscape proportions, matching fill/outline pairs and a
minimum effective 16 px label size at a 720 px reading width.
The bar charts use their original palette and layout.

From the repository root:

```sh
for diagram in service-loop execution-models latency-path; do
  d2 --layout elk --elk-nodeNodeBetweenLayers 20 \
    --elk-padding '[top=36,left=12,bottom=12,right=12]' \
    --elk-edgeNodeBetweenLayers 16 --elk-nodeSelfLoop 20 \
    --theme 0 --pad 12 --scale 1 \
    "docs/assets/rtos-comparison/$diagram.d2" \
    "docs/assets/rtos-comparison/$diagram.svg"
done
python3 docs/assets/rtos-comparison/render.py
python3 docs/assets/rtos-comparison/render.py --check
python3 -m unittest discover -s docs/assets/rtos-comparison -p 'test_*.py'
```

D2 renders were produced with v0.9.0. The chart renderer uses only Python's
standard library; no plotting framework, downloaded font or website is needed.
`--scale 1` gives the SVG an intrinsic size so narrow diagrams do not expand
their labels to fill the reader's viewport. Large images can still fit down
to the document's column width.
All chart axes start at zero. kB means 1,000 bytes; KiB means 1,024 bytes.
RAM segments identify common reservations and nominal test fields, not a
complete allocator attribution. The 250 kB marker is a planning reference,
not the usable RAM of the ESP32-S3 or a qualified product memory budget.
