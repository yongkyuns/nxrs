#!/usr/bin/env bash
# Compatibility entry; Rust validates and copies the private checkout.
set -euo pipefail
test "$#" = 3 || { echo "Usage: $0 CACHE DESTINATION REVISION" >&2; exit 1; }
root="$(cd "$(dirname "$0")/.." && pwd)"
exec cargo run --quiet --locked --manifest-path "$root/Cargo.toml" -p nxrs-build -- --workspace-root "$root" copy-hal-cache --cache "$1" --destination "$2" --revision "$3"
