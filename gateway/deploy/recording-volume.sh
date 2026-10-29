#!/bin/sh
# Read-only admission for the operator-provisioned recording store. No unlock,
# format, repair, directory creation or key material is performed here.
set -eu
export LC_ALL=C
test "$#" = 1
printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
test "$(wc -l < /proc/swaps)" -eq 1
grep -Fxq '|/bin/false' /proc/sys/kernel/core_pattern
test "$(stat -c '%u %a' /run/nxrs-keys)" = '0 700'
findmnt --nocanonicalize -rn -T /run/nxrs-keys -t tmpfs >/dev/null
device="$(findmnt --nocanonicalize -rn -M /var/lib/nxrs-recordings -t ext4 -O rw -o MAJ:MIN)"
case "$device" in ''|*[!0-9:]*) exit 1;; esac
expected="CRYPT-LUKS2-$(printf '%s' "$1" | tr -d '-')-nxrs-recordings"
test "$(cat "/sys/dev/block/$device/dm/uuid")" = "$expected"
status="$(/usr/sbin/cryptsetup status nxrs-recordings)"
printf '%s\n' "$status" | grep -Eq '^  type: +LUKS2$'
printf '%s\n' "$status" | grep -Eq '^  cipher: +aes-xts-plain64$'
printf '%s\n' "$status" | grep -Eq '^  keysize: +512 bits$'
printf '%s\n' "$status" | grep -Eq '^  mode: +read/write$'
