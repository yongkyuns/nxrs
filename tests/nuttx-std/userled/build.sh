#!/usr/bin/env bash
# Isolated target test, reusing the existing pinned ESP32-S3 std bootstrap.
# The intermediate thread-probe image is NOT USERLED evidence. Clean/relink with
# the actual LED binary, then regenerate all final-image/ABI records below.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
test "${NUTTX_STD_COMPAT_FIXES:-0}" = 1 || {
  echo 'USERLED qualification requires explicit NUTTX_STD_COMPAT_FIXES=1' >&2
  exit 1
}
bash tests/nuttx-std/build.sh esp32s3
OUT="$ROOT/target/nuttx-esp32s3-std"
source "$ROOT/target/qemu-tools/environment.sh"
export RUSTUP_TOOLCHAIN=1.90.0 RUSTC_BOOTSTRAP=1
export NUTTX_STD_SYSROOT="$OUT/toolchain"
export RUSTC="$NUTTX_STD_SYSROOT/bin/rustc" CARGO_BUILD_RUSTC="$NUTTX_STD_SYSROOT/bin/rustc"
export NUTTX_STD_GNU_LINKER="$(command -v xtensa-esp32s3-elf-ld)"
CARGO_BIN="$(rustup which --toolchain 1.90.0 cargo)"
TARGET=xtensa-esp32s3-nuttx
TARGET_ARG="$OUT/$TARGET.json"

# Remove baseline objects before changing the qualification app's target C
# sources. Production Rust provider/helper and upstream USERLED are not edited.
make -C "$OUT/nuttx" clean CROSSDEV=xtensa-esp32s3-elf-
APP="$OUT/apps/examples/nxrs_std"
cp "$ROOT/tests/nuttx-std/userled/nuttx/"* "$APP/"
cp "$ROOT/hal/led/ffi/nuttx/nxrs_userled.c" "$APP/"
(
  cd "$OUT/nuttx"
  kconfig-tweak --enable CONFIG_USERLED
  for symbol in USERLED_LOWER USERLED_LOWER_READSTATE USERLED_EFFECTS; do
    kconfig-tweak --disable "CONFIG_$symbol"
  done
  make olddefconfig
  grep -qx CONFIG_USERLED=y .config
  if grep -Eq '^CONFIG_(USERLED_LOWER|USERLED_LOWER_READSTATE|USERLED_EFFECTS)=y$' .config; then
    echo 'Unexpected physical/effects lower half in instrumented fixture' >&2
    exit 1
  fi
  cp .config "$OUT/resolved.config"
)
export NXRS_USERLED_PATH=/dev/nxrs-userled
export NXRS_PLATFORM=nuttx-userled
export CARGO_TARGET_DIR="$OUT/cargo"
export CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_STRIP=none CARGO_PROFILE_RELEASE_DEBUG=1
export NUTTX_STD_LINK_LOG="$OUT/rust-link.json"
export RUSTFLAGS="-C panic=abort -C linker=$ROOT/tests/nuttx-std/link.py"
"$CARGO_BIN" build --locked --release -p nxrs-std-userled --bin nxrs-std-userled \
  --target "$TARGET_ARG" -Zbuild-std=std,panic_abort \
  --message-format=json-render-diagnostics | tee "$OUT/cargo-messages.jsonl"
ELF="$CARGO_TARGET_DIR/$TARGET/release/nxrs-std-userled"
xtensa-esp32s3-elf-nm "$ELF" > "$OUT/rust-symbols.txt"
for symbol in main; do grep -Eq " T $symbol$" "$OUT/rust-symbols.txt"; done
for symbol in nxrs_userled_supported nxrs_userled_set nxrs_userled_fixture_install nxrs_userled_fixture_verify nxrs_userled_fixture_fds close; do
  grep -Eq " U $symbol$" "$OUT/rust-symbols.txt" || {
    echo "Missing USERLED import $symbol" >&2; exit 1;
  }
done
python3 tests/nuttx-std/check-abi.py --check-imports "$OUT/rust-symbols.txt"
python3 - "$OUT" <<'PY'
import json, os, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
messages = [json.loads(line) for line in (out / 'cargo-messages.jsonl').read_text().splitlines()]
std = [m for m in messages if m.get('reason') == 'compiler-artifact' and m['target']['name'] == 'std']
assert len(std) == 1
actual = pathlib.Path(std[0]['target']['src_path']).resolve()
expected = pathlib.Path(os.environ['NUTTX_STD_SYSROOT']) / 'lib/rustlib/src/rust/library/std/src/lib.rs'
assert actual == expected.resolve()
(out / 'std-source.json').write_text(json.dumps(dict(std_source=str(actual), selected_source_matches=True), indent=2) + '\n')
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
cfgs = [cfg for cfg in selection[0]['cfgs'] if cfg.startswith('nxrs_provider=')]
assert cfgs == ['nxrs_provider="nuttx"'], f'wrong LED platform selection: {cfgs}'
(out / 'userled-hal-selection.json').write_text(json.dumps({
    'package': 'nxrs-led', 'target': 'led', 'artifact': led_roots[0],
    'platform': os.environ['NXRS_PLATFORM'], 'cfgs': cfgs,
}, indent=2) + '\n')
PY
unset RUSTFLAGS
make -C "$OUT/nuttx" -j4 CROSSDEV=xtensa-esp32s3-elf- NXRS_STD_ELF="$ELF" ESPTOOL_BINDIR=.
python3 tests/nuttx-std/check-abi.py --out "$OUT" --target "$TARGET" --target-spec "$TARGET_ARG"
xtensa-esp32s3-elf-nm "$OUT/nuttx/nuttx" > "$OUT/symbols.txt"
for symbol in rust_std_main nx_start userled_register userled_ioctl nxrs_userled_supported nxrs_userled_set nxrs_userled_fixture_install; do
  grep -Eq " [TtWw] $symbol$" "$OUT/symbols.txt" || {
    echo "Missing final USERLED symbol $symbol" >&2; exit 1;
  }
done
xtensa-esp32s3-elf-size -A "$OUT/nuttx/nuttx" > "$OUT/image-size.txt"
xtensa-esp32s3-elf-readelf -h -l -A "$OUT/nuttx/nuttx" > "$OUT/image-layout.txt"
xtensa-esp32s3-elf-readelf -h "$ELF" > "$OUT/rust-elf-header.txt"
{
  echo "nxrs=$(git rev-parse HEAD)"
  echo "nuttx=$(git rev-parse HEAD:external/nuttx)"
  echo "apps=$(git rev-parse HEAD:external/nuttx-apps)"
  echo 'profile=userled-esp32s3'
  echo "nxrs_platform=$NXRS_PLATFORM"
  echo 'board=esp32s3-devkit:nsh'
  echo "target=$TARGET"
  echo "provider_path=$NXRS_USERLED_PATH"
  echo 'lower_half=instrumented-test-not-physical-gpio'
  "$RUSTC" --version --verbose
  xtensa-esp32s3-elf-gcc --version | head -n1
  qemu-system-xtensa --version | head -n1
} > "$OUT/provenance.txt"
sha256sum "$OUT/nuttx/nuttx" "$OUT/nuttx/nuttx.merged.bin" "$ELF" "$TARGET_ARG" > "$OUT/binaries.sha256"
sha256sum hal/led/Cargo.toml hal/led/src/lib.rs hal/led/src/api.rs \
  hal/led/src/providers/nuttx.rs hal/led/ffi/nuttx/nxrs_userled.c \
  platform/hal-build.rs tools/platform_config.py platform/profiles/nuttx-userled.toml \
  tests/nuttx-std/userled/src/main.rs tests/nuttx-std/userled/nuttx/fixture.c \
  tests/nuttx-std/userled/nuttx/Makefile tests/nuttx-std/userled/build.sh > "$OUT/userled-sources.sha256"
echo 'USERLED final image ready; runtime qualification is a separate required step.'
