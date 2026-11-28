#!/usr/bin/env bash
# Package the browser-only tracker peer beside the ESP-EMU and NuttX assets.
set -euo pipefail

if [[ $# != 6 && $# != 7 ]]; then
  echo "usage: $0 ESP_EMU_WWW PATCHED_PKG NUTTX_ELF MBEDTLS_ARCHIVE JSQR_ARCHIVE OUTPUT [SITE_ARCHIVE]" >&2
  exit 2
fi

root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/tests/browser/tracker-demo"
www=$1
patch=$2
elf=$3
tls_archive=$4
qr_archive=$5
mkdir -p "$6"
out=$(cd "$6" && pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT

tls_pin="$root/upstream/mbedtls/source.json"
tls_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$tls_pin")
tls_hash=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$tls_pin")
qr_hash=b5299b37917a1fe7a8cab9dd5cc6b8accf82663add80abe5bf7761a921cc6602
for input in "$tls_archive" "$qr_archive"; do
  actual=$(shasum -a 256 "$input" | cut -d ' ' -f 1)
  if [[ "$input" == "$tls_archive" ]]; then expected=$tls_hash; else expected=$qr_hash; fi
  if [[ "$actual" != "$expected" ]]; then
    echo "unexpected archive hash for $input" >&2
    exit 1
  fi
done

firmware=$(dirname "$elf")
if ! grep -qx 'CONFIG_SMP=y' "$firmware/.config" || ! grep -qx 'CONFIG_SMP_NCPUS=2' "$firmware/.config"; then
  echo "expected a two-core NuttX firmware build" >&2
  exit 1
fi
"${NXRS_TRACKER_NM:-xtensa-esp32s3-elf-nm}" "$elf" > "$build/symbols"
awk '
  BEGIN { print "export default {" }
  $3 ~ /^tracker_(pixels|colors|frame_sequence|model_error|touch_input|touch_reads)$/ {
    printf "  %s: 0x%s,\n", $3, $1
    count++
  }
  END { print "};"; if (count != 6) exit 1 }
' "$build/symbols" > "$out/symbols.mjs"

tar -xjf "$tls_archive" -C "$build"
sdk="$build/mbedtls-$tls_version"
emcmake cmake -S "$sdk" -B "$build/sdk" -DCMAKE_BUILD_TYPE=MinSizeRel \
  -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF -DGEN_FILES=OFF \
  "-DTF_PSA_CRYPTO_USER_CONFIG_FILE=$src/crypto-config.h"
cmake --build "$build/sdk" --target mbedtls --parallel "${NXRS_TRACKER_JOBS:-8}"
libraries="$build/sdk/library"
emcc "$src/tls-peer.c" -I "$sdk/include" -I "$sdk/tf-psa-crypto/include" \
  -I "$sdk/tf-psa-crypto/drivers/builtin/include" \
  "-DTF_PSA_CRYPTO_USER_CONFIG_FILE=\"$src/crypto-config.h\"" \
  "$libraries/libmbedtls.a" "$libraries/libmbedx509.a" "$libraries/libtfpsacrypto.a" \
  -Os --no-entry -sENVIRONMENT=web,worker -sMODULARIZE=1 -sEXPORT_ES6=1 \
  -sFILESYSTEM=0 -sEXPORTED_RUNTIME_METHODS=HEAPU8 -o "$out/tls-client.mjs"

tar -xzf "$qr_archive" -C "$build" package/dist/jsQR.js package/LICENSE
cp "$build/package/dist/jsQR.js" "$out/"
cp "$patch/esp_emu.js" "$patch/esp_emu_bg.wasm" "$patch/browser_devices.js" "$out/"
cp "$www/vendor/esp_cosim_pb.js" "$firmware/nuttx.merged.bin" "$out/"
for file in index.html style.css app.mjs worker.mjs h4.mjs gnss.mjs panel.mjs secure-stream.mjs touchscreen.mjs serve.mjs; do
  cp "$src/$file" "$out/"
done
mkdir -p "$out/licenses"
cp "$www/pkg/LICENSE" "$out/licenses/ESP-EMU.txt"
cp "$sdk/LICENSE" "$out/licenses/Mbed-TLS.txt"
cp "$sdk/tf-psa-crypto/LICENSE" "$out/licenses/TF-PSA-Crypto.txt"
cp "$build/package/LICENSE" "$out/licenses/jsQR.txt"
cp "$root/LICENSE" "$out/licenses/NXRS.txt"
cp "$root/external/nuttx/LICENSE" "$out/licenses/NuttX-LICENSE.txt"
cp "$root/external/nuttx/NOTICE" "$out/licenses/NuttX-NOTICE.txt"
cp "$root/external/nuttx-apps/LICENSE" "$out/licenses/NuttX-Apps-LICENSE.txt"
cp "$root/external/nuttx-apps/NOTICE" "$out/licenses/NuttX-Apps-NOTICE.txt"
if [[ $# == 7 ]]; then
  mkdir -p "$(dirname "$7")"
  COPYFILE_DISABLE=1 tar -czf "$7" -C "$out" \
    esp_emu.js esp_emu_bg.wasm esp_cosim_pb.js jsQR.js nuttx.merged.bin symbols.mjs \
    tls-client.mjs tls-client.wasm licenses/ESP-EMU.txt licenses/Mbed-TLS.txt \
    licenses/TF-PSA-Crypto.txt licenses/jsQR.txt licenses/NXRS.txt licenses/NuttX-LICENSE.txt \
    licenses/NuttX-NOTICE.txt licenses/NuttX-Apps-LICENSE.txt licenses/NuttX-Apps-NOTICE.txt
fi
printf 'Standalone browser demo: %s\n' "$out"
