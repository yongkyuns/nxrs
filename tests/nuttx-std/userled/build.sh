#!/usr/bin/env bash
# Build the instrumented USERLED fixture through the ordinary product frontend.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
test "${NUTTX_STD_COMPAT_FIXES:-0}" = 1 || {
  echo 'USERLED qualification requires explicit NUTTX_STD_COMPAT_FIXES=1' >&2
  exit 1
}
OUT="$ROOT/target/nuttx-esp32s3-std"
bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/userled/Cargo.toml \
  --app-package nxrs-std-userled --bin nxrs-std-userled --command rust_std \
  --priority 100 --stack-size 65536 --platform esp32s3-userled \
  --out "$OUT" --abi-profile minimal \
  --target-c-source tests/nuttx-std/userled/nuttx/fixture.c

# The frontend checks the configured ABI and regenerates all final-image/std
# evidence. Retain the stricter USERLED-specific ownership and symbol gates.
grep -qx CONFIG_USERLED=y "$OUT/resolved.config"
if grep -Eq '^CONFIG_(USERLED_LOWER|USERLED_LOWER_READSTATE|USERLED_EFFECTS)=y$' "$OUT/resolved.config"; then
  echo 'Unexpected physical/effects lower half in instrumented fixture' >&2
  exit 1
fi
for symbol in main userled_supported userled_set; do
  grep -Eq " T $symbol$" "$OUT/rust-symbols.txt" || {
    echo "Missing package-owned USERLED definition $symbol" >&2; exit 1;
  }
done
for symbol in nxrs_userled_fixture_install nxrs_userled_fixture_verify nxrs_userled_fixture_fds close; do
  grep -Eq " U $symbol$" "$OUT/rust-symbols.txt" || {
    echo "Missing USERLED fixture/libc import $symbol" >&2; exit 1;
  }
done
python3 tests/nuttx-std/check-abi.py --check-imports "$OUT/rust-symbols.txt"
python3 - "$OUT" <<'PY_EVIDENCE'
import json, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
assert json.loads((out / 'std-source.json').read_text())['selected_source_matches']
messages = [json.loads(line) for line in (out / 'cargo-messages.jsonl').read_text().splitlines()]
metadata = json.loads(subprocess.check_output(
    ['cargo', 'metadata', '--locked', '--format-version=1', '--no-deps'], text=True))
led_packages = [package for package in metadata['packages'] if package['name'] == 'nxrs-led']
assert len(led_packages) == 1, 'unified LED HAL package missing or duplicated'
led_id = led_packages[0]['id']
led_roots = [m for m in messages if m.get('reason') == 'compiler-artifact'
             and m['package_id'] == led_id and m['target']['name'] == 'led']
assert len(led_roots) == 1, 'unified HAL root target led was not compiled'
selection = [m for m in messages if m.get('reason') == 'build-script-executed'
             and m['package_id'] == led_id]
assert len(selection) == 1, 'missing/duplicate LED platform cfg evidence'
cfgs = [cfg for cfg in selection[0]['cfgs'] if cfg.startswith(('platform=', 'hal_', 'nxrs_hal_'))]
assert cfgs == ['platform="nuttx"'], f'wrong LED platform selection: {cfgs}'
assert dict(selection[0]['env'])['NXRS_USERLED_PATH'] == '/dev/nxrs-userled'
assert 'static=led_native' in selection[0]['linked_libs'], 'LED package did not own its native archive'
(out / 'userled-hal-selection.json').write_text(json.dumps({
    'package': 'nxrs-led', 'target': 'led', 'artifact': led_roots[0],
    'platform': 'esp32s3-userled', 'device': '/dev/nxrs-userled', 'cfgs': cfgs,
    'native_libraries': selection[0]['linked_libs'],
}, indent=2) + '\n')
PY_EVIDENCE
for symbol in rust_std_main nx_start userled_register userled_ioctl userled_supported userled_set nxrs_userled_fixture_install nxrs_userled_fixture_verify nxrs_userled_fixture_fds; do
  grep -Eq " [TtWw] $symbol$" "$OUT/symbols.txt" || {
    echo "Missing final USERLED symbol $symbol" >&2; exit 1;
  }
done
cat >> "$OUT/provenance.txt" <<'USERLED_PROVENANCE'
qualification=userled-esp32s3
provider_path=/dev/nxrs-userled
lower_half=instrumented-test-not-physical-gpio
USERLED_PROVENANCE
sha256sum hal/led/Cargo.toml hal/led/build.rs hal/led/src/lib.rs hal/led/src/api.rs \
  hal/led/src/platform/mod.rs hal/led/src/platform/nuttx/mod.rs \
  hal/led/src/platform/nuttx/ffi/userled.c \
  platform/build-support/Cargo.toml platform/build-support/src/lib.rs \
  platform/build-support/src/profile.rs platform/build-support/src/native.rs \
  platform/build-support/src/native_nuttx.rs platform/nuttx/std-app/Makefile \
  platform/nuttx/platforms/esp32s3-userled.toml platform/nuttx/platforms/esp32s3-qemu-mock.toml \
  tests/nuttx-std/userled/src/main.rs tests/nuttx-std/userled/nuttx/fixture.c \
  tests/nuttx-std/userled/Cargo.toml tests/nuttx-std/userled/build.sh > "$OUT/userled-sources.sha256"
echo 'USERLED final image ready; runtime qualification is a separate required step.'
