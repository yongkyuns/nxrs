#!/usr/bin/env bash
# Build the C-only browser scheduler witness in a fresh NuttX tree.
set -euo pipefail

if [[ $# != 2 || ! -d "$1/components/esp_system" ]]; then
  echo "usage: bash $0 ESP_HAL_3RDPARTY_SOURCE OUTPUT_DIRECTORY" >&2
  exit 2
fi

root=$(cd "$(dirname "$0")/.." && pwd)
esp_hal=$(cd "$1" && pwd)
mkdir -p "$2"
out=$(cd "$2" && pwd)
mkdir "$out/nuttx" "$out/apps"

git -C "$root/external/nuttx" archive HEAD | tar -xf - -C "$out/nuttx"
git -C "$root/external/nuttx-apps" archive HEAD | tar -xf - -C "$out/apps"
chip="$out/nuttx/arch/xtensa/src/esp32s3"
cp -R "$esp_hal" "$chip/esp-hal-3rdparty"
# A prepared SDK can contain objects for a single-core build.
find "$chip/esp-hal-3rdparty" -type f -name '*.o' -delete

board="$out/nuttx/boards/xtensa/esp32s3/esp32s3-devkit/src"
cp "$root/tests/browser/esp32s3-preemption/preemption.c" "$board/preemption.c"
printf '\nCSRCS += preemption.c\n' >> "$board/Make.defs"

cd "$out/nuttx"
host=-l
if [[ $(uname -s) == Darwin ]]; then
  host=-m
fi
./tools/configure.sh "$host" -a ../apps esp32s3-devkit:smp KCONFIG_OLDDEFCONFIG=olddefconfig
kconfig-tweak --file .config \
  -e ESP32S3_QEMU_IMAGE -e DEBUG_ASSERTIONS -e DEBUG_ERROR -e DEBUG_FEATURES \
  -d SYSTEM_NXINIT -d TESTING_GETPRIME -d TESTING_OSTEST -d TESTING_SMP \
  -d RTC -d SCHED_TICKLESS -d NSH_PIPELINE \
  --set-str INIT_ENTRYPOINT preemption_main --set-str INIT_ENTRYNAME preemption_main \
  --set-val USEC_PER_TICK 1000
make olddefconfig KCONFIG_OLDDEFCONFIG=olddefconfig
make -j "${NXRS_NUTTX_BUILD_JOBS:-8}" CROSSDEV=xtensa-esp32s3-elf-
