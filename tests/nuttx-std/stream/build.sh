#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
STREAM_PLATFORM=esp32s3-camera-stream
STREAM_ARGS=()
while (($#)); do
  case "$1" in
    --platform) STREAM_PLATFORM="${2:?choose a product platform}"; shift 2 ;;
    --detector) echo 'Use --platform; the product profile owns detector selection.' >&2; exit 2 ;;
    --probe) STREAM_ARGS+=(--feature probe); shift ;;
    *) STREAM_ARGS+=("$1"); shift ;;
  esac
done
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/stream/Cargo.toml \
  --app-package nxrs-std-stream --bin nxrs-std-stream --command stream_test \
  --priority 100 --stack-size 32768 --platform "$STREAM_PLATFORM" \
  --out target/camera-stream-device --abi-profile minimal \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c \
  --target-c-source tests/nuttx-std/stream/ffi/platform.c \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "${STREAM_ARGS[@]}"
