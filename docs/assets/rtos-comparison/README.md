# RTOS comparison figures

Figures for the [RTOS analysis](../../rtos-comparison.md), rendered from the
committed public JSON cohorts. Timing cohorts are not pooled.

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

D2 0.9.0. Chart axes start at zero. `kB` = 1,000 bytes; `KiB` = 1,024 bytes. RAM segments
show identified reservations and test fields, not complete allocator
attribution. The 250 kB marker is a planning reference, not usable-chip RAM
or a qualified product budget.

The size chart uses uncompressed gap-free ZIP packages from the separate image
layout report, hash-bound to the measured firmware. It is not a chart of flat
flash-file lengths. Required address gaps are reconstructed before flashing;
their on-device span remains in the analysis table.
