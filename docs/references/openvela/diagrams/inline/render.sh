#!/usr/bin/env bash
# Render the editable D2, route declared connectors, and check both passes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
D2="${D2:-d2}"
if [[ "$("$D2" --version)" != v0.9.0 ]]; then
  echo 'Use the official D2 v0.9.0 binary with bundled TALA.' >&2
  exit 1
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
for pass in first second; do
  mkdir -p "$work/$pass" "$work/$pass-routed"
  for source in *.d2; do
    [[ "$source" == material.d2 ]] && continue
    "$D2" --layout=tala --theme=0 --pad=24 "$source" "$work/$pass/${source%.d2}.svg"
  done
  python3 route_svg.py --input-dir "$work/$pass" --output-dir "$work/$pass-routed"
done
for svg in "$work/first/"*.svg; do
  name="$(basename "$svg")"
  cmp -- "$svg" "$work/second/$name"
  cmp -- "$work/first-routed/$name" "$work/second-routed/$name"
done
cp -- "$work/first-routed/"*.svg .
cp -- "$work/first-routed/routing-checks.json" .
python3 check.py
python3 test_route_svg.py
printf '%s\n' 'Inline D2 and routed SVGs reproduced byte-for-byte in two independent renders.'
