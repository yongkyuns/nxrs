#!/usr/bin/env bash
# Standalone physical-camera image; no radio credentials or networking.
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/camera/Cargo.toml \
  --app-package nxrs-std-camera --bin nxrs-std-camera --command camera_test \
  --priority 100 --stack-size 8192 --platform esp32s3-camera \
  --out target/camera-device --abi-profile minimal \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "$@"
