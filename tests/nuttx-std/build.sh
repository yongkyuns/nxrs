#!/usr/bin/env bash
# Build the shared ordinary-main probe, or its separate resource test image.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
test "$#" -le 1 || { echo "usage: $0 [threads|resources|pico2|armv8m-qemu|esp32s3|integration-esp32s3|udp]" >&2; exit 1; }
PROFILE="${1:-threads}"
unset NXRS_PLATFORM
TARGET=riscv32imac-unknown-nuttx-elf
CROSSDEV=riscv64-unknown-elf-
BOARD=rv-virt:nsh
MACHINE=RISC-V
CODE_MODEL=(-C code-model=medium)
ESP32=0
INTEGRATION=0
APP_SYMBOL=EXAMPLES_NXRS_STD
case "$PROFILE" in
  threads|pico2|armv8m-qemu|esp32s3)
    OUT="$ROOT/target/nuttx-std"
    BIN=nxrs-browser-threads
    CARGO_ARGS=(-p "$BIN")
    PROBE_SOURCE=tests/browser-threads/src/main.rs
    ENTRY=rust_std_main
    if test "$PROFILE" = pico2; then
      OUT="$ROOT/target/nuttx-pico2"
      TARGET=thumbv8m.main-nuttx-eabi
      CROSSDEV=arm-none-eabi-
      BOARD=raspberrypi-pico-2:nsh
      MACHINE=ARM
      CODE_MODEL=()
    elif test "$PROFILE" = armv8m-qemu; then
      OUT="$ROOT/target/nuttx-armv8m-qemu-std"
      TARGET=thumbv8m.main-nuttx-eabi
      CROSSDEV=arm-none-eabi-
      BOARD=mps2-an521:nsh
      MACHINE=ARM
      CODE_MODEL=()
    elif test "$PROFILE" = esp32s3; then
      OUT="$ROOT/target/nuttx-esp32s3-std"
      TARGET=xtensa-esp32s3-nuttx
      CROSSDEV=xtensa-esp32s3-elf-
      BOARD=esp32s3-devkit:nsh
      MACHINE='Tensilica Xtensa Processor'
      CODE_MODEL=()
      ESP32=1
    fi
    ;;
  integration-esp32s3)
    OUT="$ROOT/target/nuttx-qemu"
    BIN=nxrs-nuttx-std-app
    CARGO_ARGS=(-p nxrs-nuttx-app --features std-integration)
    PROBE_SOURCE=tests/nuttx/src/main.rs
    ENTRY=rc_rust_std_main
    TARGET=xtensa-esp32s3-nuttx
    CROSSDEV=xtensa-esp32s3-elf-
    BOARD=esp32s3-devkit:nsh
    MACHINE='Tensilica Xtensa Processor'
    CODE_MODEL=()
    ESP32=1
    INTEGRATION=1
    APP_SYMBOL=EXAMPLES_NXRS_SIM
    NXRS_PLATFORM=nuttx-std
    ;;
  udp)
    OUT="$ROOT/target/nuttx-udp"
    BIN=nxrs-std-udp
    CARGO_ARGS=(-p "$BIN")
    PROBE_SOURCE=tests/nuttx-std/udp/src/main.rs
    ENTRY=rust_std_main
    NXRS_PLATFORM=nuttx-std
    ;;
  resources)
    OUT="$ROOT/target/nuttx-resources"
    BIN=nxrs-std-resources
    CARGO_ARGS=(--manifest-path "$ROOT/tests/nuttx-std/resources/Cargo.toml")
    PROBE_SOURCE=tests/nuttx-std/resources/src/main.rs
    ENTRY=rust_resources_probe_main
    ;;
  *) echo "unknown qualification profile: $PROFILE" >&2; exit 1 ;;
esac
export RUSTUP_TOOLCHAIN=nightly-2025-09-15
SDK=nightly-2025-09-15
if test "$ESP32" = 1; then
  source "$ROOT/target/qemu-tools/environment.sh"
  export RUSTC_BOOTSTRAP=1
  SDK=esp-1.90.0.0
fi
SOURCE_RUSTC="${RUSTC:-rustc}"
for cmd in cargo rustc git make kconfig-tweak "${CROSSDEV}gcc" "${CROSSDEV}objcopy" "${CROSSDEV}readelf" "${CROSSDEV}nm" "${CROSSDEV}size"; do
  command -v "$cmd" >/dev/null || { echo "Missing prerequisite: $cmd" >&2; exit 1; }
done
if test "$ESP32" = 1; then
  command -v qemu-system-xtensa >/dev/null
  command -v esptool.py >/dev/null
elif test "$PROFILE" = armv8m-qemu; then
  command -v qemu-system-arm >/dev/null || { echo "Missing prerequisite: qemu-system-arm" >&2; exit 1; }
elif test "$PROFILE" != pico2; then
  command -v qemu-system-riscv32 >/dev/null || { echo "Missing prerequisite: qemu-system-riscv32" >&2; exit 1; }
fi
mkdir -p "$OUT"
rm -f "$OUT/std-parker.patch" "$OUT/std-fd-sanitization.patch" "$OUT/std-patch.json" "$OUT/std-sigign.patch"
rm -rf "$OUT/nuttx" "$OUT/apps" "$OUT/cargo" "$OUT/toolchain"
mkdir -p "$OUT/nuttx" "$OUT/apps"
for pair in nuttx:nuttx nuttx-apps:apps; do
  source_name=${pair%:*}; destination=${pair#*:}
  expected=$(git -C "$ROOT" rev-parse "HEAD:external/$source_name")
  actual=$(git -C "$ROOT/external/$source_name" rev-parse HEAD)
  test "$expected" = "$actual" || { echo "Unpinned $source_name" >&2; exit 1; }
  git -C "$ROOT/external/$source_name" archive "$expected" | tar -x -C "$OUT/$destination"
  python3 "$ROOT/tools/apply-nuttx-patches.py" \
    --component "$source_name" --source "$OUT/$destination" --revision "$expected" \
    --record "$OUT/$source_name-patches.json"
done
if test "$INTEGRATION" = 1; then
  APP="$OUT/apps/examples/nxrs_sim"
  mkdir -p "$APP"
  cp "$ROOT/platform/nuttx/qualification/"* "$APP/"
  cp "$ROOT/tests/nuttx/c/"* "$APP/"
  cp "$ROOT/hal/camera/ffi/nuttx/"{camera.c,camera.h} "$APP/"
  cp "$ROOT/hal/storage/ffi/nuttx/"{storage.c,storage.h} "$APP/"
  cp "$ROOT/hal/support/nuttx/ffi/"{nuttx_support.c,nuttx_support.h} "$APP/"
  rm -f "$APP/transport.c" "$APP/transport.h"
else
  mkdir -p "$OUT/apps/examples/nxrs_std"
  cp "$ROOT/tests/nuttx-std/nuttx/"* "$OUT/apps/examples/nxrs_std/"
  if test "$PROFILE" = resources; then
    cp "$ROOT/tests/nuttx-std/resources/nuttx/"* "$OUT/apps/examples/nxrs_std/"
  fi
fi
cd "$OUT/nuttx"
./tools/configure.sh -l "$BOARD"
for symbol in EXAMPLES_HELLO TESTING_OSTEST TESTING_GETPRIME ARCH_FPU SIG_DEFAULT; do
  kconfig-tweak --disable "CONFIG_$symbol"
done
if test "$PROFILE" = pico2; then
  # This first board gate emits an ELF/bin, not an unqualified UF2 package.
  kconfig-tweak --disable CONFIG_RP23XX_UF2_BINARY
  kconfig-tweak --enable CONFIG_RAW_BINARY
elif test "$PROFILE" = armv8m-qemu; then
  # The stock MPS2 nsh profile enables DEBUG_SCHED. Together with
  # SYSTEM_TIME64, NuttX intentionally starts the scheduler tick counter
  # five seconds before the 32-bit wrap. That is a scheduler wrap stress
  # test, not representative runtime configuration, and it breaks MPS2
  # watchdog-backed pthread timed waits under QEMU.
  for symbol in NET SMP DISABLE_PTHREAD DEBUG_SCHED DEBUG_SCHED_ERROR; do
    kconfig-tweak --disable "CONFIG_$symbol"
  done
  kconfig-tweak --set-val CONFIG_PTHREAD_STACK_DEFAULT 65536
elif test "$ESP32" = 1; then
  for symbol in ESP32S3_QEMU_IMAGE DEBUG_FEATURES DEBUG_ASSERTIONS DEBUG_SYMBOLS DEBUG_LINK_MAP STACK_COLORATION; do
    kconfig-tweak --enable "CONFIG_$symbol"
  done
  if test "$INTEGRATION" = 1; then
    for symbol in NETDEV_LATEINIT FS_TMPFS NET NET_IPv4 NET_UDP NET_LOOPBACK NET_SOCKOPTS       NET_UDP_WRITE_BUFFERS NET_READAHEAD SCHED_HPWORK SCHED_LPWORK EXAMPLES_NXRS_PREEMPTION; do
      kconfig-tweak --enable "CONFIG_$symbol"
    done
    for symbol in NET_IPv6 NET_TCP NET_USRSOCK NET_ETHERNET NSH_NETINIT NETUTILS_NETINIT       SMP DISABLE_PTHREAD ESP32S3_WIFI ESP32S3_BLE ESP32S3_SPIRAM; do
      kconfig-tweak --disable "CONFIG_$symbol"
    done
    kconfig-tweak --set-val CONFIG_NET_RECV_BUFSIZE 4096
    kconfig-tweak --set-val CONFIG_PTHREAD_STACK_DEFAULT 8192
  else
    for symbol in NET SMP DISABLE_PTHREAD NSH_NETINIT NETUTILS_NETINIT NET_USRSOCK ESP32S3_WIFI ESP32S3_BLE ESP32S3_SPIRAM NET_ETHERNET; do
      kconfig-tweak --disable "CONFIG_$symbol"
    done
  fi
  kconfig-tweak --set-val CONFIG_ARCH_INTERRUPTSTACK 4096
fi
if test "$PROFILE" = udp; then
  # Test only the native loopback stack: no radio, Ethernet, proxy or DNS.
  for symbol in NET NET_IPv4 NET_UDP NET_LOOPBACK NET_SOCKOPTS NETDEV_LATEINIT \
    NET_UDP_WRITE_BUFFERS NET_READAHEAD SCHED_HPWORK SCHED_LPWORK; do
    kconfig-tweak --enable "CONFIG_$symbol"
  done
  for symbol in NET_IPv6 NET_TCP NET_USRSOCK NET_ETHERNET NSH_NETINIT NETUTILS_NETINIT SMP; do
    kconfig-tweak --disable "CONFIG_$symbol"
  done
  kconfig-tweak --set-val CONFIG_NET_RECV_BUFSIZE 4096
fi
for symbol in "$APP_SYMBOL" SYSTEM_TIME64 FS_LARGEFILE DEV_URANDOM SCHED_WAITPID SCHED_HAVE_PARENT SCHED_CHILD_STATUS NSH_DISABLEBG NSH_ARGCAT; do
  kconfig-tweak --enable "CONFIG_$symbol"
done
kconfig-tweak --set-val CONFIG_TLS_NELEM 16
kconfig-tweak --set-val CONFIG_TLS_NCLEANUP 16
kconfig-tweak --set-val CONFIG_RR_INTERVAL 10
make olddefconfig
# Retain resolved settings even when a required option fails validation.
cp .config "$OUT/resolved.config"
if test "$PROFILE" = resources; then
  grep -qx 'CONFIG_RAM_SIZE=33554432' .config || { echo "Resource probe requires the pinned 32-MiB heap profile" >&2; exit 1; }
elif test "$PROFILE" = pico2; then
  for required in CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2=y CONFIG_RAM_SIZE=532480 CONFIG_RAW_BINARY=y '# CONFIG_ARCH_FPU is not set'; do
    grep -qx "$required" .config || { echo "Pico 2 profile mismatch: $required" >&2; exit 1; }
  done
  # Invisible disabled Kconfig symbols may be absent, not an explicit unset line.
  if grep -qx "CONFIG_SMP=y" .config; then
    echo "Pico 2 qualification requires SMP disabled" >&2; exit 1
  fi
elif test "$PROFILE" = armv8m-qemu; then
  for required in CONFIG_ARCH_BOARD_MPS2_AN521=y CONFIG_ARCH_CHIP_MPS2_AN521=y CONFIG_RAM_SIZE=2097152 CONFIG_PTHREAD_STACK_DEFAULT=65536; do
    grep -qx "$required" .config || { echo "ARMv8-M QEMU profile mismatch: $required" >&2; exit 1; }
  done
  if grep -Eq '^CONFIG_(ARCH_FPU|SMP|NET|DISABLE_PTHREAD|DEBUG_SCHED|DEBUG_SCHED_ERROR)=y$' .config; then
    echo 'Unexpected FPU, SMP, network, disabled pthreads, or scheduler wrap-stress debug mode in ARMv8-M QEMU profile' >&2
    exit 1
  fi
elif test "$ESP32" = 1; then
  for required in CONFIG_ARCH_XTENSA=y CONFIG_ARCH_CHIP_ESP32S3=y CONFIG_ESP32S3_QEMU_IMAGE=y CONFIG_ESPRESSIF_SIMPLE_BOOT=y CONFIG_ARCH_INTERRUPTSTACK=4096; do
    grep -qx "$required" .config || { echo "ESP32-S3 profile mismatch: $required" >&2; exit 1; }
  done
  if test "$INTEGRATION" = 1; then
    for required in CONFIG_FS_TMPFS=y CONFIG_NET=y CONFIG_NET_IPv4=y CONFIG_NET_UDP=y CONFIG_NET_LOOPBACK=y CONFIG_NETDEV_LATEINIT=y CONFIG_EXAMPLES_NXRS_PREEMPTION=y CONFIG_NET_RECV_BUFSIZE=4096; do
      grep -qx "$required" .config || { echo "ESP32-S3 integration mismatch: $required" >&2; exit 1; }
    done
    if grep -Eq '^CONFIG_(ARCH_SIM|SMP|DISABLE_PTHREAD|NET_USRSOCK|NET_ETHERNET|NSH_NETINIT|NETUTILS_NETINIT|ESP32S3_WIFI|ESP32S3_BLE|ESP32S3_SPIRAM)=y$' .config; then
      echo 'Unexpected simulator, external network, radio, PSRAM, SMP or disabled pthreads in integration profile' >&2
      exit 1
    fi
  else
    if grep -Eq '^CONFIG_(NET|SMP|ARCH_SIM|DISABLE_PTHREAD|NET_USRSOCK|NSH_NETINIT|NETUTILS_NETINIT|ESP32S3_WIFI|ESP32S3_BLE|ESP32S3_SPIRAM)=y$' .config; then
      echo 'Unexpected network, simulator, SMP, radio, PSRAM or disabled pthreads' >&2
      exit 1
    fi
  fi
fi
if test "$PROFILE" = udp; then
  for symbol in NET NET_IPv4 NET_UDP NET_LOOPBACK NET_SOCKOPTS NETDEV_LATEINIT NET_READAHEAD; do
    grep -qx "CONFIG_$symbol=y" .config || { echo "Unresolved UDP setting: $symbol" >&2; exit 1; }
  done
  if grep -Eq '^CONFIG_(NET_IPv6|NET_TCP|NET_USRSOCK|NET_ETHERNET|NSH_NETINIT|NETUTILS_NETINIT|SMP|DISABLE_PTHREAD)=y$' .config; then
    echo 'Unexpected protocol, external network or disabled pthreads in UDP profile' >&2; exit 1
  fi
fi
for required in CONFIG_BUILD_FLAT=y CONFIG_$APP_SYMBOL=y CONFIG_SYSTEM_TIME64=y CONFIG_FS_LARGEFILE=y CONFIG_TLS_NELEM=16 CONFIG_TLS_NCLEANUP=16 CONFIG_SCHED_WAITPID=y CONFIG_SCHED_HAVE_PARENT=y CONFIG_SCHED_CHILD_STATUS=y CONFIG_NSH_DISABLEBG=y CONFIG_NSH_ARGCAT=y CONFIG_RR_INTERVAL=10; do
  grep -qx "$required" .config || { echo "Unresolved requirement: $required" >&2; exit 1; }
done
if grep -qx 'CONFIG_SIG_DEFAULT=y' .config; then
  echo 'This SDK profile requires SIG_DFL=SIG_IGN=0; default signal actions are unqualified' >&2; exit 1
fi
cd "$ROOT"
# Opt in explicitly: these are target std fixes, not an unmodified-upstream claim.
export NUTTX_STD_SYSROOT="$("$SOURCE_RUSTC" --print sysroot)"
case "${NUTTX_STD_COMPAT_FIXES:-0}" in
  0) printf '%s\n' '{"mode":"upstream-unmodified"}' > "$OUT/std-patch.json" ;;
  1)
    python3 tests/nuttx-std/prepare-std.py --source "$("$SOURCE_RUSTC" --print sysroot)" --output "$OUT" --sdk "$SDK"
    export NUTTX_STD_SYSROOT="$OUT/toolchain"
    ;;
  *) echo "NUTTX_STD_COMPAT_FIXES must be 0 or 1" >&2; exit 1 ;;
esac
# Use the selected SDK executables directly, not rustup proxy discovery.
export RUSTC="$NUTTX_STD_SYSROOT/bin/rustc"
export CARGO_BUILD_RUSTC="$RUSTC"
test "$("$RUSTC" --print sysroot)" = "$NUTTX_STD_SYSROOT"
CARGO_BIN="$NUTTX_STD_SYSROOT/bin/cargo"
TARGET_ARG="$TARGET"
ABI_ARGS=()
if test "$ESP32" = 1; then
  # The pinned distribution supplies rustc/std, with upstream Cargo separately.
  CARGO_BIN="$(rustup which --toolchain 1.90.0 cargo)"
  export NUTTX_STD_GNU_LINKER="$(command -v xtensa-esp32s3-elf-ld)"
  # Preserve the pinned compiler's processor/data layout rather than guessing it.
  "$RUSTC" -Z unstable-options --print target-spec-json --target xtensa-esp32s3-none-elf > "$OUT/xtensa-bare-target.json"
  python3 - "$OUT" <<'PY_TARGET'
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
target = json.loads((out / 'xtensa-bare-target.json').read_text())
assert target['arch'] == 'xtensa' and target['cpu'] == 'esp32s3'
assert str(target['target-pointer-width']) == '32'
assert target.get('max-atomic-width') == 32
# NuttX supplies its libc/pthreads at final link; retain normal binary startup.
target.update(os='nuttx', **{'target-family': ['unix'], 'executables': True,
              'has-thread-local': False, 'linker-flavor': 'ld',
              'linker': 'xtensa-esp32s3-elf-ld', 'relocation-model': 'static'})
for key in ('pre-link-args', 'late-link-args', 'post-link-args'):
    target.pop(key, None)
target['metadata'] = {'description': 'Isolated ESP32-S3 NuttX std probe', 'std': True}
(out / 'xtensa-esp32s3-nuttx.json').write_text(json.dumps(target, indent=2) + '\n')
PY_TARGET
  TARGET_ARG="$OUT/$TARGET.json"
  ABI_ARGS=(--target-spec "$TARGET_ARG")
  cp "$ROOT/target/qemu-tools/downloads.sha256" "$OUT/downloads.sha256"
fi
export CARGO_TARGET_DIR="$OUT/cargo"
export CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_STRIP=none CARGO_PROFILE_RELEASE_DEBUG=1
export NUTTX_STD_LINK_LOG="$OUT/rust-link.json"
if test -n "${NXRS_PLATFORM:-}"; then
  export NXRS_PLATFORM
fi
KEEP_LINK=()
if test "$INTEGRATION" = 1; then
  # C qualification calls this Rust ABI witness only after the Cargo partial
  # link, so keep that exported symbol across --gc-sections explicitly.
  KEEP_LINK=(-C link-arg=-u -C link-arg=rc_rust_abi_probe)
fi
export RUSTFLAGS="-C panic=abort ${CODE_MODEL[*]} -C linker=$ROOT/tests/nuttx-std/link.py ${KEEP_LINK[*]}"
"$CARGO_BIN" build --locked --release "${CARGO_ARGS[@]}" \
  --bin "$BIN" --target "$TARGET_ARG" -Zbuild-std=std,panic_abort \
  --message-format=json-render-diagnostics | tee "$OUT/cargo-messages.jsonl"
# A patched file alone is not evidence that Cargo compiled it. Check the actual
# std compiler-artifact source path before accepting the kernel integration.
python3 - "$OUT" <<'PY_SOURCE'
import json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
messages = [json.loads(line) for line in (out / "cargo-messages.jsonl").read_text().splitlines()]
std = [m for m in messages if m.get("reason") == "compiler-artifact"
       and m["target"]["name"] == "std" and "rlib" in m["target"]["crate_types"]]
assert len(std) == 1, f"expected one std artifact, found {len(std)}"
actual = pathlib.Path(std[0]["target"]["src_path"]).resolve()
expected = pathlib.Path(os.environ["NUTTX_STD_SYSROOT"]) / "lib/rustlib/src/rust/library/std/src/lib.rs"
proof = {"std_source": str(actual), "expected_source": str(expected.resolve()),
         "rustc": os.environ["RUSTC"], "selected_source_matches": actual == expected.resolve()}
(out / "std-source.json").write_text(json.dumps(proof, indent=2) + "\n")
assert proof["selected_source_matches"], "Cargo selected the wrong standard-library source"
PY_SOURCE
ELF="$CARGO_TARGET_DIR/$TARGET/release/$BIN"
"${CROSSDEV}readelf" -h "$ELF" > "$OUT/rust-elf-header.txt"
grep -q 'REL (Relocatable file)' "$OUT/rust-elf-header.txt"
grep -Eq "Machine: +$MACHINE$" "$OUT/rust-elf-header.txt"
"${CROSSDEV}nm" "$ELF" > "$OUT/rust-symbols.txt"
grep -Eq ' T main$' "$OUT/rust-symbols.txt"

if test "$INTEGRATION" = 1; then
  # The full app uses std time and std UDP but does not spawn a Rust worker.
  # Require the concrete std OS calls observed for this integration binary.
  for symbol in socket bind connect send close fcntl clock_gettime nanosleep; do
    grep -Eq " U $symbol$" "$OUT/rust-symbols.txt" || {
      echo "Missing expected std integration import $symbol" >&2
      exit 1
    }
  done
  for symbol in rc_nx_camera_open rc_nx_read rc_nx_file_create rc_nx_append rc_nx_flush rc_nx_close rc_target_qualify rc_sim_note; do
    grep -Eq " U $symbol$" "$OUT/rust-symbols.txt" || {
      echo "Missing expected integration fixture import $symbol" >&2
      exit 1
    }
  done
else
  grep -Eq ' U pthread_create$' "$OUT/rust-symbols.txt"
fi

# Refuse known-unsafe Rust imports BEFORE resolving them to native NuttX symbols.
python3 tests/nuttx-std/check-abi.py --check-imports "$OUT/rust-symbols.txt"

# Unset Rust-only settings before invoking NuttX's C build.
unset RUSTFLAGS
make -C "$OUT/nuttx" -j4 CROSSDEV="$CROSSDEV" NXRS_STD_ELF="$ELF" ESPTOOL_BINDIR=.

# Compile the ABI witnesses against generated target headers before any boot.
python3 tests/nuttx-std/check-abi.py --self-test
python3 tests/nuttx-std/check-abi.py --out "$OUT" --target "$TARGET" "${ABI_ARGS[@]}"
if test "$PROFILE" = udp || test "$INTEGRATION" = 1; then
  # A thread ABI pass is not socket qualification. Fail before any runtime boot.
  python3 tests/nuttx-std/udp/check-abi.py --out "$OUT"
fi

"${CROSSDEV}nm" "$OUT/nuttx/nuttx" > "$OUT/symbols.txt"
for symbol in "$ENTRY" pthread_create pthread_join clock_gettime nx_start; do
  grep -Eq " [TtWw] $symbol$" "$OUT/symbols.txt" || {
    echo "Missing NuttX symbol $symbol" >&2
    exit 1
  }
done

if test "$INTEGRATION" = 1; then
  for symbol in rc_nx_camera_open rc_nx_read rc_nx_close_real rc_nx_file_create_real rc_nx_append rc_target_qualify; do
    grep -Eq " [TtWw] $symbol$" "$OUT/symbols.txt" || {
      echo "Missing integration symbol $symbol" >&2
      exit 1
    }
  done
  if grep -Eq " [TtWw] rc_nx_(udp_open|send)$" "$OUT/symbols.txt"; then
    echo "Legacy UDP C bridge leaked into std integration image" >&2
    exit 1
  fi
fi

"${CROSSDEV}size" -A "$OUT/nuttx/nuttx" > "$OUT/image-size.txt"
"${CROSSDEV}readelf" -h -l -A "$OUT/nuttx/nuttx" > "$OUT/image-layout.txt"
{
  echo "nxrs=$(git rev-parse HEAD)"
  echo "nuttx=$(git rev-parse HEAD:external/nuttx)"
  echo "apps=$(git rev-parse HEAD:external/nuttx-apps)"
  echo "target=$TARGET"
  echo "profile=$PROFILE"
  echo "board=$BOARD"
  "${RUSTC:-rustc}" --version --verbose
  "${CROSSDEV}gcc" --version | head -n 1
  if test "$ESP32" = 1; then
    qemu-system-xtensa --version | head -n 1
  elif test "$PROFILE" = armv8m-qemu; then
    qemu-system-arm --version | head -n 1
  elif test "$PROFILE" != pico2; then
    qemu-system-riscv32 --version | head -n 1
  fi
} > "$OUT/provenance.txt"

sha256sum "$OUT/nuttx/nuttx" "$ELF" "$PROBE_SOURCE" > "$OUT/binaries.sha256"
if test "$PROFILE" = pico2; then
  test -s "$OUT/nuttx/nuttx.bin"
  sha256sum "$OUT/nuttx/nuttx.bin" >> "$OUT/binaries.sha256"
  echo "Pico 2 build/ABI evidence only; hardware execution is not qualified."
elif test "$ESP32" = 1; then
  test -s "$OUT/nuttx/nuttx.merged.bin"
  sha256sum "$OUT/nuttx/nuttx.merged.bin" "$TARGET_ARG" >> "$OUT/binaries.sha256"
fi

echo "NuttX std image: $OUT/nuttx/nuttx"
