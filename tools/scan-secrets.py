#!/usr/bin/env python3
"""Install a hash-pinned scanner or scan without disclosing secret-bearing output."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "security/tools.json").read_text())["gitleaks"]


def install(output):
    systems = {("Linux", "x86_64"): "linux_x64", ("Darwin", "arm64"): "darwin_arm64"}
    target = systems.get((platform.system(), platform.machine()))
    if target is None:
        raise ValueError("unsupported scanner install host; use a verified upstream binary")
    version = SETTINGS["version"]
    name = f"gitleaks_{version}_{target}.tar.gz"
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{version}/{name}"
    with urlopen(url, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SETTINGS[target + "_sha256"]:
        raise ValueError("scanner archive size/checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile() or member.size > 64 * 1024 * 1024:
            raise ValueError("invalid scanner archive executable")
        executable = archive.extractfile(member).read()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never extract archive paths or silently replace an existing executable.
    with output.open("xb") as stream:
        stream.write(executable)
    output.chmod(0o700)
    print("Installed hash-verified Gitleaks " + version)


def execute(scanner, mode, source, config):
    scanner = str(Path(scanner).resolve())
    version = subprocess.run([scanner, "version"], capture_output=True, text=True, check=True)
    if version.stdout.strip() != SETTINGS["version"]:
        raise ValueError("pinned scanner version required")
    with tempfile.TemporaryDirectory(prefix="nxrs-gitleaks-") as directory:
        directory = Path(directory)
        report = directory / "report.json"
        ignored = directory / "empty.ignore"
        ignored.write_text("")
        command = [scanner, mode, "--config", str(config), "--gitleaks-ignore-path", str(ignored),
                   "--ignore-gitleaks-allow", "--redact=100", "--no-banner", "--log-level", "error",
                   "--report-format", "json", "--report-path", str(report), "--timeout", "120"]
        if mode == "git":
            command += ["--log-opts=--all"]
        command.append(str(source))
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=150)
        # Never print scanner stdout/stderr or match/secret/line fields, even on errors.
        if result.returncode not in (0, 1) or not report.is_file():
            raise ValueError("scanner infrastructure failure; raw output withheld")
        findings = json.loads(report.read_text())
        if (not isinstance(findings, list)
                or not all(isinstance(item, dict) and isinstance(item.get("RuleID"), str)
                           and isinstance(item.get("File"), str) and isinstance(item.get("StartLine"), int)
                           for item in findings)
                or (result.returncode == 0) != (not findings)):
            raise ValueError("inconsistent scanner result")
        return findings, result.stdout + result.stderr + report.read_text()


def scan(scanner):
    findings, _ = execute(scanner, "git", ROOT, ROOT / "security/gitleaks.toml")
    for finding in findings:
        print(json.dumps({key: finding.get(key) for key in ("RuleID", "File", "StartLine", "Commit")}))
    if findings:
        raise ValueError(f"secret scan blocked: {len(findings)} findings; rotate real credentials privately")
    print("Secret scan passed: all available Git history; zero findings after reviewed hash-field exclusions")


def self_test(scanner):
    config = ROOT / "security/gitleaks.toml"
    with tempfile.TemporaryDirectory(prefix="nxrs-secret-control-") as directory:
        source = Path(directory)
        token = "gh" + "p_" + secrets.token_hex(18)
        # Synthetic: never provisioned with a service or persisted in the repository.
        path = source / "synthetic.txt"
        path.write_text('access_token = "' + token + '"\n')
        findings, output = execute(scanner, "dir", source, config)
        if not any(item.get("RuleID") == "github-pat" for item in findings) or token in output:
            raise ValueError("synthetic-secret detection/redaction control failed")
        # A real-shaped token in an allowlisted path must still be detected.
        path.unlink()
        path = source / "tests/rtos-bench/upstream.json"
        path.parent.mkdir(parents=True)
        path.write_text('access_token = "' + token + '"\n')
        findings, output = execute(scanner, "dir", source, config)
        if not findings or token in output:
            raise ValueError("hash-field exclusion hides a token or redaction failed")
        path.write_text('{"description":"synthetic clean file"}\n')
        findings, _ = execute(scanner, "dir", source, config)
        if findings:
            raise ValueError("clean-input control failed")
    print("Secret scanner controls passed: detection, narrow exclusions, redaction and clean input")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("install")
    provision.add_argument("--output", type=Path, required=True)
    for name in ("scan", "self-test"):
        command = commands.add_parser(name)
        command.add_argument("--scanner", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.output)
        elif args.command == "self-test":
            self_test(args.scanner)
        else:
            scan(args.scanner)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        # Even exception text can contain secret-bearing remote input or scanner output.
        print("Secret check failed; see safe finding locations above or verify tool/configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
