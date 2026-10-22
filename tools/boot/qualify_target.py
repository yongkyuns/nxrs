#!/usr/bin/env python3
"""Disposable build-only qualification with a fresh synthetic application authority."""
import argparse
import json
import os
from pathlib import Path
import sys

import build_esp32s3 as builder
import qualify as process
import sign
import target_plan as target
import evidence

PROFILE = "esp32s3-camera-mcuboot"


def qualify(args):
    builder.checked_environment()
    work = target.fresh(args.work)
    logs = work / "logs"
    logs.mkdir(mode=0o700)
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "TMPDIR") if key in os.environ}
    environment.update(PYTHONNOUSERSITE="1", NXRS_BOOT_PYTHON=sys.executable)
    environment["PATH"] = str(Path(sys.executable).parent) + ":" + environment["PATH"]
    status, _ = process.run(["cargo", "+1.90.0", "firmware", "--platform", PROFILE,
        "--emit-boot-plan", "--out", str(work / "plan")], target.ROOT, environment, logs, "plan", 120)
    target.require(status == 0)
    data = target.plan(PROFILE)
    target.require(json.loads((work / "plan/signer-settings.json").read_bytes()) == data["settings"])
    authority = work / "authority"
    authority.mkdir(mode=0o700)
    # Random password stays in this process; no argv, environment, artifact or log.
    password = os.urandom(48).hex().encode("ascii")
    pin = sign.initialize(authority, data["settings"], password)
    del password
    public = authority / "policy.json"
    command = [sys.executable, str(target.ROOT / "tools/boot/build_esp32s3.py"), "build",
        "--platform", PROFILE, "--out", str(work / "boot"), "--policy", str(public), "--policy-sha256", pin]
    if args.cache:
        command.extend(("--cache", str(args.cache)))
    status, _ = process.run(command, target.ROOT, environment, logs, "boot", 600)
    if status != 0:
        import re
        error = (logs / "boot.err").read_text()
        locations = re.findall(r"^Boot build operation failed at ([a-z_]+[.]py:[0-9]+)$", error, re.M)
        if locations:
            print("Target boot stage refused at " + locations[-1], file=sys.stderr)
    target.require(status == 0)
    report = json.loads((work / "boot/boot-build.json").read_bytes())
    target.require(report["cross_build_qualified"] and report["public_policy_sha256"] == pin)
    boot = work / "boot"
    source = next((boot / "mcuboot-source").iterdir())
    hal = next((boot / "hal-source").iterdir())
    compiler = next((boot / "compiler-source").iterdir())
    captured = json.loads((boot / "official-advisory-capture.json").read_bytes())
    documents = {path.name: json.loads(path.read_bytes()) for path in (boot / "cyclonedx-schemas").glob("*.json")}
    controls = evidence.exercise_refusals(boot, report, source, hal, compiler, environment, captured, documents)
    target.require(controls["bundle_sha256"] == report["native_evidence"])
    if args.nuttx:
        # Existing frozen Linux SDK environment, never a global toolchain switch.
        script = 'source "$1"; exec cargo +1.90.0 firmware --app std-demo --platform "$2" --out "$3" --boot-policy "$4" --boot-policy-sha256 "$5"'
        status, _ = process.run(["bash", "-euc", script, "target-application",
            str(target.ROOT / "target/qemu-tools/environment.sh"), PROFILE,
            str(work / "application"), str(public), pin], target.ROOT, environment, logs, "application", 1200)
        target.require(status == 0)
        application = json.loads((work / "application/payload-build.json").read_bytes())
        target.require(application["policy_sha256"] == pin
                       and application["configuration_sha256"] == report["configuration_sha256"]
                       and application["payload_sha256"] == process.digest(work / "application/payload.bin"))
        # Retain only public bounded evidence, never SDK/example keys or images.
        (work / "application-build.json").write_bytes(target.canonical(application) + b"\n")
    print("Actual target build qualification passed; physical/ROM/protected-counter gates remain open")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--nuttx", action="store_true")
    args = parser.parse_args()
    try:
        qualify(args)
    except Exception:
        print("Target qualification failed; private stage logs retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
