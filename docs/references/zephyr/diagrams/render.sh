#!/usr/bin/env bash
# Reproduce the Zephyr reference SVGs with D2 v0.9.0.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
D2="${D2:-d2}"
if ! command -v "$D2" >/dev/null 2>&1; then
  echo 'D2 v0.9.0 is required; install it or set D2 to its executable path.' >&2
  exit 1
fi
for name in zephyr-abstractions zephyr-build-selection nxrs-direction; do
  "$D2" --layout=elk --theme=0 --pad=16 "$name.d2" "$name.svg"
done
