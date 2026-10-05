#!/usr/bin/env bash
# Render the OpenVela reference diagrams with D2 v0.9.0.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
D2="${D2:-d2}"
if ! command -v "$D2" >/dev/null 2>&1; then
  echo 'D2 is required. Install v0.9.0 or set D2 to its executable path.' >&2
  exit 1
fi
for name in openvela-abstractions nxrs-capability-boundary; do
  "$D2" --layout=elk --theme=0 --pad=16 "$name.d2" "$name.svg"
done
