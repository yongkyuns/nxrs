#!/usr/bin/env bash
# Reproduce explicit D2 node geometry, then apply the declared connector routes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
D2="${D2:-d2}"
if [[ "$("$D2" --version)" != 'v0.9.0' ]]; then
  echo 'This atlas requires the official D2 v0.9.0 binary (bundled TALA).' >&2
  exit 1
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
render_one() {
  local dest="$1"
  mkdir -p "$dest"
  for source in *.d2; do
    [[ "$source" == material.d2 ]] && continue
    "$D2" --layout=tala --theme=0 --pad=24 "$source" "$dest/${source%.d2}.svg"
  done
}
render_one "$work/first"
render_one "$work/second"
for first in "$work/first/"*.svg; do
  cmp -- "$first" "$work/second/$(basename "$first")"
done
python3 route_svg.py --input-dir "$work/first" --output-dir .
python3 route_svg.py --input-dir "$work/second" --output-dir "$work/routed-second"
for second in "$work/routed-second/"*.svg; do
  cmp -- "$second" "$(basename "$second")"
done
python3 check.py
python3 test_route_svg.py
printf '%s\n' 'Raw and routed SVGs reproduced byte-for-byte in two independent renders.'
