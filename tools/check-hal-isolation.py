#!/usr/bin/env python3
"""Audit clean HAL builds and the actual platform cfg emitted by Cargo."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = ("camera", "storage", "transport", "led", "imu", "gnss", "wifi", "ble")


def packages(text, names):
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert records and records[-1].get("reason") == "build-finished"
    assert records[-1]["success"], "Cargo build failed"
    artifacts = [r for r in records if r.get("reason") == "compiler-artifact"]
    assert artifacts, "no compiler-artifact evidence"
    return {names[r["package_id"]] for r in artifacts}


def check(text, names, expected):
    actual = packages(text, names)
    assert actual == expected, f"wrong compiled dependency set: {actual} != {expected}"
    return sorted(actual)


def check_selection(text, names, package, provider):
    reports = [r for line in text.splitlines() if line.strip()
               if (r := json.loads(line)).get("reason") == "build-script-executed"
               and names[r["package_id"]] == package]
    assert len(reports) == 1, "missing/duplicate platform build-script evidence"
    cfgs = [cfg for cfg in reports[0]["cfgs"] if cfg.startswith("nxrs_provider=")]
    assert cfgs == [f'nxrs_provider="{provider}"'], f"wrong platform selection: {cfgs}"


def oracle(name):
    path = ROOT / "tools" / f"check-{name}-isolation.py"
    spec = importlib.util.spec_from_file_location(f"{name}_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def self_test():
    names = {"hal": "nxrs-camera", "common": "nxrs-hal-common", "bad": "retired-provider"}
    def fixture(ids, provider="none", success=True):
        return "\n".join(json.dumps(r) for r in [
            *({"reason": "compiler-artifact", "package_id": i} for i in ids),
            {"reason": "build-script-executed", "package_id": "hal", "cfgs": [f'nxrs_provider="{provider}"']},
            {"reason": "build-finished", "success": success}])
    expected = {"nxrs-camera", "nxrs-hal-common"}
    check(fixture(["hal", "common"]), names, expected)
    check_selection(fixture(["hal", "common"]), names, "nxrs-camera", "none")
    for value in (fixture(["hal"]), fixture(["hal", "common", "bad"]),
                  fixture(["hal", "common"], success=False), ""):
        try:
            check(value, names, expected)
        except (AssertionError, KeyError):
            pass
        else:
            raise AssertionError("accepted incomplete/contaminated build evidence")
    try:
        check_selection(fixture(["hal", "common"], "native"), names, "nxrs-camera", "none")
    except AssertionError:
        pass
    else:
        raise AssertionError("accepted incorrect provider cfg")
    print("PASS: incomplete/contaminated build and wrong-selection rejection controls")


def run(out, capabilities=CAPABILITIES):
    out = out.resolve()
    assert not out.exists(), "use a fresh output directory, not cached build evidence"
    out.mkdir(parents=True)
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version=1", "--no-deps"], cwd=ROOT, text=True))
    names = {p["id"]: p["name"] for p in metadata["packages"]}
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (out / "source.txt").write_text(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True))
    source = out / "source.raw"
    source.write_bytes(bytes([7] * 4 + [9] * 4))
    report = []
    for capability in capabilities:
        package = f"nxrs-{capability}"
        expected = {package, "nxrs-hal-common"}
        if capability == "storage":
            expected.add("nxrs-camera")
        cases = [("contract-core", None, "none", "thumbv6m-none-eabi", None),
                 ("contract-wasm", None, "none", "wasm32-unknown-unknown", None)]
        if capability not in {"wifi", "ble"}:
            cases.append(("mock-wasm", "mock", "mock", "wasm32-unknown-unknown", None))
        if capability in {"camera", "storage", "transport"}:
            example = {"camera": "native_replay", "storage": "native_record", "transport": "native_send"}[capability]
            cases.append(("native", "native", "native", None, example))
        for label, profile, provider, target, example in cases:
            label = f"{capability}-{label}"
            with tempfile.TemporaryDirectory(prefix=label + "-", dir=out) as directory:
                env = {**os.environ, "CARGO_TARGET_DIR": directory}
                env.pop("NXRS_PLATFORM", None)
                if profile:
                    env["NXRS_PLATFORM"] = profile
                command = ["cargo", "build", "--locked", "-p", package]
                command += ["--example", example] if example else ["--lib"]
                if target:
                    command += ["--target", target]
                command += ["--message-format=json-render-diagnostics"]
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
                (out / (label + ".jsonl")).write_text(result.stdout)
                (out / (label + ".stderr")).write_text(result.stderr)
                assert result.returncode == 0, result.stderr
                compiled = set(expected)
                if target == "thumbv6m-none-eabi" and capability in {"camera", "storage"}:
                    compiled.add("nxrs-nuttx-support")  # shared ABI owner, not a provider
                check(result.stdout, names, compiled)
                check_selection(result.stdout, names, package, provider)
                records = [json.loads(line) for line in result.stdout.splitlines()]
                assert not any(r.get("fresh", False) for r in records if r.get("reason") == "compiler-artifact"), "cached evidence"
                if example:
                    executables = [r["executable"] for r in records if r.get("reason") == "compiler-artifact" and r.get("executable")]
                    assert len(executables) == 1
                    if capability == "transport":
                        oracle("transport").execute(executables[0], out)
                    else:
                        destination = source if capability == "camera" else out / "records"
                        execution = subprocess.run([executables[0], str(destination)], check=True, capture_output=True, text=True, timeout=15)
                        assert execution.stdout.strip() == capability.upper() + "_ONLY_PASS"
                        if capability == "storage":
                            oracle("storage").check_records(destination)
                report.append({"case": label, "platform": profile, "provider": provider, "compiled_packages": sorted(compiled)})
    for profile, target in [("native", "wasm32-unknown-unknown"), ("esp32s3-wifi", None)]:
        with tempfile.TemporaryDirectory(prefix="reject-", dir=out) as directory:
            command = ["cargo", "check", "--locked", "-p", "nxrs-led", "--lib"]
            if target:
                command += ["--target", target]
            result = subprocess.run(command, cwd=ROOT,
                                    env={**os.environ, "NXRS_PLATFORM": profile, "CARGO_TARGET_DIR": directory},
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode != 0 and "does not support target OS" in result.stderr, "missing platform/target rejection"
    # Reuse one Cargo cache: changing the environment must rerun selection, not
    # leave an old implementation active in a developer's incremental build.
    with tempfile.TemporaryDirectory(prefix="switch-", dir=out) as directory:
        for profile, provider in [("native", "native"), ("mock", "mock"), (None, "none")]:
            env = {**os.environ, "CARGO_TARGET_DIR": directory}
            env.pop("NXRS_PLATFORM", None)
            if profile:
                env["NXRS_PLATFORM"] = profile
            result = subprocess.run(["cargo", "check", "--locked", "-p", "nxrs-camera", "--lib",
                                     "--message-format=json-render-diagnostics"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stderr
            check_selection(result.stdout, names, "nxrs-camera", provider)
        env["RUSTFLAGS"] = '--cfg nxrs_provider="mock"'
        result = subprocess.run(["cargo", "check", "--locked", "-p", "nxrs-camera", "--lib"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode != 0 and "NXRS_PLATFORM" in result.stderr, "manual cfg bypass accepted"
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(report)} clean builds, native oracles, incremental platform switches and rejection controls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability", choices=CAPABILITIES, action="append")
    parser.add_argument("--out", type=Path, default=ROOT / "target/hal-isolation")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test() if args.self_test else run(args.out, args.capability or CAPABILITIES)
