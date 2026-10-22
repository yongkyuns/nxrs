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


def run(command, cwd, environment, logs, name, timeout=600):
    # Logs are private, never uploaded. Inputs contain only public SDK/test fixtures.
    out, err = logs / (name + ".out"), logs / (name + ".err")
    with out.open("xb") as stdout, err.open("xb") as stderr:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stdout,
                                 stderr=stderr, start_new_session=True)
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


def qualify(args):
    if sys.version_info < (3, 12) or platform.system() not in ("Linux", "Darwin"):
        raise ValueError("Python 3.12+ and a Linux/macOS qualification host required")
    spec = json.loads((ROOT / "security/boot-dependencies.json").read_text())
    for relative, expected in spec["files"].items():
        verify(ROOT / relative, expected)
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
    # Cargo may not rewrite the reviewed graph even after successful tests.
    verify(source / "Cargo.lock", spec["files"]["tests/boot/Cargo.lock"])
    report = {"schema": 1, "scope": "host-native-simulator-only", "qualified": True,
              "host": {"system": platform.system(), "machine": platform.machine()},
              "rust": version.strip(), "features": FEATURES.split(","),
              "mcuboot": {k: v for k, v in spec["mcuboot"].items() if k != "source_path"},
              "mbedtls": {k: v for k, v in spec["mbedtls"].items() if k != "source_path"},
              "inputs": dict(spec["files"], **{"tools/boot/qualify.py": digest(Path(__file__))}),
              "rustsec": advisory,
              "required_policy_tests": list(POLICY_TESTS),
              "required_upstream_tests": list(CORE_TESTS),
              "affected_upstream_lock_detected": True,
              "physical_qualified": False, "production_firmware_built": False,
              "native_advisory_closure": False}
    with (work / "qualification.json").open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print("MCUboot host-native RSA-3072 policy and affected-lock controls passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="new disposable directory")
    parser.add_argument("--auditor", default="cargo-audit")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    try:
        qualify(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        print("MCUboot qualification failed; inspect private local logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
