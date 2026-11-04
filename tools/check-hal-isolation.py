#!/usr/bin/env python3
"""Audit clean HAL builds, capability admission and target isolation."""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOST_PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
CAPABILITIES = ("camera", "storage", "transport", "led", "imu", "gnss", "wifi", "ble")


def host_dependencies(metadata, target_packages):
    """Read Cargo's resolved build/proc-macro edges, excluding dev-only edges.

    This is an allowance for host compilation, never for the target graph. The
    unfiltered metadata covers host dependencies even when cross-compiling.
    """
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    macros = {package["id"] for package in metadata["packages"]
              if any("proc-macro" in target["kind"] for target in package["targets"])}
    pending = []
    for identity in target_packages:
        for dep in nodes[identity]["deps"]:
            kinds = {kind["kind"] for kind in dep["dep_kinds"]}
            if "build" in kinds or (None in kinds and dep["pkg"] in macros):
                pending.append(dep["pkg"])
    allowed = set()
    while pending:
        identity = pending.pop()
        if identity in allowed:
            continue
        allowed.add(identity)
        for dep in nodes[identity]["deps"]:
            if any(kind["kind"] in {None, "build"} for kind in dep["dep_kinds"]):
                pending.append(dep["pkg"])
    return allowed


def packages(text, names, directory, target):
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert records and records[-1].get("reason") == "build-finished"
    assert records[-1]["success"], "Cargo build failed"
    artifacts = [r for r in records if r.get("reason") == "compiler-artifact"]
    assert artifacts, "no compiler-artifact evidence"
    directory = Path(directory).resolve()
    target_directory = directory / target
    compiled = {"target": set(), "host": set(), "build_scripts": set()}
    for artifact in artifacts:
        identity = artifact["package_id"]
        assert identity in names, f"unknown Cargo artifact package: {identity}"
        paths = [Path(path).resolve() for path in artifact["filenames"]]
        assert paths and all(path.is_relative_to(directory) for path in paths), \
            f"artifact outside the audited output directory: {artifact}"
        target_paths = [path.is_relative_to(target_directory) for path in paths]
        kinds = artifact["target"]["kind"]
        if all(target_paths):
            assert not {"custom-build", "proc-macro"}.intersection(kinds), \
                f"host-only artifact in target output: {artifact}"
            compiled["target"].add(identity)
        else:
            assert not any(target_paths), f"mixed host/target artifact paths: {artifact}"
            scope = "build_scripts" if "custom-build" in kinds else "host"
            compiled[scope].add(identity)
    return compiled


def check(text, names, expected, host, directory, target):
    actual = packages(text, names, directory, target)
    assert actual["target"] == expected, \
        f"wrong compiled target dependency set: {actual['target']} != {expected}"
    assert actual["host"] <= host, \
        f"unexpected host dependencies: {actual['host'] - host}"
    assert actual["build_scripts"] <= expected | host, \
        f"unexpected build scripts: {actual['build_scripts'] - expected - host}"
    return actual


def check_selection(text, names, package, provider):
    reports = [r for line in text.splitlines() if line.strip()
               if (r := json.loads(line)).get("reason") == "build-script-executed"
               and names.get(r["package_id"]) == package]
    assert len(reports) == 1, "missing/duplicate platform build-script evidence"
    cfgs = [cfg for cfg in reports[0]["cfgs"] if cfg.startswith(("hal_", "nxrs_hal_"))]
    expected = [] if provider == "none" else ["hal_mock" if provider == "mock" else "hal_enabled"]
    assert cfgs == expected, f"wrong capability selection: {cfgs} != {expected}"


def oracle(name):
    path = ROOT / "tools" / f"check-{name}-isolation.py"
    spec = importlib.util.spec_from_file_location(f"{name}_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def self_test():
    names = {"hal": "nxrs-camera", "common": "nxrs-hal-common", "bad": "retired-provider",
             "registry-driver": "driver", "registry-build-dependency": "build-support"}
    directory = Path(tempfile.gettempdir()).resolve() / "hal-isolation-fixture"
    target = "fixture-target"
    def fixture(ids, host=(), provider="none", success=True):
        return "\n".join(json.dumps(r) for r in [
            *({"reason": "compiler-artifact", "package_id": i, "target": {"kind": ["lib"]},
               "filenames": [str(directory / target / "debug/deps" / (i + ".rlib"))]} for i in ids),
            *({"reason": "compiler-artifact", "package_id": i, "target": {"kind": ["lib"]},
               "filenames": [str(directory / "debug/deps" / (i + ".rlib"))]} for i in host),
            {"reason": "build-script-executed", "package_id": "hal", "cfgs": ([] if provider == "none" else ["hal_mock" if provider == "mock" else "hal_enabled"])},
            {"reason": "build-finished", "success": success}])
    expected = {"hal"}
    host = {"registry-build-dependency"}
    actual = check(fixture(["hal"], host), names, expected, host, directory, target)
    assert actual["host"] == host, "host dependency was omitted from the evidence"
    check_selection(fixture(["hal"]), names, "nxrs-camera", "none")
    for value in (fixture(["common"]), fixture(["hal", "common"]), fixture(["hal", "bad"]),
                  fixture(["hal", "registry-driver"]), fixture(["hal", "registry-build-dependency"]),
                  fixture(["hal", "unknown"]), fixture(["hal"], ["registry-driver"]),
                  fixture(["hal"], success=False), ""):
        try:
            check(value, names, expected, host, directory, target)
        except (AssertionError, KeyError):
            pass
        else:
            raise AssertionError("accepted incomplete/contaminated build evidence")
    try:
        check_selection(fixture(["hal"], provider=HOST_PLATFORM), names, "nxrs-camera", "none")
    except AssertionError:
        pass
    else:
        raise AssertionError("accepted incorrect provider cfg")
    print("PASS: host/target separation, unknown/contaminated build and wrong-selection rejection controls")


def run(out, capabilities=CAPABILITIES):
    out = out.resolve()
    assert not out.exists(), "use a fresh output directory, not cached build evidence"
    out.mkdir(parents=True)
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version=1"], cwd=ROOT, text=True))
    names = {p["id"]: p["name"] for p in metadata["packages"]}
    workspace = {names[identity]: identity for identity in metadata["workspace_members"]}
    compiler = subprocess.check_output([os.environ.get("RUSTC", "rustc"), "-vV"], text=True)
    host_target = next(line.removeprefix("host: ") for line in compiler.splitlines()
                       if line.startswith("host: "))
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (out / "source.txt").write_text(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True))
    source = out / "source.raw"
    source.write_bytes(bytes([7] * 4 + [9] * 4))
    report = []
    for capability in capabilities:
        package = f"nxrs-{capability}"
        expected = {package}
        if capability in {"wifi", "ble", "imu", "gnss"}:
            expected.add("nxrs-messaging")
            if capability in {"wifi", "ble"}:
                expected.add("nxrs-hal-common")
        if capability == "storage":
            expected.add("nxrs-camera")
        expected = {workspace[name] for name in expected}
        host = host_dependencies(metadata, expected)
        cases = [("contract-core", None, "none", "thumbv6m-none-eabi", None),
                 ("contract-wasm", None, "none", "wasm32-unknown-unknown", None)]
        if capability not in {"wifi", "ble"}:
            cases.append(("mock-wasm", "mock", "mock", "wasm32-unknown-unknown", None))
        if capability in {"camera", "storage", "transport"}:
            example = {"camera": "native_replay", "storage": "native_record", "transport": "native_send"}[capability]
            cases.append((HOST_PLATFORM, HOST_PLATFORM, HOST_PLATFORM, None, example))
        for label, profile, provider, target, example in cases:
            label = f"{capability}-{label}"
            target = target or host_target
            with tempfile.TemporaryDirectory(prefix=label + "-", dir=out) as directory:
                env = {**os.environ, "CARGO_TARGET_DIR": directory}
                env.pop("NXRS_PLATFORM", None)
                if profile:
                    env["NXRS_PLATFORM"] = profile
                command = ["cargo", "build", "--locked", "-p", package]
                command += ["--example", example] if example else ["--lib"]
                # Even native builds use an explicit target: Cargo then puts
                # target libraries and host build dependencies in distinct dirs.
                command += ["--target", target]
                command += ["--message-format=json-render-diagnostics"]
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
                (out / (label + ".jsonl")).write_text(result.stdout)
                (out / (label + ".stderr")).write_text(result.stderr)
                assert result.returncode == 0, result.stderr
                compiled = check(result.stdout, names, expected, host, directory, target)
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
                report.append({"case": label, "platform": profile, "provider": provider, "target": target,
                               "compiled_packages": sorted({names[identity] for ids in compiled.values() for identity in ids}),
                               "artifacts": {scope: [{"id": identity, "name": names[identity]} for identity in sorted(ids)]
                                             for scope, ids in compiled.items()}})
    for profile, target in [(HOST_PLATFORM, "wasm32-unknown-unknown"), ("esp32s3-wifi", None)]:
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
        for profile, provider in [(HOST_PLATFORM, HOST_PLATFORM), ("mock", "mock"), (None, "none")]:
            env = {**os.environ, "CARGO_TARGET_DIR": directory}
            env.pop("NXRS_PLATFORM", None)
            if profile:
                env["NXRS_PLATFORM"] = profile
            result = subprocess.run(["cargo", "check", "--locked", "-p", "nxrs-camera", "--lib",
                                     "--message-format=json-render-diagnostics"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stderr
            check_selection(result.stdout, names, "nxrs-camera", provider)
        env["RUSTFLAGS"] = '--cfg hal_mock'
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
