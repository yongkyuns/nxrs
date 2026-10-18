#!/bin/bash
# Reuse the checkout, never objects compiled against another NuttX config.
set -euo pipefail
test "$#" = 3 || { echo "Usage: $0 CACHE DESTINATION REVISION" >&2; exit 1; }
cache=$1
destination=$2
revision=$3
cache_root="$(cd "$cache" && pwd -P)"
test -d "$cache_root/.git" &&
  test "$(git -C "$cache_root" rev-parse --show-toplevel)" = "$cache_root" || {
  echo "Espressif cache must be its own Git checkout" >&2; exit 1;
}
test "$(git -C "$cache_root" rev-parse HEAD)" = "$revision" || {
  echo "Unexpected Espressif HAL cache revision" >&2; exit 1;
}
git -C "$cache_root" diff --quiet --ignore-submodules=all HEAD || {
  echo "Uncommitted Espressif HAL source changes require a patch" >&2; exit 1;
}
test ! -e "$destination" && test ! -L "$destination" || {
  echo "Espressif HAL destination already exists" >&2; exit 1;
}
mkdir -p "$(dirname "$destination")"
if test "$(uname -s)" = Darwin; then
  cp -cR "$cache_root" "$destination"
else
  cp -a "$cache_root" "$destination"
fi
export NXRS_ESP_HAL_CLONE_ROOT="$(cd "$destination" && pwd -P)"
test "$(git -C "$destination" rev-parse --absolute-git-dir)" = "$NXRS_ESP_HAL_CLONE_ROOT/.git" || {
  echo "Copied HAL Git metadata points outside its build checkout" >&2; exit 1;
}
# Only clean the newly copied build tree. Keep tracked vendor libraries and
# the original cache intact. Also clean initialized submodules' build output.
git -C "$destination" clean -fdx
git -C "$destination" submodule --quiet foreach --recursive '
  case "$(git rev-parse --absolute-git-dir)" in
    "$NXRS_ESP_HAL_CLONE_ROOT"/.git/*) ;;
    *) echo "Copied submodule Git metadata escapes build checkout" >&2; exit 1 ;;
  esac
  git clean -fdx
'
