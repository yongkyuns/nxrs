# Comparison figures

These figures belong to [the independent RTOS analysis](../../rtos-comparison.md).
The D2 sources describe the service architecture and timing definitions. The
three charts are generated directly from the committed public JSON reports;
they do not pool the full-capacity, timing-control and scheduling cohorts.

The architecture diagrams import `material-theme.d2`: a shared Material 3-style
light palette, rounded 12 px cards, 18 px node labels and 16 px connection labels.
Boxes use thin outline-variant strokes; arrows retain stronger contrast.
Outer surfaces, nested inbox containers and leaf cards have distinct fills
from the surface, tertiary-container and primary/secondary-container roles.
D2 has no built-in Google Material preset; these explicit styles override its
neutral base theme. Compact, mostly vertical layouts keep the text readable
when the SVG is fitted to the document, rather than requiring zoom. The tests
enforce a minimum effective 16 px label size at a 720 px reading width.
The bar charts use their original palette and layout.

From the repository root:

```sh
d2 --layout dagre --dagre-nodesep 32 --theme 0 --pad 24 --scale 1 docs/assets/rtos-comparison/service-loop.d2 docs/assets/rtos-comparison/service-loop.svg
d2 --layout dagre --theme 0 --pad 24 --scale 1 docs/assets/rtos-comparison/execution-models.d2 docs/assets/rtos-comparison/execution-models.svg
d2 --layout dagre --theme 0 --pad 24 --scale 1 docs/assets/rtos-comparison/latency-path.d2 docs/assets/rtos-comparison/latency-path.svg
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
