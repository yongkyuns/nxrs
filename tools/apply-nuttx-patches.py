#!/usr/bin/env python3
"""Apply tracked upstream patch series to archived NuttX build copies only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SERIES = {
    "nuttx": (
        "0001-Fix-ESP32S3-BLE-advertising.patch",
        "0002-Add-ESP32S3-NimBLE-HCI-configuration-option.patch",
        "0003-Add-ESP32-S3-camera-driver-for-OV3660-sensor.patch",
        "0004-Enable-ESP32-S3-Wi-Fi-HAL-helpers-under-NuttX.patch",
        "0005-net-udp-return-zero-length-datagrams-from-readahead.patch",
        "0006-flat-build-global-pthread-keys.patch",
        "0007-esp32s3-spiram-size-mismatch-return.patch",
        "0008-esp32s3-devkit-freenove-userled.patch",
    ),
    "nuttx-apps": ("0001-Add-ESP32S3-VHCI-transport-support-for-NimBLE.patch",),
}
MARKERS = {
    "nuttx": "libs/libc/tls/Kconfig",
    "nuttx-apps": "wireless/bluetooth/nimble/Makefile.nimble",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_env(source):
    # A build archive may live under the nxrs checkout. Prevent git apply from
    # treating its paths as relative to that unrelated outer repository.
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)}


def run_git_apply(source, *options, patch):
    return subprocess.run(
        ["git", "apply", *options, str(patch)],
        cwd=source, env=git_env(source), text=True, capture_output=True,
    )


def apply(source, revision, record, component="nuttx"):
    source = Path(source).resolve(strict=True)
    if (source / ".git").exists():
        raise ValueError("refusing to patch a Git checkout; use an archived build copy")
    if not (source / MARKERS[component]).is_file():
        raise ValueError(f"not a {component} source archive")

    provenance = {"schema": 1, "component": component,
                  "upstream_revision": revision, "patches": []}
    for name in SERIES[component]:
        patch = ROOT / "platform" / component / "patches" / name
        paths = [line.split("\t", 2)[2] for line in subprocess.check_output(
            ["git", "apply", "--numstat", str(patch)],
            cwd=source, env=git_env(source), text=True,
        ).splitlines()]
        if not paths:
            raise ValueError(f"patch {name} has no source changes")
        check = run_git_apply(source, "--check", "--whitespace=error", patch=patch)
        if check.returncode:
            raise ValueError(f"patch {name} is incompatible or already applied: {check.stderr.strip()}")
        before = {path: sha256(source / path) if (source / path).is_file() else None
                  for path in paths}
        result = run_git_apply(source, "--whitespace=error", patch=patch)
        if result.returncode:
            raise RuntimeError(f"failed to apply {name}: {result.stderr.strip()}")
        provenance["patches"].append({
            "name": name,
            "sha256": sha256(patch),
            "files": {path: {"before": before[path], "after": sha256(source / path)}
                      for path in paths},
        })
    Path(record).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--component", choices=SERIES, default="nuttx")
    args = parser.parse_args()
    apply(args.source, args.revision, args.record, args.component)
