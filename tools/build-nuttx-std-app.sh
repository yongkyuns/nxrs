#!/usr/bin/env bash
# Compatibility entry for existing qualification drivers. Rust owns the build.
set -euo pipefail
cd "$(dirname "$0")/.."
exec cargo nxrs nuttx-build "$@"
