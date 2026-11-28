#!/usr/bin/env python3
"""Rebuild the archived ARM fixture with the reviewed startup correction."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


EXPECTED_ELF = "df780f2e12bed92342f0fdf9693cd46a9cba95c17ebd157bf4f5e48ada38ad78"
EXPECTED_CONFIG = "ada256ac803291d57aad9e317045c43c9212e4ed461d7540aa57cbd0170ea486"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enabled_configuration(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            if value != "n":
                values[key] = value
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    retained, output = args.retained.resolve(), args.output.resolve()
    if sha256(retained / "nuttx") != EXPECTED_ELF or sha256(retained / "resolved.config") != EXPECTED_CONFIG:
        raise RuntimeError("Original fixture image/configuration identity mismatch")
    original = json.loads((retained / "build.json").read_text())
    compiler = subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0]
    if compiler != original["compiler"]:
        raise RuntimeError(f"Compiler differs from original: {compiler!r}")
    output.mkdir(parents=True, exist_ok=False)
    sources = root / "target/memory-timing-sources"
    sources.mkdir(parents=True, exist_ok=False)
    with (output / "prepare.log").open("w") as log:
        def run(command, cwd=root):
            subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)

        for name, repo, revision in (
            ("nuttx", "apache/nuttx", original["nuttx_revision"]),
            ("apps", "apache/nuttx-apps", original["apps_revision"]),
        ):
            tree = sources / name
            run(["git", "init", str(tree)])
            run(["git", "remote", "add", "origin", "https://github.com/" + repo + ".git"], tree)
            run(["git", "fetch", "--depth=1", "origin", revision], tree)
            run(["git", "checkout", "--detach", "FETCH_HEAD"], tree)
        nuttx, apps = sources / "nuttx", sources / "apps"
        for name in ("memory-timing", "heap-replay", "heap-requests"):
            installed = apps / "examples" / name.replace("-", "_")
            shutil.copytree(root / "tests/nuttx-std" / name, installed)
            shutil.copy2(root / "tests/nuttx-std/memory-timing/qemu_timer_clock.c", installed)
            if name == "memory-timing":
                shutil.copy2(root / "shared/acquisition/src/platform/nuttx/ffi/trace.c",
                             installed / "acquisition_trace.c")
        run(["bash", "tools/configure.sh", "-l", "-a", "../apps", "mps2-an521:nsh"], nuttx)
        if apps.resolve() != Path(original["configured_apps_directory"]):
            raise RuntimeError("Original configured Apps path was not reproduced")
        shutil.copy2(retained / "resolved.config", nuttx / ".config")
        run([sys.executable, "tools/memory-explorer/tests/build_timed_capture_nuttx.py",
             "--nuttx", str(nuttx), "--apps", str(apps), "--output", str(output / "build")])
    rebuilt = json.loads((output / "build/build.json").read_text())
    sys.path.insert(0, str(root / "tools/memory-explorer/tests"))
    from build_timing_nuttx import validate_atomic_stack_patch
    validate_atomic_stack_patch(rebuilt, root, output / "build")
    atomic_stack = rebuilt["atomic_stack_patch"]
    if (atomic_stack["before"] != "bad2478f501954bdf252ca71280b4025c5ed29ade912b4d94fc8f0957946520f" or
            atomic_stack["after"] != "f1a091dd7bf23825cdb5e9563cc37718a2c90b38014049919d0e7eb2d9528666"):
        raise RuntimeError("Native startup source differs from the reviewed correction")
    if enabled_configuration(retained / "resolved.config") != enabled_configuration(output / "build/resolved.config"):
        raise RuntimeError("Rebuilt runtime configuration differs from the original fixture")
    same_fields = ["compiler", "nuttx_revision", "apps_revision", "clock_contract",
                   "fixture_sha256", "installed_fixture_sha256", "copied_sources",
                   "production_patch", "systick_patch"]
    for field in same_fields:
        if original[field] != rebuilt[field]:
            raise RuntimeError("Rebuilt fixture input differs: " + field)
    report = {"purpose": "Before/after startup correction diagnosis",
              "original_elf_sha256": EXPECTED_ELF, "original_config_sha256": EXPECTED_CONFIG,
              "rebuilt_elf_sha256": sha256(output / "build/nuttx"),
              "rebuilt_config_sha256": sha256(output / "build/resolved.config"),
              "enabled_configuration_identical": True, "identical_inputs": same_fields,
              "atomic_stack_patch": atomic_stack,
              "stack_transition_patch_sha256": sha256(root / "upstream/nuttx/patches/0150-armv8m-atomic-stack-transition.patch"),
              "build_receipt_sha256": sha256(output / "build/build.json")}
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
