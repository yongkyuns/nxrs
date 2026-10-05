#!/usr/bin/env bash
# D2 layout stays fixed; route_svg.py changes connectors only.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
D2="${D2:-d2}"
[[ "$("$D2" --version)" == *0.9.0* ]] || { echo 'D2 v0.9.0 required' >&2; exit 1; }
raw="$(mktemp -d)"
trap 'rm -rf "$raw"' EXIT
for name in architecture execution-contexts uorb-delivery topic-retention imu-acquisition estimator-inputs outer-control fast-control nxrs-direction execution-map imu-to-ekf gnss-to-ekf sensor-to-ekf-execution-map execution-loops-data-flow; do
  if [[ -n "${D2_SKILL_RENDER:-}" ]]; then
    bash "$D2_SKILL_RENDER" --engine elk --theme 0 --pad 16 --no-remote-assets "$name.d2" "$raw/$name.svg"
  else
    "$D2" --layout=elk --theme=0 --pad=16 "$name.d2" "$raw/$name.svg"
  fi
  cp "$raw/$name.svg" "$name.svg"
done
python3 route_svg.py --input-dir "$raw" --output-dir .
python3 check.py
python3 -m unittest -v test_route_svg.py

# Compact diagrams embedded in the main architecture overview.
D2="$D2" bash inline/render.sh
