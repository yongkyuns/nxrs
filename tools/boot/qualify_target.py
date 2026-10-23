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
import upstream_advisories as upstream

PROFILE = "esp32s3-camera-mcuboot"


def boot_failure_location(text):
    # The target builder's basename includes digits: build_esp32s3.py.
    import re
    locations = re.findall(r"^Boot build operation failed at ([a-z][a-z0-9_]*[.]py:[0-9]+)$", text, re.M)
    return locations[-1] if locations else None


def hook_failure_category(text):
    import re
    values = re.findall(r"^Boot hook origin refused: (retained-record|archive-member|archive-identity|symbol-bounds)$", text, re.M)
    return values[-1] if values else None


def qualify(args):
    builder.checked_environment()
    token = os.environ.pop("NXRS_ADVISORY_GITHUB_TOKEN", None)
    github_token = upstream.validated_token(token.encode("ascii")) if token is not None else None
    del token
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
    if not args.execute:
        del password
    public = authority / "policy.json"
    command = [sys.executable, str(target.ROOT / "tools/boot/build_esp32s3.py"), "build",
        "--platform", PROFILE, "--out", str(work / "boot"), "--policy", str(public), "--policy-sha256", pin]
    if args.cache:
        command.extend(("--cache", str(args.cache)))
    with upstream.token_pipe(github_token) as token_fd:
        if token_fd is not None:
            command.extend(("--github-token-fd", str(token_fd)))
        status, _ = process.run(command, target.ROOT, environment, logs, "boot", 600,
                                pass_fds=(token_fd,) if token_fd is not None else ())
    del github_token
    if status != 0:
        error = (logs / "boot.err").read_text()
        diagnostic = upstream.public_diagnostic(error)
        if diagnostic:
            print(diagnostic, file=sys.stderr)
        category = hook_failure_category(error)
        if category:
            print("Target hook origin refused: " + category, file=sys.stderr)
        location = boot_failure_location(error)
        if location:
            print("Target boot stage refused at " + location, file=sys.stderr)
        print("Target boot child exit status: " + str(status), file=sys.stderr)
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
    import qualify_preflight
    preflight = qualify_preflight.qualify(boot, public, pin, PROFILE, work / 'preflight')
    (work / 'preflight-qualification.json').write_bytes(target.canonical(preflight) + b'\n')
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
    import qualify_watchdog_handoff
    handoff = qualify_watchdog_handoff.qualify(work / 'watchdog-handoff', report['runtime_contract'], environment)
    handoff.update(boot_build_sha256=process.digest(boot / 'boot-build.json'),
                   configuration_sha256=report['configuration_sha256'], repository_head=report['repository_head'],
                   repository_has_changes=report['repository_has_changes'])
    (work / 'watchdog-handoff-qualification.json').write_bytes(target.canonical(handoff) + b'\n')
    if args.execute:
        import boot_execution
        with sign.directory(authority) as fd:
            public_data, signing_policy, key, _ = sign.state_open(fd, password, pin)
        del password
        execution = boot_execution.qualify(boot, data["settings"], pin, (public_data, signing_policy, key),
                                           work / 'execution', args.cache, args.emulator_library_path)
        (work / 'boot-execution-qualification.json').write_bytes(target.canonical(execution) + b'\n')
        if args.nuttx:
            import qualify_nuttx_boot
            startup = qualify_nuttx_boot.qualify(boot, work / 'application', data['settings'], pin,
                (public_data, signing_policy, key), work / 'nuttx-startup', work / 'execution', environment)
            (work / 'signed-nuttx-qualification.json').write_bytes(target.canonical(startup) + b'\n')
        del key
    print("Actual target build qualification passed; physical/ROM/protected-counter gates remain open")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--nuttx", action="store_true")
    parser.add_argument("--execute", action="store_true", help="execute the production bootloader and signed executable corpus in ESP32-S3 QEMU")
    parser.add_argument("--emulator-library-path", help="explicit private macOS emulator dependency directories")
    args = parser.parse_args()
    if args.emulator_library_path and not args.execute:
        parser.error("--emulator-library-path requires --execute")
    try:
        qualify(args)
    except Exception as error:
        # Only source locations from our own modules may leave private stage logs.
        # Exception text and external SDK tracebacks can contain paths or key data.
        import traceback
        frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                  if Path(frame.filename).parent.resolve() == Path(__file__).parent.resolve()]
        if frames:
            frame = frames[-2] if len(frames) > 1 and frames[-1].name == "require" else frames[-1]
            print("Target qualification refused at " + Path(frame.filename).name + ":" + str(frame.lineno), file=sys.stderr)
        print("Target qualification failed; private stage logs retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
