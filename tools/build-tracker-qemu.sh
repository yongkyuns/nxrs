#!/usr/bin/env bash
# The tracker needs Espressif's UART2 backend and a local SLiRP peer.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/target/tracker-qemu"}
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
stage=$(mktemp -d "$out/build.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT

unpack() {
  local repository=$1 revision=$2 hash=$3 destination=$4
  local archive="$out/${repository##*/}-$revision.tar.gz"
  if [[ ! -f "$archive" ]] || ! printf '%s  %s\n' "$hash" "$archive" | shasum -a 256 -c --status; then
    curl --proto '=https' -fsSL --retry 3 \
      "https://codeload.github.com/$repository/tar.gz/$revision" -o "$stage/download"
    printf '%s  %s\n' "$hash" "$stage/download" | shasum -a 256 -c -
    mv "$stage/download" "$archive"
  fi
  mkdir -p "$destination"
  tar -xzf "$archive" --strip-components=1 -C "$destination"
}

source_dir="$stage/source"
unpack espressif/qemu 7411393a2f61e6f0b05af00276d30780769e0839 \
  ffa041a08d2eb964efbcc15e0b0fc3b5141344757f5ee22ab4e89f0c4083cf20 "$source_dir"
unpack qemu/keycodemapdb f5772a62ec52591ff6870b7e8ef32482371f22c6 \
  d014b53382dbb17b8196ad12f50de7f20d0ef1b9f7d54b0be51a6cbb14209195 "$source_dir/subprojects/keycodemapdb"
unpack qemu/berkeley-softfloat-3 b64af41c3276f97f0e181920400ee056b9c88037 \
  faae889814ea6a292f7ca03d9b36e6c7e95bab2a64777804883cc822b8d48757 "$source_dir/subprojects/berkeley-softfloat-3"
unpack qemu/berkeley-testfloat-3 e7af9751d9f9fd3b47911f51a5cfd08af256a9ab \
  e7a09d51dc7e96cb8422c919c85fc3833d4f788567638c9162ef5cd7db59b1df "$source_dir/subprojects/berkeley-testfloat-3"
for project in berkeley-softfloat-3 berkeley-testfloat-3; do
  cp -R "$source_dir/subprojects/packagefiles/$project/." "$source_dir/subprojects/$project/"
done
GIT_CEILING_DIRECTORIES="$stage" git -C "$source_dir" apply --verbose "$root"/upstream/qemu/patches/*.patch

mkdir "$stage/build"
cd "$stage/build"
"$source_dir/configure" --target-list=xtensa-softmmu --without-default-features \
  --enable-tcg --enable-gcrypt --enable-fdt=system --enable-slirp \
  --disable-download --disable-docs --disable-werror
build_jobs=()
if [[ -n ${NXRS_QEMU_BUILD_JOBS:-} ]]; then
  build_jobs=(-j "$NXRS_QEMU_BUILD_JOBS")
fi
ninja "${build_jobs[@]}" qemu-system-xtensa
install -m 755 qemu-system-xtensa "$out/"
install -m 644 "$source_dir/pc-bios/esp32s3_rev0_rom.bin" "$out/"
