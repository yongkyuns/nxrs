#!/usr/bin/env bash
# Optional Dropbear qualification, using the ordinary build/profile frontend.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
OUT="$ROOT/target/dropbear-ssh"
BIND=127.0.0.1
KEY_PATH=/mnt/ssh/dropbear_ecdsa_host_key
PASSWD_PATH=/mnt/ssh/passwd
SSH=1
DRY=0
EXTRA=()
while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --hostkey-path) KEY_PATH="$2"; shift 2 ;;
    --passwd-path) PASSWD_PATH="$2"; shift 2 ;;
    --without-ssh) SSH=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help)
      echo 'Usage: build.sh [--out DIR] [--bind IPV4] [--hostkey-path /mount/key] [--passwd-path /mount/passwd] [--without-ssh] [--dry-run]'
      echo 'The default bind is loopback for build-only CI; supply the reserved device IPv4 for hardware.'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
python3 - "$BIND" "$KEY_PATH" "$PASSWD_PATH" <<'PY'
import ipaddress, re, sys
address = ipaddress.IPv4Address(sys.argv[1])
if address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
    raise SystemExit('SSH requires a concrete unicast IPv4 bind address')
for path in sys.argv[2:]:
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or '..' in path.split('/'):
        raise SystemExit('Provision paths must be absolute, without whitespace, traversal or Kconfig quoting')
if sys.argv[2] == sys.argv[3]:
    raise SystemExit('Host key and password database must have different paths')
PY
ARGS=(
  --app-manifest tests/nuttx-std/ssh/Cargo.toml
  --app-package nxrs-std-ssh --bin nxrs-std-ssh --command ssh_probe
  --priority 100 --stack-size 65536 --profile esp32s3-wifi
  --out "$OUT" --abi-profile minimal --size-optimized --trace-only-backtrace --panic-immediate-abort
  --target-c-source tests/nuttx-std/ssh/nuttx/fixture.c
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c
)
# Shared instrumentation belongs in BOTH images for a meaningful delta. TMPFS
# supports a disposable bring-up fixture; persistence requires a real mount.
for symbol in FS_TMPFS FS_PROCFS STACK_COLORATION NSH_DISABLEBG SCHED_WAITPID SCHED_HAVE_PARENT SCHED_CHILD_STATUS; do
  ARGS+=(--kconfig-enable "$symbol")
done
if ((SSH)); then
  for symbol in NET_TCP NSH_LIBRARY NSH_BUILTIN_APPS BUILTIN PSEUDOTERM PIPES ARCH_SETJMP_H \
      SERIAL_TERMIOS TTY_SIGINT LIBC_NETDB LIBC_GAISTRERROR \
      CRYPTO CRYPTO_SW_AES ALLOW_BSD_COMPONENTS CRYPTO_CRYPTODEV \
      CRYPTO_CRYPTODEV_SOFTWARE_CRYPTO NETUTILS_CODECS CODECS_BASE64 \
      FSUTILS_PASSWD NETUTILS_DROPBEAR ESP32S3_RNG DEV_RANDOM DEV_URANDOM \
      DEV_URANDOM_ARCH NETUTILS_DROPBEAR_SYSLOG; do
    ARGS+=(--kconfig-enable "$symbol")
  done
  # NSH_DROPBEAR is invisible with background commands disabled. The verifier
  # rejects it if enabled; do not demand an invisible '# ... is not set' line.
  for symbol in NSH_PIPELINE NETUTILS_DROPBEAR_SCP \
      NETUTILS_DROPBEAR_GENERATE_HOSTKEY FSUTILS_PASSWD_READONLY; do
    ARGS+=(--kconfig-disable "$symbol")
  done
  for value in \
      "NETUTILS_DROPBEAR_BIND_ADDRESS=\"$BIND\"" \
      "NETUTILS_DROPBEAR_HOSTKEY_PATH=\"$KEY_PATH\"" \
      "FSUTILS_PASSWD_PATH=\"$PASSWD_PATH\"" \
      NETUTILS_DROPBEAR_PORT=2222 NETUTILS_DROPBEAR_STACKSIZE=65536 \
      NETUTILS_DROPBEAR_SHELL_STACKSIZE=8192 \
      NETUTILS_DROPBEAR_AUTH_TIMEOUT=30 NETUTILS_DROPBEAR_IDLE_TIMEOUT=60 \
      NETUTILS_DROPBEAR_MAX_AUTH_TRIES=3 NETUTILS_DROPBEAR_LISTEN_RETRIES=2 \
      NETUTILS_DROPBEAR_LISTEN_RETRY_MAX=2 NETUTILS_DROPBEAR_STOP_TIMEOUT=10 \
      TTY_SIGINT_CHAR=0x03; do
    ARGS+=(--kconfig-set "$value")
  done
fi
ARGS+=("${EXTRA[@]}")
if ((DRY)); then ARGS+=(--dry-run); fi
bash tools/build-nuttx-std-app.sh "${ARGS[@]}"
if ((DRY)); then exit 0; fi
python3 tests/nuttx-std/ssh/check-build.py "$OUT" "$SSH"
echo 'SSH cross-build evidence written; device qualification remains a separate step.'
