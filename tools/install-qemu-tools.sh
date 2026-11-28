#!/usr/bin/env bash
# Reproducible Linux/x86-64 compiler + emulator bundle, isolated from espup.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="$ROOT/target/qemu-tools"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'Linux x86_64 required' >&2; exit 1; }
mkdir -p "$TOOLS"
fetch_unpack() {
  local name=$1 url=$2 digest=$3
  local archive="$TOOLS/$name.tar.xz"
  if [[ ! -f "$archive" ]] || ! echo "$digest  $archive" | sha256sum --check --status; then
    curl --fail --location --retry 3 "$url" -o "$archive.tmp"
    echo "$digest  $archive.tmp" | sha256sum --check
    mv "$archive.tmp" "$archive"
  fi
  # Check cached input too; never trust a cache hit as download verification.
  echo "$digest  $archive" | sha256sum --check
  rm -rf "$TOOLS/$name"
  mkdir -p "$TOOLS/$name"
  tar -xJf "$archive" -C "$TOOLS/$name"
}
fetch_unpack qemu \
  https://github.com/espressif/qemu/releases/download/esp-develop-9.2.2-20261109/qemu-xtensa-softmmu-esp_develop_9.2.2_20261109-x86_64-linux-gnu.tar.xz \
  fbc3bffa9fa71192847969a627a584aabe2e86d85bed4902a096ff06c2b8a815
fetch_unpack gcc \
  https://github.com/espressif/crosstool-NG/releases/download/esp-16.2.0_20261109/xtensa-esp-elf-16.2.0_20261109-x86_64-linux-gnu.tar.xz \
  085098d02d56c7b2c8942ccb3c934b610adb57aa5b687e3fc1fc4e26b8f2fe40
fetch_unpack rust \
  https://github.com/esp-rs/rust-build/releases/download/v1.100.0.0/rust-1.100.0.0-x86_64-unknown-linux-gnu.tar.xz \
  5a9fad3e2f842dd72719e47e38d58d76dfb8e26074c641083e02d7f66a340a0c
fetch_unpack rust-src \
  https://github.com/esp-rs/rust-build/releases/download/v1.100.0.0/rust-src-1.100.0.0.tar.xz \
  bec4a2e7d914c237391f9dda1f530975d92fda6fb15b3c7ccc3053cb116f9d4e
# Use the distribution installer: rustc and host rust-std are SEPARATE
# components in this archive. Pointing RUSTC into just rustc/ cannot build
# Cargo build scripts, even though the target application is no_std.
INSTALLER=$(find "$TOOLS/rust" -maxdepth 2 -name install.sh -print -quit)
[[ -f "$INSTALLER" ]]
RUST_ROOT="$TOOLS/rust-installed"
rm -rf "$RUST_ROOT"
bash "$INSTALLER" --prefix="$RUST_ROOT" --disable-ldconfig \
  --components=rustc,rust-std-x86_64-unknown-linux-gnu
RUSTC_PATH="$RUST_ROOT/bin/rustc"
GCC_PATH=$(find "$TOOLS/gcc" -path '*/bin/xtensa-esp-elf-gcc' -print -quit)
QEMU_PATH=$(find "$TOOLS/qemu" -path '*/bin/qemu-system-xtensa' -type f -print -quit)
CORE_MANIFEST=$(find "$TOOLS/rust-src" -path '*/library/core/Cargo.toml' -print -quit)
[[ -x "$RUSTC_PATH" && -x "$GCC_PATH" && -x "$QEMU_PATH" && -f "$CORE_MANIFEST" ]]
LIBRARY=$(dirname "$(dirname "$CORE_MANIFEST")")
mkdir -p "$RUST_ROOT/lib/rustlib/src/rust"
ln -sfn "$LIBRARY" "$RUST_ROOT/lib/rustlib/src/rust/library"
# Cargo is the separately pinned upstream 1.100.0; only rustc/rust-src use the
# Xtensa fork. No floating espup installer or global toolchain replacement.
{
  printf 'export RUSTUP_TOOLCHAIN=1.100.0\n'
  printf 'export RUSTC=%q\n' "$RUSTC_PATH"
  printf 'export RUSTDOC=%q\n' "$RUST_ROOT/bin/rustdoc"
  printf 'export PATH=%q:%q:"$PATH"\n' "$(dirname "$GCC_PATH")" "$(dirname "$QEMU_PATH")"
} > "$TOOLS/environment.sh"
source "$TOOLS/environment.sh"
"$RUSTC" --version --verbose
printf 'fn main() {}\n' | "$RUSTC" --crate-name nxrs_host_probe - -o "$TOOLS/host-probe"
"$TOOLS/host-probe"
xtensa-esp32s3-elf-gcc --version
qemu-system-xtensa --version
qemu-system-xtensa -machine help | grep esp32s3
sha256sum "$TOOLS/"*.tar.xz > "$TOOLS/downloads.sha256"
