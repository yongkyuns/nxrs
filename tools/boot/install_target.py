#!/usr/bin/env python3
"""Create a fresh hash-pinned build environment; vendor esptool is built offline."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import qualify as process
import target_plan as target


def install(path):
    target.require(sys.version_info >= (3, 12))
    work = target.fresh(path)
    environment = {"PATH": os.environ["PATH"], "HOME": str(work), "PYTHONNOUSERSITE": "1",
                   "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    requirements = target.ROOT / "tools/boot/target-requirements.txt"
    spec = json.loads((target.ROOT / "security/esp32s3-boot-dependencies.json").read_text())
    process.verify(requirements, spec["files"][str(requirements.relative_to(target.ROOT))])
    status, _ = process.run([sys.executable, "-m", "venv", str(work / "venv")],
                            work, environment, work, "venv", 60)
    target.require(status == 0)
    python = str(work / "venv/bin/python")
    backend = work / "backend.txt"
    groups = re.findall(r"(?ms)^(?:setuptools|wheel|packaging)==.*?(?=^[a-zA-Z0-9_-]+==|\Z)", requirements.read_text())
    target.require(len(groups) == 3)
    backend.write_text("".join(groups))
    # Only the fixed esptool sdist needs a build. Its backend is hash-installed
    # first; --no-build-isolation prohibits pip's separate unpinned backend fetch.
    commands = (["--require-hashes", "--only-binary=:all:", "-r", str(backend)],
                ["--require-hashes", "--no-build-isolation", "--only-binary=:all:", "--no-binary=esptool", "-r", str(requirements)])
    for index, arguments in enumerate(commands):
        status, _ = process.run([python, "-m", "pip", "install", *arguments],
                                work, environment, work, "install-" + str(index), 300)
        target.require(status == 0)
    status, _ = process.run([python, "-m", "pip", "check"], work, environment, work, "check", 30)
    target.require(status == 0)
    print("Hash-verified isolated target build environment installed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        install(args.out)
    except Exception:
        print("Isolated target build environment installation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
