#!/usr/bin/env python3
"""Identify IDF 6.0 radio inputs against the measured NuttX SDK, offline by default.

No firmware build, serial access, gain forcing or runtime qualification occurs.
Whole-member differences can include debug information; they are not a machine
code diff. Even complete archive equality does not establish metadata semantics.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

HERE = Path(__file__).resolve().parent
PIN_FILE = HERE / "idf60-reference.json"
EVIDENCE_FILE = HERE / "results/esp32s3-tcp-controls-2026-10-22.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(path, expected):
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f"{path.name}: unexpected archive SHA256")
    return data


def fetch(directory, pins):
    """Explicit opt-in download of immutable official URLs; validate before save."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, pin in pins["archives"].items():
        path = directory / name
        if path.exists():
            checked(path, pin["sha256"])
            continue
        url = (
            f"https://raw.githubusercontent.com/espressif/{pin['repository']}/"
            f"{pin['revision']}/esp32s3/{name}"
        )
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if digest(data) != pin["sha256"]:
            raise ValueError(f"{name}: downloaded archive SHA256 mismatch")
        # Never replace an existing artifact, including one created concurrently.
        with path.open("xb") as output:
            output.write(data)


def members(ar, archive):
    names = subprocess.check_output([ar, "t", str(archive)], text=True).splitlines()
    if not names or len(set(names)) != len(names):
        raise ValueError("empty archive or ambiguous duplicate members")
    if any(not name.endswith(".o") or Path(name).name != name for name in names):
        raise ValueError("unexpected archive member name")
    return {
        name: subprocess.check_output([ar, "p", str(archive), name]) for name in names
    }


def compare(reference, sdk, ar, pins, evidence):
    measured = evidence["latest_build"]
    expected_paths = measured["radio_archives_sha256"]
    actual_paths = {pin["sdk_path"] for pin in pins["archives"].values()}
    if actual_paths != set(expected_paths):
        raise ValueError("reference manifest must cover every measured radio archive")
    if pins["measured_image_sha256"] != measured["image_sha256"]:
        raise ValueError("reference manifest does not identify the measured image")
    # Validate all inputs before invoking external tools or reporting a comparison.
    inputs = {}
    for name, pin in pins["archives"].items():
        inputs[name] = (
            checked(reference / name, pin["sha256"]),
            checked(sdk / pin["sdk_path"], expected_paths[pin["sdk_path"]]),
        )
    archives = {}
    for name, (ref_data, sdk_data) in inputs.items():
        pin = pins["archives"][name]
        ref_members = members(ar, reference / name)
        sdk_members = members(ar, sdk / pin["sdk_path"])
        common = set(ref_members) & set(sdk_members)
        identical = {n for n in common if ref_members[n] == sdk_members[n]}
        focus = {}
        for member in pin["focus_members"]:
            if member not in common:
                raise ValueError(f"{name}: required comparison member missing")
            focus[member] = {
                "reference_sha256": digest(ref_members[member]),
                "measured_sha256": digest(sdk_members[member]),
                "byte_identical": member in identical,
            }
        archives[name] = {
            "reference_sha256": digest(ref_data),
            "measured_sha256": digest(sdk_data),
            "byte_identical": ref_data == sdk_data,
            "reference_member_count": len(ref_members),
            "measured_member_count": len(sdk_members),
            "common_member_count": len(common),
            "identical_member_count": len(identical),
            "different_common_member_count": len(common - identical),
            "different_common_members": sorted(common - identical),
            "reference_only_members": sorted(set(ref_members) - common),
            "measured_only_members": sorted(set(sdk_members) - common),
            "focus_members": focus,
        }
    return {
        "schema_version": 1,
        "scope": "Exact archive and object-member identity only, including debug data",
        "reference": {k: v for k, v in pins.items() if k != "archives"},
        "archives": archives,
        "reference_firmware_built": False,
        "hardware_accessed": False,
        "gain_forcing_executed": False,
        "private_gain_metadata_qualified": False,
        "presence_detection_qualified": False,
        "limits": [
            "Different object bytes do not by themselves prove different executable behavior.",
            "Identical PHY archives do not establish Wi-Fi RX metadata construction or meaning.",
            "No independent IDF 6.0 over-the-air reference capture has been measured.",
            "No undocumented RX bytes or physical gain response are qualified by this report.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", required=True, type=Path)
    parser.add_argument("--sdk-root", required=True, type=Path)
    parser.add_argument("--ar", default="xtensa-esp-elf-ar")
    parser.add_argument(
        "--fetch", action="store_true", help="download pinned official archives"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pins = json.loads(PIN_FILE.read_text())
    evidence = json.loads(EVIDENCE_FILE.read_text())
    if args.fetch:
        fetch(args.reference_dir, pins)
    report = compare(args.reference_dir, args.sdk_root, args.ar, pins, evidence)
    report["analysis_inputs_sha256"] = {
        "reference.py": digest(Path(__file__).read_bytes()),
        PIN_FILE.name: digest(PIN_FILE.read_bytes()),
        EVIDENCE_FILE.name: digest(EVIDENCE_FILE.read_bytes()),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("Static reference comparison recorded; RF metadata remains unqualified")


if __name__ == "__main__":
    main()
