#!/usr/bin/env python3
"""Fresh complete operator audit plus actual vulnerable-requirement refusal; no installation."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import qualify as process

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {"imgtool": "2.4.0", "cryptography": "50.0.2", "cffi": "2.1.1",
            "pycparser": "3.11", "click": "8.5.0", "intelhex": "2.3.0",
            "cbor2": "6.1.5", "pyyaml": "6.0.3"}


class Refusal(ValueError):
    def __init__(self, category, status=None):
        self.category = category
        self.status = status
        super().__init__(category)


def request(stage, arguments, directory, environment, timeout):
    try:
        return process.run(arguments, directory, environment, directory, stage, timeout)
    except Exception:
        raise Refusal(stage + "-execution") from None


def dependencies(stage, status, output):
    # Publish categories only. SDK/index errors can contain local paths or URLs.
    try:
        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError()
                result[name] = value
            return result
        report = json.loads(output, object_pairs_hook=unique)
        result = report["dependencies"]
        if (type(result) is not list or not 1 <= len(result) <= 64
                or any(type(item) is not dict or type(item.get("name")) is not str
                       or type(item.get("version")) is not str
                       or type(item.get("vulns")) is not list for item in result)):
            raise ValueError()
        return result
    except Exception:
        raise Refusal(stage + ("-query" if status else "-response"), status) from None


def audit(python, target=False):
    expected = (json.loads((ROOT / "security/esp32s3-boot-dependencies.json").read_text())["operator_versions"]
                if target else EXPECTED)
    requirements = ROOT / ("tools/boot/target-requirements.txt" if target else "tools/boot/requirements.txt")
    python = str(Path(python).absolute())
    with tempfile.TemporaryDirectory(prefix="nxrs-signing-audit-") as temporary:
        directory = Path(temporary)
        environment = {"PATH": os.environ["PATH"], "HOME": temporary,
            "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        status, output = request("version", [python, "-m", "pip_audit", "--version"],
                                 directory, environment, 30)
        if status or output.strip() != "pip-audit 2.10.1":
            raise Refusal("scanner-version", status)
        arguments = [python, "-m", "pip_audit", "--disable-pip", "--format", "json"]
        status, output = request("current", arguments + ["--require-hashes", "-r", str(requirements)],
                                  directory, environment, 180)
        current = dependencies("current", status, output)
        actual = {re.sub(r"[-_.]+", "-", item["name"].lower()): item["version"] for item in current}
        if actual != expected or len(current) != len(expected):
            raise Refusal("current-dependencies", status)
        if any(item.get("skip_reason") is not None for item in current):
            raise Refusal("current-skipped", status)
        if any(item["vulns"] for item in current):
            raise Refusal("current-vulnerabilities", status)
        if status:
            raise Refusal("current-status", status)
        negative = directory / "affected.txt"
        negative.write_text("cryptography==3.4.8\n")
        status, output = request("affected", arguments + ["--no-deps", "-r", str(negative)],
                                  directory, environment, 180)
        affected = dependencies("affected", status, output)
        if (status != 1 or len(affected) != 1 or affected[0]["name"] != "cryptography"
                or affected[0]["version"] != "3.4.8" or not affected[0]["vulns"]
                or affected[0].get("skip_reason") is not None):
            raise Refusal("affected-control", status)
        historical = {"PYSEC-2023-11", "GHSA-w7pp-m8wf-vj6r", "CVE-2023-23931"}
        identifiers = set()
        for item in affected[0]["vulns"]:
            if (type(item) is not dict or type(item.get("id")) is not str
                    or type(item.get("aliases", [])) is not list
                    or any(type(alias) is not str for alias in item.get("aliases", []))):
                raise Refusal("affected-response", status)
            identifiers.add(item["id"])
            identifiers.update(item.get("aliases", []))
        if not identifiers.intersection(historical):
            raise Refusal("affected-advisory", status)
    print(f"All {len(expected)} pinned operator packages passed; historical affected requirement was refused without installation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-python", type=Path, required=True)
    parser.add_argument("--target", action="store_true", help="audit the complete target-build environment")
    args = parser.parse_args()
    try:
        audit(args.audit_python, args.target)
    except Exception as error:
        category = error.category if isinstance(error, Refusal) else "operation"
        status = error.status if isinstance(error, Refusal) else None
        suffix = " status=" + str(status) if type(status) is int and 0 <= status <= 255 else ""
        print("Signing operator advisory qualification failed: " + category + suffix, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
