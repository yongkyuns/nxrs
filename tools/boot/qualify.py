#!/usr/bin/env python3
"""Qualify the pinned, real MCUboot RSA-3072 bootutil on a disposable host."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
FEATURES = "sig-rsa3072,validate-primary-slot,hw-rollback-protection"
POLICY_TESTS = (
    "required_native_policy_is_enabled",
    "actual_primary_signature_positive_and_negative",
    "signed_header_body_and_protected_counter_tampering_are_rejected",
    "invalid_candidate_keeps_authenticated_primary",
    "trial_reverts_without_advancing_security_counter",
    "confirmation_advances_counter_and_refuses_old_signed_image",
)
CORE_TESTS = ("bad_secondary_slot", "basic_revert", "perm_with_fails",
              "perm_with_random_fails", "hw_prot_missing_security_cnt",
              "hw_prot_failed_security_cnt_check")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(path, expected):
    if path.is_symlink() or not path.is_file() or digest(path) != expected:
        raise ValueError("input identity mismatch")


def fetch(entry, directory):
    path = directory / entry["filename"]
    if path.exists() or path.is_symlink():
        verify(path, entry["sha256"])
        return path
    request = urllib.request.Request(entry["url"], headers={"User-Agent": "nxrs-boot-qualification"})
    with urllib.request.urlopen(request, timeout=60) as response, path.open("xb") as out:
        total = 0
        deadline = time.monotonic() + 180
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > 32 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("archive exceeds download budget")
            out.write(block)
    verify(path, entry["sha256"])
    return path


def extract(archive, destination):
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 5000 or sum(m.size for m in members) > 100 * 1024 * 1024:
            raise ValueError("archive exceeds expansion budget")
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or not (member.isfile() or member.isdir()) or member.name in names):
                raise ValueError("unsafe archive member")
            names.add(member.name)
        source.extractall(destination, members=members, filter="data")
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError("unexpected source layout")
    return children[0]


def run(command, cwd, environment, logs, name, timeout=600, pass_fds=()):
    # Logs are private. Secret pipes require an explicit FD allowlist; SDK calls
    # use the empty default, and command/environment values contain no token.
    out, err = logs / (name + ".out"), logs / (name + ".err")
    with out.open("xb") as stdout, err.open("xb") as stderr:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stdout,
                                 stderr=stderr, start_new_session=True, close_fds=True, pass_fds=pass_fds)
        try:
            deadline = time.monotonic() + timeout
            while child.poll() is None:
                if (time.monotonic() > deadline or out.stat().st_size > 16 * 1024 * 1024
                        or err.stat().st_size > 16 * 1024 * 1024):
                    raise ValueError("tool deadline or output budget exceeded")
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            status = child.returncode
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
    if out.stat().st_size > 16 * 1024 * 1024 or err.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("tool output exceeds qualification budget")
    return status, out.read_text(errors="strict")


def audit_result(status, output, negative=False):
    report = json.loads(output)
    found = report["vulnerabilities"]
    warnings = report["warnings"]
    if report["settings"]["ignore"] or report["database"]["advisory-count"] < 1:
        raise ValueError("incomplete or exempted advisory scan")
    if negative:
        ids = {item["advisory"]["id"] for item in found["list"]}
        if status == 0 or "RUSTSEC-2025-0009" not in ids:
            raise ValueError("affected upstream lockfile was not detected")
    elif status != 0 or found["found"] or found["count"] or found["list"] or any(warnings.values()):
        raise ValueError("host simulator advisory gate failed")
    return {"advisory_count": report["database"]["advisory-count"],
            "database_commit": report["database"]["last-commit"],
            "packages": report["lockfile"]["dependency-count"]}


def require_tests(output, names, prefix=""):
    for name in names:
        if not re.search(r"^test " + re.escape(prefix + name) + r" \.\.\. ok$", output, re.M):
            raise ValueError("required actual native test missing or unsuccessful")


def boot_state_records(raw):
    if len(raw) > 4 * 1024 * 1024 + 16 or raw[:8] != b"NXRSBT02" or len(raw) < 16:
        raise ValueError("invalid native boot state record")
    operations = int.from_bytes(raw[8:12], "little")
    revert_operations = int.from_bytes(raw[12:16], "little")
    if not (2 <= operations <= 4096 and 2 <= revert_operations <= 4096):
        raise ValueError("invalid native operation count")
    expected = ["initial", "pending", "trial", "reverted", "confirmed", "permanent"]
    expected += [prefix + str(index) for index in range(1, operations + 1)
                 for prefix in ("cut-", "trial-resumed-", "reverted-")]
    expected += [prefix + str(index) for index in range(1, revert_operations + 1)
                 for prefix in ("revert-cut-", "revert-resumed-")]
    records, offset = {}, 16
    for name in expected:
        begin = offset
        if offset >= len(raw):
            raise ValueError("missing native observation")
        count = raw[offset]
        offset += 1
        if count != len(name) or raw[offset:offset + count] != name.encode("ascii"):
            raise ValueError("wrong or reordered native observation")
        offset += count
        if offset + 148 > len(raw):
            raise ValueError("truncated native observation")
        slot = int.from_bytes(raw[offset:offset + 4], "little")
        if not 65536 <= slot <= 16 * 1024 * 1024:
            raise ValueError("invalid native slot size")
        records[name] = (begin, offset + 4, offset + 148)
        offset += 148
    if offset != len(raw):
        raise ValueError("unknown native observation")
    return operations, revert_operations, records


def qualify_update_protocol(work, environment, logs):
    fixture = work / "native-boot-states.bin"
    original = fixture.read_bytes()
    operations, revert_operations, records = boot_state_records(original)
    environment = dict(environment, CARGO_TARGET_DIR=str(work / "update-contract"),
                       CARGO_PROFILE_DEV_PANIC="unwind")
    status, output = run(["cargo", "+1.90.0", "build", "--locked", "-p", "nxrs-update", "--message-format=json"],
        ROOT, environment, logs, "update-contract", 180)
    if status:
        raise ValueError("production update contract build failed")
    artifacts = [entry for line in output.splitlines() if (entry := json.loads(line)).get("reason") == "compiler-artifact"
                 and entry["target"]["name"] == "update"]
    libraries = [Path(name) for item in artifacts for name in item["filenames"] if name.endswith(".rlib")]
    if len(libraries) != 1 or not libraries[0].resolve().is_relative_to(work.resolve()):
        raise ValueError("unbound update contract artifact")
    consumer = work / "update-protocol-test"
    status, _ = run(["rustc", "+1.90.0", "--test", "--edition=2021", str(ROOT / "tests/boot/update_protocol.rs"),
        "--extern", "update=" + str(libraries[0]), "-L", "dependency=" + str(work / "update-contract/debug/deps"),
        "-o", str(consumer)], ROOT, environment, logs, "update-protocol-build", 60)
    if status:
        raise ValueError("exact production trailer predicate compilation failed")
    command = [str(consumer), "--exact", "actual_sdk_boot_states_match_production_staging_policy"]
    status, output = run(command, work, environment, logs, "update-protocol-positive", 30)
    if status:
        raise ValueError("actual SDK trailers disagree with production staging admission")
    require_tests(output, ["actual_sdk_boot_states_match_production_staging_policy"])
    controls = []
    # Coherent native trailers relabeled as an unconfirmed trial must not satisfy
    # the independent state oracle. Also require every interruption observation.
    forged = bytearray(original)
    _, trial, _ = records["trial"]
    _, confirmed, _ = records["confirmed"]
    forged[trial:trial + 144] = original[confirmed:confirmed + 144]
    begin, _, end = records["cut-1"]
    try:
        for name, raw in (("coherent-confirmed-trailer-as-trial", forged),
                          ("missing-interruption-observation", original[:begin] + original[end:])):
            fixture.write_bytes(raw)
            status, output = run(command, work, environment, logs, "update-protocol-refusal-" + name, 30)
            if status == 0 or "actual_sdk_boot_states_match_production_staging_policy ... FAILED" not in output:
                raise ValueError("native trailer oracle corruption was not refused")
            controls.append(name)
    finally:
        fixture.write_bytes(original)
    if fixture.read_bytes() != original:
        raise ValueError("native observation restoration failed")
    status, output = run(command, work, environment, logs, "update-protocol-restored", 30)
    if status:
        raise ValueError("restored native state evidence failed")
    require_tests(output, ["actual_sdk_boot_states_match_production_staging_policy"])
    return {"scope": "actual-host-native-bootutil-trailers-versus-production-rust-predicates",
            "qualified": True, "simulator_geometry": "upstream-Stm32f4-variable-sector-model",
            "simulator_security_counter": True, "target_configuration_qualified": False,
            "total_trial_flash_operations": operations,
            "each_interrupted_trial_operation_resumed_and_reverted": operations,
            "total_revert_flash_operations": revert_operations,
            "each_interrupted_revert_operation_resumed": revert_operations,
            "required_observations": len(records), "observations_sha256": digest(fixture),
            "consumer_sha256": digest(consumer), "consumer_rust_panic_strategy": "unwind",
            "refusal_controls": controls,
            "restored_positive_passed": True, "physical_power_loss": False,
            "protected_device_counter": False, "activation_qualified": False}


def qualify(args):
    if sys.version_info < (3, 12) or platform.system() not in ("Linux", "Darwin"):
        raise ValueError("Python 3.12+ and a Linux/macOS qualification host required")
    update_protocol = getattr(args, "update_protocol", False)
    spec = json.loads((ROOT / "security/boot-dependencies.json").read_text())
    for relative, expected in spec["files"].items():
        verify(ROOT / relative, expected)
    update_names = ["tests/boot/update_states.rs", "tests/boot/update_protocol.rs", "hal/update/src/protocol.rs",
                    "hal/update/src/api.rs", "hal/update/src/lib.rs", "hal/update/Cargo.toml", "driver/firmware-image/src/lib.rs",
                    "driver/firmware-image/Cargo.toml", "driver/firmware-image/src/policy.rs",
                    "Cargo.toml", "Cargo.lock", "platform/hal-build.rs", "tools/platform_config.py"]
    update_inputs = {name: digest(ROOT / name) for name in update_names} if update_protocol else {}
    recipe = digest(Path(__file__))
    # New exclusive directory; never alters the repo's NuttX checkout, HOME or SDKs.
    work = args.work.absolute()
    work.mkdir(mode=0o700, parents=False)
    logs = work / "logs"
    logs.mkdir(mode=0o700)
    home = work / "home"
    home.mkdir(mode=0o700)
    (work / ".cargo").mkdir()
    (work / ".cargo/audit.toml").write_text("[advisories]\nignore = []\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home),
           "CARGO_HOME": str(work / "cargo"), "CARGO_TARGET_DIR": str(work / "target"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "RUST_LOG": "error", "CARGO_BUILD_JOBS": "2"}
    status, version = run(["rustc", "+1.90.0", "--version"], work, env, logs, "rust-version", 30)
    if status or not version.startswith("rustc 1.90.0 "):
        raise ValueError("pinned Rust compiler required")
    auditor = str(Path(shutil.which(args.auditor) or args.auditor).absolute())
    status, audit_version = run([auditor, "--version"], work, env, logs, "audit-version", 30)
    if status or audit_version.strip() != "cargo-audit 0.22.2":
        raise ValueError("pinned advisory scanner required")
    for name in ("mcuboot", "mbedtls"):
        entry = spec[name]
        archive = fetch(entry, work)
        entry["source_path"] = str(extract(archive, work / name))
    source = Path(spec["mcuboot"]["source_path"])
    audit = [auditor, "audit", "--json", "--deny", "unsound", "--file", str(source / "Cargo.lock")]
    if args.database:
        audit += ["--db", str(args.database.resolve())]
    status, output = run(audit, work, env, logs, "affected-lock", 180)
    audit_result(status, output, negative=True)
    status, _ = run(["patch", "--batch", "--fuzz=0", "-p1", "-i",
                     str(ROOT / "tests/boot/simulator.patch")], source, env, logs, "patch", 30)
    if status:
        raise ValueError("reviewed SDK fixture patch failed")
    shutil.copyfile(ROOT / "tests/boot/Cargo.lock", source / "Cargo.lock")
    (source / "sim/tests/nxrs").mkdir()
    shutil.copyfile(ROOT / "tests/boot/nxrs_policy.rs", source / "sim/tests/nxrs/policy.rs")
    if update_protocol:
        with (source / "sim/tests/nxrs/policy.rs").open("a") as module:
            module.write((ROOT / "tests/boot/update_states.rs").read_text())
        env["NXRS_BOOT_STATE_FIXTURE"] = str(work / "native-boot-states.bin")
    (source / "ext/mbedtls").rmdir()
    shutil.copytree(spec["mbedtls"]["source_path"], source / "ext/mbedtls")
    status, output = run(audit, work, env, logs, "maintained-lock", 180)
    advisory = audit_result(status, output)
    status, tests = run(["cargo", "+1.90.0", "test", "--locked", "--manifest-path",
                         str(source / "sim/Cargo.toml"), "--features", FEATURES],
                        work, env, logs, "native-tests", 900)
    if status:
        raise ValueError("actual native bootutil qualification failed")
    require_tests(tests, POLICY_TESTS, "image::nxrs_policy::")
    require_tests(tests, CORE_TESTS)
    protocol_result = None
    if update_protocol:
        require_tests(tests, ["actual_boot_trailers_and_interrupted_trial_revert_swaps"], "image::nxrs_policy::")
        protocol_result = qualify_update_protocol(work, env, logs)
    # Cargo may not rewrite the reviewed graph even after successful tests.
    verify(source / "Cargo.lock", spec["files"]["tests/boot/Cargo.lock"])
    report = {"schema": 1, "scope": "host-native-simulator-only", "qualified": True,
              "host": {"system": platform.system(), "machine": platform.machine()},
              "rust": version.strip(), "features": FEATURES.split(","),
              "mcuboot": {k: v for k, v in spec["mcuboot"].items() if k != "source_path"},
              "mbedtls": {k: v for k, v in spec["mbedtls"].items() if k != "source_path"},
              "inputs": dict(spec["files"], **{"tools/boot/qualify.py": recipe}),
              "rustsec": advisory,
              "required_policy_tests": list(POLICY_TESTS),
              "required_upstream_tests": list(CORE_TESTS),
              "affected_upstream_lock_detected": True,
              "physical_qualified": False, "production_firmware_built": False,
              "native_advisory_closure": False}
    if update_protocol:
        for name, expected in update_inputs.items():
            verify(ROOT / name, expected)
        report["inputs"].update(update_inputs)
        report["update_protocol"] = protocol_result
    verify(Path(__file__), recipe)
    with (work / "qualification.json").open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print("MCUboot host-native RSA-3072 policy and affected-lock controls passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="new disposable directory")
    parser.add_argument("--auditor", default="cargo-audit")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--update-protocol", action="store_true")
    args = parser.parse_args()
    try:
        qualify(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        print("MCUboot qualification failed; inspect private local logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
