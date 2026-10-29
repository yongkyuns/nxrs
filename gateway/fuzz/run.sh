#!/usr/bin/env bash
# Coverage-guided qualification of production parsers; no live/network inputs.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
ulimit -c 0
toolchain=nightly-2026-03-01
host="$(rustc +"$toolchain" -vV | sed -n 's/^host: //p')"
work="$(mktemp -d "${TMPDIR:-/tmp}/nxrs-parser-fuzz.XXXXXX")"
mkdir "$work/probe" "$work/mjpeg" "$work/http_request"
echo "Parser fuzz inputs and logs: $work (never uploaded by CI)"
# Instrument dependencies too, using LLVM's libFuzzer coverage and ASan support.
# --target keeps compiler/proc-macro host code outside sanitizer instrumentation.
export RUSTFLAGS='-Cpasses=sancov-module -Cllvm-args=-sanitizer-coverage-level=4 -Cllvm-args=-sanitizer-coverage-inline-8bit-counters -Cllvm-args=-sanitizer-coverage-pc-table -Cllvm-args=-sanitizer-coverage-trace-compares -Zsanitizer=address -Cdebug-assertions=yes -Coverflow-checks=yes --cfg fuzzing'
unset NXRS_PLATFORM
cargo +"$toolchain" build --locked --manifest-path gateway/Cargo.toml \
  -p nxrs-parser-fuzz --target "$host" --release --bins \
  --config 'profile.release.debug="line-tables-only"'
binary="gateway/target/$host/release"
if "$binary/asan_probe" -runs=1 -artifact_prefix="$work/probe/" >"$work/asan.log" 2>&1; then
  echo 'FAIL: sanitizer positive control unexpectedly succeeded' >&2
  exit 1
fi
"${NXRS_PYTHON:-python3}" - "$work/asan.log" <<'PY'
import sys
with open(sys.argv[1], 'rb') as stream:
    if b'ERROR: AddressSanitizer: heap-use-after-free' not in stream.read(65536):
        raise SystemExit('FAIL: missing real ASan positive control')
print('PASS: actual ASan heap-use-after-free positive control')
PY
for target in http_request mjpeg; do
  max_length=8193
  if [[ "$target" == mjpeg ]]; then max_length=524289; fi
  "$binary/$target" "$work/$target" "gateway/fuzz/seeds/$target" \
    -max_total_time=30 -timeout=5 -rss_limit_mb=512 -max_len="$max_length" \
    -artifact_prefix="$work/$target/" -print_final_stats=1
done
