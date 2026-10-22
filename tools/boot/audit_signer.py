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


def audit(python):
    python = str(Path(python).absolute())
    with tempfile.TemporaryDirectory(prefix="nxrs-signing-audit-") as temporary:
        directory = Path(temporary)
        environment = {"PATH": os.environ["PATH"], "HOME": temporary,
            "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        status, output = process.run([python, "-m", "pip_audit", "--version"],
                                     directory, environment, directory, "version", 30)
        if status or output.strip() != "pip-audit 2.10.1":
            raise ValueError("pinned scanner required")
        arguments = [python, "-m", "pip_audit", "--disable-pip", "--format", "json"]
        status, output = process.run(arguments + ["--require-hashes", "-r",
            str(ROOT / "tools/boot/requirements.txt")], directory, environment, directory, "current", 180)
        report = json.loads(output)
        dependencies = report["dependencies"]
        actual = {re.sub(r"[-_.]+", "-", item["name"].lower()): item["version"] for item in dependencies}
        if (status or actual != EXPECTED or len(dependencies) != len(EXPECTED)
                or any(item.get("skip_reason") or item["vulns"] for item in dependencies)):
            raise ValueError("complete operator environment advisory gate failed")
        negative = directory / "affected.txt"
        negative.write_text("cryptography==3.4.8\n")
        status, output = process.run(arguments + ["--no-deps", "-r", str(negative)],
                                     directory, environment, directory, "affected", 180)
        affected = json.loads(output)["dependencies"]
        if (status == 0 or len(affected) != 1 or affected[0]["name"] != "cryptography"
                or affected[0]["version"] != "3.4.8" or not affected[0]["vulns"]
                or affected[0].get("skip_reason")):
            raise ValueError("actual affected requirement was not refused")
    print("All eight pinned operator packages passed; historical affected requirement was refused without installation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-python", type=Path, required=True)
    args = parser.parse_args()
    try:
        audit(args.audit_python)
    except Exception:
        print("Signing operator advisory qualification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
