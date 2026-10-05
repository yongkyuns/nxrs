# Comparison figures

These figures belong to [the independent RTOS analysis](../../rtos-comparison.md).
The D2 sources describe the service architecture and timing definitions. The
three charts are generated directly from the committed public JSON reports;
they do not pool the full-capacity, timing-control and scheduling cohorts.

From the repository root:

```sh
d2 --layout dagre --theme 0 --pad 24 docs/assets/rtos-comparison/service-loop.d2 docs/assets/rtos-comparison/service-loop.svg
d2 --layout dagre --theme 0 --pad 24 docs/assets/rtos-comparison/execution-models.d2 docs/assets/rtos-comparison/execution-models.svg
d2 --layout dagre --theme 0 --pad 24 docs/assets/rtos-comparison/latency-path.d2 docs/assets/rtos-comparison/latency-path.svg
python3 docs/assets/rtos-comparison/render.py
python3 docs/assets/rtos-comparison/render.py --check
python3 -m unittest discover -s docs/assets/rtos-comparison -p 'test_*.py'
```

D2 renders were produced with v0.9.0. The chart renderer uses only Python's
standard library; no plotting framework, downloaded font or website is needed.
All chart axes start at zero. kB means 1,000 bytes; KiB means 1,024 bytes.
RAM segments identify common reservations and nominal test fields, not a
complete allocator attribution. The 250 kB marker is a planning reference,
not the usable RAM of the ESP32-S3 or a qualified product memory budget.
