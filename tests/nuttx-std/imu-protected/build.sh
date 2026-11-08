#!/usr/bin/env bash
# Cross-build a test-only protected image from the Nxrs-pinned source trees.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
if [[ $# != 1 ]]; then
  echo "Usage: $0 NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi

# Only stage into a new output directory. The source checkouts are read-only.
mkdir -- "$1"
OUT=$(cd "$1" && pwd -P)
NUTTX="$OUT/nuttx"
APPS="$OUT/apps"
mkdir -- "$NUTTX" "$APPS"
for entry in nuttx nuttx-apps; do
  destination="$NUTTX"
  [[ "$entry" == nuttx ]] || destination="$APPS"
  revision=$(git -C "$ROOT" rev-parse "HEAD:external/$entry")
  git -C "$ROOT/external/$entry" archive "$revision" | tar -xf - -C "$destination"
  printf '%s %s\n' "$entry" "$revision" >> "$OUT/sources.txt"
done

app="$APPS/examples/nxrs_imu_witness"
mkdir -- "$app"
cp "$HERE/i2c_probe.c" "$HERE/probe_main.c" "$app/"
cp "$ROOT/driver/i2c/src/platform/nuttx/ffi/i2c.c" \
   "$ROOT/driver/gpio/src/platform/nuttx/ffi/gpio.c" \
   "$ROOT/driver/acquisition/src/platform/nuttx/ffi/events.c" "$app/"
cat > "$app/Kconfig" <<'KCONFIG'
config EXAMPLES_NXRS_IMU_WITNESS
	bool "Nxrs protected IMU witness"
	default n
	depends on BUILD_PROTECTED
KCONFIG
cat > "$app/Make.defs" <<'MAKEDEFS'
ifeq ($(CONFIG_EXAMPLES_NXRS_IMU_WITNESS),y)
CONFIGURED_APPS += $(APPDIR)/examples/nxrs_imu_witness
endif
MAKEDEFS
cat > "$app/Makefile" <<'MAKEFILE'
include $(APPDIR)/Make.defs
PROGNAME = imu_witness
PRIORITY = 100
STACKSIZE = 4096
MODULE = $(CONFIG_EXAMPLES_NXRS_IMU_WITNESS)
MAINSRC = probe_main.c
CSRCS = i2c_probe.c i2c.c gpio.c events.c
include $(APPDIR)/Application.mk
MAKEFILE
board="$NUTTX/boards/xtensa/esp32s3/esp32s3-devkit/src"
cp "$HERE/i2c_lower.c" "$HERE/gpio_lower.c" "$board/"
cat >> "$board/Make.defs" <<'MAKEDEFS'

ifeq ($(CONFIG_EXAMPLES_NXRS_IMU_WITNESS),y)
CSRCS += gpio_lower.c i2c_lower.c
endif
MAKEDEFS
python3 - "$board/esp32s3_boot.c" <<'PY'
from pathlib import Path
import sys

boot = Path(sys.argv[1])
source = boot.read_text()
hook = "void board_late_initialize(void)\n{"
if source.count(hook) != 1:
    raise SystemExit("Expected one DevKit late initialization hook")
boot.write_text(source.replace(hook, """#ifdef CONFIG_EXAMPLES_NXRS_IMU_WITNESS
int imu_witness_i2c_register(void);
int imu_witness_gpio_register(void);
#endif

""" + hook + """
#ifdef CONFIG_EXAMPLES_NXRS_IMU_WITNESS
  if (imu_witness_i2c_register() < 0 || imu_witness_gpio_register() < 0)
    {
      return;
    }
#endif
""", 1))
PY

# NuttX will fetch its pinned vendor tree when this optional local cache is absent.
if [[ -n "${NXRS_IMU_WITNESS_HAL_CACHE:-}" ]]; then
  vendor="$NUTTX/arch/xtensa/src/esp32s3/esp-hal-3rdparty"
  pin=$(awk '$1 == "ESP_HAL_3RDPARTY_VERSION" { print $3 }' \
    "$NUTTX/arch/xtensa/src/esp32s3/Make.defs")
  git clone -q --shared --no-checkout "$NXRS_IMU_WITNESS_HAL_CACHE" "$vendor"
  git -C "$vendor" checkout -q --detach "$pin"
  submodule=components/mbedtls/mbedtls
  sub_pin=$(git -C "$vendor" rev-parse "HEAD:$submodule")
  git clone -q --shared --no-checkout "$NXRS_IMU_WITNESS_HAL_CACHE/$submodule" "$vendor/$submodule"
  git -C "$vendor/$submodule" checkout -q --detach "$sub_pin"
fi

cd "$NUTTX"
case $(uname -s) in
  Darwin) host=-m ;;
  Linux) host=-l ;;
  *) echo "Use a Linux or macOS host" >&2; exit 2 ;;
esac
tools/configure.sh "$host" -a ../apps esp32s3-devkit:knsh > "$OUT/configure.log" 2>&1
kconfig-tweak --file .config \
  --enable ESP32S3_I2C0 --enable I2C_DRIVER --enable DEV_GPIO --enable ESPRESSIF_GPIO_IRQ \
  --enable PIPES --enable BOARD_LATE_INITIALIZE --enable EXAMPLES_NXRS_IMU_WITNESS \
  --set-val DEV_GPIO_NPOLLWAITERS 1 --set-val DEV_GPIO_NSIGNALS 0 \
  --set-str INIT_ENTRYPOINT imu_witness_main --set-str INIT_ENTRYNAME imu_witness_main
config_args=()
if command -v olddefconfig >/dev/null; then
  config_args=(KCONFIG_OLDDEFCONFIG=olddefconfig)
fi
make "${config_args[@]}" olddefconfig >> "$OUT/configure.log" 2>&1
for expected in CONFIG_BUILD_PROTECTED=y CONFIG_BUILD_2PASS=y \
  CONFIG_EXAMPLES_NXRS_IMU_WITNESS=y CONFIG_DEV_GPIO_NPOLLWAITERS=1 \
  CONFIG_DEV_GPIO_NSIGNALS=0 CONFIG_BOARD_LATE_INITIALIZE=y \
  CONFIG_PIPES=y CONFIG_DEV_GPIO=y CONFIG_I2C_DRIVER=y \
  'CONFIG_INIT_ENTRYPOINT="imu_witness_main"'; do
  grep -Fxq "$expected" .config || { echo "Missing effective setting $expected" >&2; exit 1; }
done

# Reproduce the pinned ESP32-S3 PREBUILD userspace address calculation directly.
# The stock hook applies the same value via kconfig-merge.
kernel_size=$(sed -n 's/^CONFIG_ESP32S3_KERNEL_IMAGE_SIZE=//p' .config)
[[ "$kernel_size" =~ ^0x[0-9a-fA-F]+$ ]]
user_address=$(printf '0x%x' "$((0x3c000030 + kernel_size))")
kconfig-tweak --file .config --set-val NUTTX_USERSPACE "$user_address"
if ! make PREBUILD= -j"${NXRS_IMU_WITNESS_JOBS:-4}" > "$OUT/build.log" 2>&1; then
  tail -n 40 "$OUT/build.log" >&2
  exit 1
fi
grep -Fxq "#define CONFIG_NUTTX_USERSPACE $user_address" include/nuttx/config.h

xtensa-esp32s3-elf-nm --defined-only nuttx > "$OUT/kernel-symbols.txt"
xtensa-esp32s3-elf-nm --defined-only nuttx_user.elf > "$OUT/user-symbols.txt"
check_symbol_placement() {
  local symbol=$1 present=$2 absent=$3
  if ! grep -Eq " [Tt] $symbol$" "$OUT/$present-symbols.txt"; then
    echo "Missing $symbol in $present ELF" >&2
    return 1
  fi
  if [[ -n "$absent" ]] && grep -Eq " [Tt] $symbol$" "$OUT/$absent-symbols.txt"; then
    echo "Unexpected $symbol in $absent ELF" >&2
    return 1
  fi
}
for symbol in imu_witness_main imu_witness_i2c i2c_resource_transfer gpio_resource_irq_check \
  gpio_resource_irq_register gpio_resource_irq_read gpio_resource_irq_unregister acquisition_event_pipe acquisition_event_wait acquisition_event_tick; do
  check_symbol_placement "$symbol" user kernel
done
check_symbol_placement ioctl user ''
check_symbol_placement poll user ''
for symbol in ioctl poll; do
  xtensa-esp32s3-elf-objdump -d --disassemble="$symbol" nuttx_user.elf > "$OUT/$symbol.txt"
  grep -Eq '[[:space:]]syscall[[:space:]]*$' "$OUT/$symbol.txt"
done
for symbol in imu_witness_i2c_register imu_witness_gpio_register; do
  check_symbol_placement "$symbol" kernel user
done
test -s nuttx.bin
test -s nuttx_user.bin
xtensa-esp32s3-elf-gcc --version > "$OUT/toolchain.txt"
cp .config "$OUT/resolved.config"
shasum -a 256 nuttx nuttx.bin nuttx_user.elf nuttx_user.bin > "$OUT/images.sha256"
printf 'Protected ESP32-S3 IMU witness cross-build passed. Evidence: %s\n' "$OUT"
