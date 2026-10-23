#!/usr/bin/env python3
"""Offline witnesses for selected pinned vendor calibration/presence functions.

This compiles unchanged function bodies extracted from SHA-verified packages.
Test-local structures/stubs supply their inputs; no SDK, RF, tasks or target ABI
is qualified. Vendor source is materialized only in a fresh ignored directory.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("presence_build", HERE / "build.py")
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)
FUNCTIONS = {
    "esp_wifi_sensing": (
        "hms_presence_gain_from_unit",
        "hms_cmp_float_asc",
        "hms_average_float_array",
        "hms_trimmean",
        "hms_reset_presence_state",
        "hms_update_presence_state",
    ),
    "esp-radar": (
        "csi_training_collect_sample",
        "esp_radar_train_stop_ex",
    ),
}
DEFINES = {
    "esp_wifi_sensing": (
        "HMS_PRESENCE_HISTORY_LEN",
        "HMS_PRESENCE_MIN_SAMPLES",
        "HMS_PRESENCE_TRIM_PERCENT",
        "HMS_PRESENCE_GAIN_MIN",
        "HMS_PRESENCE_GAIN_MAX",
    ),
    "esp-radar": (
        "CSI_WANDER_NUM",
        "CSI_WANDER_THRESHOLD",
        "RADAR_BUFF_NUM",
        "RADAR_OUTLIERS_THRESHOLD",
    ),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def package_source(path, name):
    data = path.read_bytes()
    if digest(data) != BUILD.PACKAGES[name]:
        raise ValueError("vendor package digest mismatch")
    filename = "esp_radar.c" if name == "esp-radar" else "esp_wifi_sensing.c"
    with zipfile.ZipFile(path) as archive:
        return archive.read(f"{name}/src/{filename}").decode()


def function(source, name):
    # Keep offsets while ignoring braces in comments and string/character literals.
    masked = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: " " * len(match[0]),
        source,
        flags=re.S,
    )
    starts = list(
        re.finditer(
            rf"(?m)^(?:static\s+)?[\w *]+\b{re.escape(name)}\([^;{{}}]*\)\s*\{{",
            masked,
        )
    )
    if len(starts) != 1:
        raise ValueError("missing or ambiguous vendor function: " + name)
    start = starts[0].start()
    brace = starts[0].end() - 1
    depth = 0
    for index in range(brace, len(masked)):
        depth += (masked[index] == "{") - (masked[index] == "}")
        if depth == 0:
            return source[start : index + 1]
    raise ValueError("incomplete vendor function: " + name)


def define(source, name):
    matches = re.findall(rf"(?m)^#define\s+{re.escape(name)}\s+[^\n]+", source)
    if len(matches) != 1 or matches[0].rstrip().endswith("\\"):
        raise ValueError("missing or ambiguous vendor constant: " + name)
    return matches[0]


def verify(output, status, negative=False):
    if status != (1 if negative else 0):
        raise ValueError("policy witness exit status")
    if negative:
        if output != "POLICY_DELIBERATE_FAILURE\n":
            raise ValueError("policy negative control evidence")
    else:
        if (
            re.findall(r"^POLICY_REPORT checks=(\d+) failures=(\d+)$", output, re.M)
            != [("30", "0")]
            or "POLICY_FAIL" in output
        ):
            raise ValueError("policy witness incomplete or failed")
        expected = {
            "stationary_default_present": "1",
            "stationary_minimum_present": "0",
            "above_baseline_minimum_present": "1",
            "collected_template_events": "44",
            "retained_template_slots": "10",
        }
        actual = re.findall(r"^POLICY_VALUE (\w+) (\d+)$", output, re.M)
        if len(actual) != len(expected) or dict(actual) != expected:
            raise ValueError("policy numerical/control evidence mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radar-package", required=True, type=Path)
    parser.add_argument("--sensing-package", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cc", default="cc")
    args = parser.parse_args()
    out = BUILD.IDF.COMMON.validate_output(args.out)
    if out.exists():
        parser.error("choose a fresh output directory")
    sources = {
        name: package_source(path, name)
        for name, path in (
            ("esp-radar", args.radar_package),
            ("esp_wifi_sensing", args.sensing_package),
        )
    }
    bodies = {
        name: function(sources[package], name)
        for package, names in FUNCTIONS.items()
        for name in names
    }
    constants = [
        define(sources[package], name)
        for package, names in DEFINES.items()
        for name in names
    ]
    out.mkdir()
    (out / "vendor-functions.inc").write_text("\n\n".join(bodies.values()) + "\n")
    (out / "vendor-constants.inc").write_text("\n".join(constants) + "\n")
    source = HERE / "policy.c"
    binary = out / "policy"
    command = [
        args.cc,
        "-std=c11",
        "-O0",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-fno-fast-math",
        "-I",
        str(out),
        str(source),
        "-o",
        str(binary),
    ]
    subprocess.run(command, check=True)
    runs = []
    for negative in (False, True):
        result = subprocess.run(
            [str(binary)] + (["fail"] if negative else []),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stderr:
            raise ValueError("unexpected policy witness stderr")
        verify(result.stdout, result.returncode, negative)
        (out / ("negative.log" if negative else "positive.log")).write_text(
            result.stdout
        )
        runs.append(
            {
                "negative": negative,
                "status": result.returncode,
                "stdout_sha256": digest(result.stdout.encode()),
                "passed": True,
            }
        )
    report = {
        "scope": "selected unchanged vendor function bodies with test-local structures and stubs; not the complete FSM, SDK, target ABI or RF",
        "package_sha256": BUILD.PACKAGES,
        "source_sha256": {k: digest(v.encode()) for k, v in sources.items()},
        "function_sha256": {k: digest(v.encode()) for k, v in bodies.items()},
        "fixture_sha256": {
            str(p.relative_to(ROOT)): digest(p.read_bytes())
            for p in (HERE / "policy.py", source)
        },
        "compiler": subprocess.check_output(
            [args.cc, "--version"], text=True
        ).splitlines()[0],
        "compiler_flags": command[1:7],
        "executable_sha256": digest(binary.read_bytes()),
        "hardware_accessed": False,
        "checks": 30,
        "runs": runs,
    }
    (out / "policy-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "PASS: 30 selected vendor-policy checks and deliberate-failure control; no RF/SDK qualification"
    )


if __name__ == "__main__":
    main()
