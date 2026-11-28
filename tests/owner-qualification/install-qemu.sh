#!/usr/bin/env bash
# Private ARM emulator for this fixture; does not replace system/ESP32 QEMU.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
QEMU_CACHE="${1:-$ROOT/target/owner-qemu-tools}"
QEMU_VERSION=8.2.7
QEMU_URL="https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
# The official release signature was verified against Michael Roth's key:
# CEACC9E15534EBABB82D3FA03353C9CEF108B584 (qemu.org/download/#source).
QEMU_SHA256=1f0604f296ab9acb4854c054764a1ba408643fc299bd54a6500cccfaaca65b55
mkdir -p "$QEMU_CACHE"
QEMU_CACHE="$(cd "$QEMU_CACHE" && pwd)"
ARCHIVE="$QEMU_CACHE/qemu-$QEMU_VERSION.tar.xz"
if [[ ! -f "$ARCHIVE" ]] || ! echo "$QEMU_SHA256  $ARCHIVE" | sha256sum --check --status; then
  curl --fail --location --retry 3 "$QEMU_URL" -o "$ARCHIVE.tmp"
  echo "$QEMU_SHA256  $ARCHIVE.tmp" | sha256sum --check
  mv "$ARCHIVE.tmp" "$ARCHIVE"
fi
# Verify cached bytes too. Only the source archive is reused across CI runs.
echo "$QEMU_SHA256  $ARCHIVE" | sha256sum --check
WORK="$(mktemp -d "$QEMU_CACHE/build.XXXXXXXX")"
mkdir "$WORK/source" "$WORK/build"
tar -xJf "$ARCHIVE" --strip-components=1 -C "$WORK/source"
OPTIONS=(--target-list=arm-softmmu --without-default-features --enable-tcg
         --enable-pixman --disable-download --disable-docs --disable-tools
         --disable-guest-agent --disable-debug-info --disable-werror)
(
  cd "$WORK/build"
  "$WORK/source/configure" "${OPTIONS[@]}"
  ninja -j "${QEMU_BUILD_JOBS:-2}" qemu-system-arm
) 2>&1 | tee "$QEMU_CACHE/build.log"
install -m 755 "$WORK/build/qemu-system-arm" "$QEMU_CACHE/qemu-system-arm.tmp"
mv "$QEMU_CACHE/qemu-system-arm.tmp" "$QEMU_CACHE/qemu-system-arm"
"$QEMU_CACHE/qemu-system-arm" --version
"$QEMU_CACHE/qemu-system-arm" -machine help > "$QEMU_CACHE/machines.txt"
grep -q '^mps2-an521 ' "$QEMU_CACHE/machines.txt"
{
  printf 'qemu_version=%s\nsource_url=%s\n' "$QEMU_VERSION" "$QEMU_URL"
  printf 'upstream_uart_fix=5a558be93ad628e5bed6e0ee062870f49251725c\n'
  printf 'configure_options='; printf '%q ' "${OPTIONS[@]}"; printf '\n'
  "${CC:-cc}" --version
  "$QEMU_CACHE/qemu-system-arm" --version
  sha256sum "$ARCHIVE" "$QEMU_CACHE/qemu-system-arm" "$0"
} > "$QEMU_CACHE/receipt.txt"
# Only this invocation's temporary source/objects are retired. Failed builds
# retain their directory and log for diagnosis; no global tool/cache is edited.
rm -rf -- "$WORK"
printf 'ARM fixture emulator: %s/qemu-system-arm\n' "$QEMU_CACHE"
