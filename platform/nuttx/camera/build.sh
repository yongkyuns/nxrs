#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest platform/nuttx/camera/Cargo.toml \
  --app-package nxrs-camera-firmware --bin nxrs-camera-firmware --command camera \
  --priority 100 --stack-size 32768 --platform esp32s3-camera-stream-startup \
  --out target/camera-stream-device --abi-profile minimal \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "$@"
