#!/usr/bin/env bash
# Build a genuine NuttX sim image without modifying the ESP32-S3 worktree.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/target/nuttx-sim"
export RUSTUP_TOOLCHAIN=1.90.0
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'This simulator profile requires Linux x86_64.' >&2
  exit 1
fi
for command in cargo gcc make git kconfig-tweak genromfs; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
mkdir -p "$OUT"
rm -rf "$OUT/nuttx" "$OUT/apps"
mkdir -p "$OUT/nuttx" "$OUT/apps"
for entry in 'nuttx:nuttx' 'nuttx-apps:apps'; do
  source_name=${entry%:*}
  destination=${entry#*:}
  expected=$(git -C "$ROOT" rev-parse ":external/$source_name")
  actual=$(git -C "$ROOT/external/$source_name" rev-parse HEAD)
  test "$expected" = "$actual" || { echo "Unpinned $source_name" >&2; exit 1; }
  git -C "$ROOT/external/$source_name" archive "$expected" | tar -x -C "$OUT/$destination"
  python3 "$ROOT/tools/apply-nuttx-patches.py" \
    --component "$source_name" --source "$OUT/$destination" --revision "$expected" \
    --record "$OUT/$source_name-patches.json"
done
python3 "$ROOT/tools/nuttx-version.py" --source "$OUT/nuttx" \
  --revision "$(git -C "$ROOT" rev-parse :external/nuttx)"
{
  echo "nxrs=$(git -C "$ROOT" rev-parse HEAD)"
  echo "nxrs_dirty=$(test -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" && echo no || echo yes)"
  echo "nuttx=$(git -C "$ROOT/external/nuttx" rev-parse HEAD)"
  echo "apps=$(git -C "$ROOT/external/nuttx-apps" rev-parse HEAD)"
  echo 'core-build=source; code-model=small; relocation-model=pic; build-std=core'
  rustc --version --verbose
} > "$OUT/provenance.txt"
cd "$ROOT"
# The distributed core uses the kernel code model, not small/PIC. Scope the
# unstable rebuild opt-in to this pinned compiler invocation.
NXRS_PLATFORM=nuttx-core RUSTC_BOOTSTRAP=1 RUSTFLAGS='-C code-model=small -C relocation-model=pic' \
  cargo build --locked --profile nuttx-sim --lib -p nxrs-nuttx-app \
  --target x86_64-unknown-none -Zbuild-std=core
LIB="$ROOT/target/x86_64-unknown-none/nuttx-sim/libnxrs_nuttx_app.a"
APP="$OUT/apps/examples/nxrs_sim"
mkdir -p "$APP"
cp "$ROOT/platform/nuttx/qualification/"* "$APP/"
cp "$ROOT/tests/nuttx/c/"* "$APP/"
cp "$ROOT/hal/camera/src/platform/nuttx/ffi/"{close.c,status.h} "$APP/"
cd "$OUT/nuttx"
source "$ROOT/platform/nuttx/profiles/sim.sh"
make -j"${NUTTX_SIM_JOBS:-4}" NXRS_SIM_LIB="$LIB"
cp .config "$OUT/resolved.config"
nm arch/sim/src/nuttx.rel > "$OUT/symbols.txt"
objdump -r arch/sim/src/nuttx.rel > "$OUT/relocations.txt"
python3 "$ROOT/tests/host/check-nuttx-sim-link.py" "$OUT"
sha256sum nuttx "$LIB" > "$OUT/binaries.sha256"
echo "NuttX simulator executable: $OUT/nuttx/nuttx"
