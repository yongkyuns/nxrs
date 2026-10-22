#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
STREAM_MODEL=none
STREAM_ARGS=()
while (($#)); do
  case "$1" in
    --detector) STREAM_MODEL="${2:?choose pico, vww or none}"; shift 2 ;;
    --probe) STREAM_ARGS+=(--feature probe); shift ;;
    *) STREAM_ARGS+=("$1"); shift ;;
  esac
done
case "$STREAM_MODEL" in
  none) ;;
  pico) STREAM_ARGS+=(--feature nxrs-detector/pico-model) ;;
  vww) STREAM_ARGS+=(--feature nxrs-detector/person-model) ;;
  *) echo 'detector must be none, pico or vww' >&2; exit 2 ;;
esac
export NXRS_STREAM_MODEL="$STREAM_MODEL"
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/stream/Cargo.toml \
  --app-package nxrs-std-stream --bin nxrs-std-stream --command stream_test \
  --priority 100 --stack-size 32768 --platform esp32s3-camera-stream \
  --out target/camera-stream-device --abi-profile minimal \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c \
  --target-c-source tests/nuttx-std/stream/ffi/platform.c \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "${STREAM_ARGS[@]}"
