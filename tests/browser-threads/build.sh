#!/usr/bin/env bash
# Qualification adapter; the shared Rust controller builds and packages the program.
set -euo pipefail
cd "$(dirname "$0")/../.."
args=()
case "${BROWSER_STD_TLS_FIX:-0}" in
  0) args+=(--unmodified-browser-std) ;;
  1) ;;
  *) echo 'BROWSER_STD_TLS_FIX must be 0 or 1' >&2; exit 1 ;;
esac
exec cargo +1.90.0 nxrs build --manifest-path tests/browser-threads/Cargo.toml \
  --platform web --out target/browser-threads --html tests/browser-threads/index.html \
  ${args[@]+"${args[@]}"}
