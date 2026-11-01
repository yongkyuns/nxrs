#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export NXRS_NUTTX_BUILD_JOBS="${NXRS_NUTTX_BUILD_JOBS:-2}"
exec bash tools/build-nuttx-std-app.sh \
  --app-manifest app/home-cam/Cargo.toml \
  --app-package nxrs-home-cam --bin home-cam --command home_cam \
  --priority 100 --stack-size 32768 --platform esp32s3-camera-stream-startup \
  --target-c-source platform/nuttx/home-cam/src/platform/nuttx/ffi/startup.c \
  --out target/camera-stream-device --abi-profile minimal \
  --size-optimized --trace-only-backtrace --panic-immediate-abort "$@"
