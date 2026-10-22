#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/stream/Cargo.toml \
  --app-package nxrs-std-stream --bin nxrs-std-stream --command stream_test \
  --priority 100 --stack-size 32768 --platform esp32s3-camera-stream \
  --out target/camera-stream-device --abi-profile minimal \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c \
  --target-c-source tests/nuttx-std/stream/ffi/platform.c \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "$@"
