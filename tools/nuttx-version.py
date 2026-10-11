#!/usr/bin/env python3
"""Restore release identity lost by git archive, using the selected source pin."""

import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def release_version(revision):
    # A shallow CI submodule need not contain release tags. Resolve the label
    # from tracked, revision-checked metadata instead of guessing from Git.
    release = json.loads((ROOT / "upstream/nuttx/release.json").read_text())
    if release.get("schema") != 1 or revision != release.get("revision"):
        raise ValueError("selected NuttX revision does not match the release pin")
    if not re.fullmatch(r"\d+\.\d+\.\d+", release["version"]):
        raise ValueError("invalid stable NuttX release version")
    return release["version"]


def stamp(source, revision):
    source = Path(source).resolve(strict=True)
    if (source / ".git").exists():
        raise ValueError("version stamping requires a private source archive")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("version stamping requires a full source revision")
    version = release_version(revision)
    # The suffix distinguishes our patched archive from a stock release. The
    # accompanying patch ledger identifies the exact adaptations and hashes.
    subprocess.run(["bash", str(source / "tools/version.sh"), "-v", version,
                    "-b", revision[:12] + "-nxrs", str(source / ".version")], check=True)
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    stamp(args.source, args.revision)
