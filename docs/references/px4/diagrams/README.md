# PX4 architecture diagrams

Read the [architecture study](../README.md) for the common review questions and the [atlas](../architecture-atlas.md) for detailed execution, data-flow and ownership explanations. The figures distinguish logical responsibility, executing thread, retained topic storage and scheduling; none of these boundaries automatically implies another process or protection domain.

## Views and reading size

The reference contains **14 editable D2 diagrams**: three primary execution maps, nine compact mechanism views and two IMU/GNSS propagation views. Every source has a checked-in SVG. See the [atlas view guide](../architecture-atlas.md#view-guide) for the complete reading map.

| Primary diagram | Canvas | Standalone reading width |
| --- | --- | --- |
| [Sensor-to-EKF execution map](sensor-to-ekf-execution-map.svg) · [D2](sensor-to-ekf-execution-map.d2) | 1854 × 1573 | 1600 px |
| [Execution loops and data flow](execution-loops-data-flow.svg) · [D2](execution-loops-data-flow.d2) | 2200 × 1695 | 1800 px |
| [Four-loop interactions](execution-map.svg) · [D2](execution-map.d2) | 1654 × 1331 | 1000 px |

These are detailed reference maps, not 800-pixel thumbnails. `layout-metrics.json` records the documented reading widths and effective type; every view has at least 14 px text at its reading width. The nine compact views use 800 px; the two supplementary propagation views use 1200 px. Open the linked SVG at the appropriate width rather than shrinking detailed labels to fit inline prose.

## Visual grammar and layout decisions

The sensor-to-EKF map uses four equal-width execution cards, an IRQ/UART entry-label band and distinct topic-routing lanes. Topic order follows consumers; the uORB caption sits below the wiring. Data and scheduling use distinct boundary ports so retained state and a wakeup cannot be mistaken for the same signal.

The wider execution map aligns workers with the modules they execute. INS0 and navigation worker cards span their related module columns. Explicit spacer cells reserve room without expanding all outer padding. Scheduler fan-out, same-thread execution and topic traffic occupy separate corridors. Labels sit beside or above routes.

The four-loop map retains independent IMU and GNSS paths, with labels above transfers and data/wake pairs separated. Two explanatory panels interrupt only decorative lifelines; they do not hide signal arrows. The Hardware/NuttX responsibility band and A/B/C/D execution ownership remain explicit. A flat build is shared address space, not protected user/kernel isolation. In the illustrated configuration, GNSS does not directly trigger EKF2; EKF2 ingests it during an IMU-driven run or a configured fallback execution.

## Exact reproduction

Requires **D2 v0.9.0**, Bash and Python 3.9+. The diagram scripts use ELK for the source layout. D2 owns topology, blocks, labels and styles; `routes.json` supplies orthogonal waypoints and explicit endpoint ports for the three primary maps. The routing pass does not move nodes. A D2 playground without the local palette, pinned layout configuration and routing sidecar is not the complete reproduction path.

```sh
bash docs/references/px4/diagrams/render.sh
```

This renders all 14 SVGs, applies the three routing plans, checks geometry/readability and runs five standard-library regression tests. Raw SVG hashes fail closed when a source or renderer changes. Review the affected geometry before refreshing hashes; do not relabel a changed render as qualified without inspection.

## Routing and browser qualification

The three primary maps contain **65 connectors** (19 + 28 + 18, including five decorative lifelines) and **130 boundary endpoints**. The routing checks preserve arrow markers, wording and rendered node groups, reject leaf-block interior intersections and shared collinear signal segments, and distinguish wire crossings from junctions with short white underlays.

The maps are **not all planar**: the two dense maps have 9 and 11 strict signal-wire crossings, while the four-loop map has none. See `routing-checks.json`. This differs from the crossing-free OpenVela/Zephyr atlas layouts; it is not a contradictory global zero-crossing claim. Layout dimensions are chosen for the actual subject, not forced into a common thumbnail size.

For optional browser qualification and PNG previews:

```sh
python3 -m pip install playwright
python3 -m playwright install chromium
python3 docs/references/px4/diagrams/check_browser.py --screenshots-dir /tmp/px4-previews
```

Use `--chromium /path/to/chromium` for an installed browser. The check measures actual rendered text with D2's embedded fonts: node-label containment, text/text overlap and text/signal-arrow clearance. It separately verifies the two masked decorative-lifeline intersections. `browser-checks.json` records the browser version and results rather than assuming browser-independent metrics.

## Evidence scope

The [qualified rendering run](https://github.com/yongkyuns/nxrs/actions/runs/37092084190) and the checked-in reports concern documentation geometry and reproducibility only. No physical-target build, timing benchmark, runtime regression test or backend qualification is implied. [Source snapshots and implementation scope](../sources.md) are independent of layout evidence. The renderer and scripts are documentation tools, not firmware dependencies.
