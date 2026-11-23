#!/usr/bin/env python3
"""Build the pinned Matter gateway candidate and emit its build receipt."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "gateway/tests/matter/versions.json"
INPUTS = (
    "gateway/Cargo.toml",
    "gateway/matter",
    "gateway/tests/matter",
    "service/detection",
    "tools/prepare-matter-fixture.py",
    "tools/build-matter-gateway.py",
)
VARIANT = "capture-privacy"
EXECUTABLE = "nxrs-matter-camera"


def run(*command, cwd=ROOT, capture=False):
    return subprocess.run(
        command, cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_revision():
    if run("git", "status", "--porcelain", "--untracked-files=all", "--", *INPUTS, capture=True):
        raise ValueError("Commit or remove changes to the Matter build inputs first")
    return run("git", "rev-parse", "HEAD", capture=True).strip()


def build(args):
    if sys.platform not in ("darwin", "linux"):
        raise ValueError("Matter gateway candidates build on macOS and Linux")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Build output must be a new directory")
    source = args.sdk.resolve(strict=True)
    revision = source_revision()
    pins = json.loads(PINS.read_bytes())
    toolchain = "+" + pins["rust"]
    rustc = run("rustc", toolchain, "-vV", capture=True)
    target = next(line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: "))
    with tempfile.TemporaryDirectory(prefix="nxrs-matter-build-", dir=output.parent) as tmp:
        scratch = Path(tmp)
        sdk = scratch / "sdk"
        run("git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(sdk))
        run("git", "checkout", "--quiet", "--detach", pins["rs_matter"]["revision"], cwd=sdk)
        run(sys.executable, ROOT / "tools/prepare-matter-fixture.py",
            "--sdk", sdk, "--variant", VARIANT,
            "--webrtc-archive", args.webrtc_archive.resolve(strict=True),
            "--dnssd-archive", args.dnssd_archive.resolve(strict=True))
        target_dir = scratch / "target"
        run("cargo", toolchain, "build", "--release", "--locked",
            *(("--offline",) if args.offline else ()),
            "--target", target, "--target-dir", target_dir,
            "-p", "rs-matter-examples", "--bin", "webrtc_camera", "--features", "webrtc,astro-dnssd",
            cwd=sdk)
        executable = target_dir / target / "release/webrtc_camera"
        receipt = {
            "version": 1,
            "nxrs_revision": revision,
            "rs_matter_revision": pins["rs_matter"]["revision"],
            "variant": VARIANT,
            "rust": pins["rust"],
            "target": target,
            "pin_file_sha256": digest(PINS),
            "sdk_lock_sha256": digest(sdk / "Cargo.lock"),
            "executable": EXECUTABLE,
            "executable_sha256": digest(executable),
        }
        if source_revision() != revision:
            raise ValueError("The Nxrs source revision changed during the build")
        marker = scratch / "build.json"
        marker.write_text(json.dumps(receipt, indent=2) + "\n")
        output.mkdir(mode=0o700)
        shutil.copyfile(executable, output / EXECUTABLE)
        (output / EXECUTABLE).chmod(0o755)
        # This marker is written only once the executable is in place.
        marker.rename(output / "build.json")
    print(f"Matter gateway build ready in {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sdk", type=Path, required=True, help="local rs-matter Git repository")
    parser.add_argument("--webrtc-archive", type=Path, required=True)
    parser.add_argument("--dnssd-archive", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="use cached Cargo dependencies")
    build(parser.parse_args())
