#!/usr/bin/env python3
"""Create private capture provenance from a source-matching CSI-only build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def create(build, output):
    build = build.resolve()
    receipt = json.loads((build / "reference-build.json").read_text())
    if receipt.get("continuous_recording", {}).get("wire_version") != 1:
        raise ValueError("requires recording-capable CSI-only build")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    if sha(build / "reference-flash.bin") != receipt["image_sha256"]:
        raise ValueError("image differs from build receipt")
    for path, digest in receipt["source_sha256"].items():
        source = (ROOT / path).resolve()
        if not source.is_relative_to(ROOT) or sha(source) != digest:
            raise ValueError("source differs from build receipt; rebuild")
    if output.resolve().is_relative_to(ROOT):
        raise ValueError("keep private capture provenance outside checkout")
    manifest = {
        "scope": "calibration_and_numeric_inputs_v1",
        "origin": "hardware",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_files_sha256": receipt["source_sha256"],
        "firmware_sha256": receipt["image_sha256"],
        "sdk_revision": receipt["idf_commit"],
        "config_sha256": receipt["config_sha256"],
        "radio_archives_sha256": receipt["radio_archives_sha256"],
        "gain_archive_sha256": receipt["gain_archive_sha256"],
        "motion_archive_sha256": receipt["motion_archive_sha256"],
        "physical_flash_identity_verified": False,
        "full_sensing_fsm_replay_supported": False,
    }
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    create(a.build, a.output)
