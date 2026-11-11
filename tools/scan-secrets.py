#!/usr/bin/env python3
"""Install a hash-pinned scanner or scan without disclosing secret-bearing output."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "security/tools.json").read_text())["gitleaks"]


class ControlFailure(ValueError):
    """Only fixed, locally constructed control identifiers may enter this error."""


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
    source = Path(source).resolve()
    config = Path(config).resolve()
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
        # Pinned Gitleaks preserves the input path in directory-mode findings.
        # Run from the source with '.' so controls exercise the same anchored
        # repository-relative paths as Git history, not absolute temp paths.
        # Reports and the explicit empty ignore file remain outside the source.
        command.append("." if mode == "dir" else str(source))
        result = subprocess.run(command, cwd=source if mode == "dir" else directory,
                                capture_output=True, text=True, timeout=150)
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


def reviewed_hash_controls(scanner, source, config, token, alphabet):
    # Full-history checkout is already required by the security workflow.
    # Verify immutable public source blobs, not just a hash-shaped string.
    imu = ("hal/imu/src/api.rs",
           "051b8789dfd02ecf97bf0b22538fca53c7fc4cf10791a2f7fa7461cd28a0a528",
           "e4a7de4ef3d71343c1a3fdbc2cc7ea62e4a49345")
    pthread = ("upstream/nuttx/patches/0006-flat-build-global-pthread-keys.patch",
               "776a412266a0ec3d30b2aef62f11ad05119b53dcc71bc836b33dfd0e93f84101",
               "18ddcdb162985348728fd5e5d167305e87e8e35f")
    for index, (_field, digest, blob) in enumerate((imu, pthread)):
        result = subprocess.run(["git", "cat-file", "blob", blob], cwd=ROOT,
                                capture_output=True, check=False, timeout=10)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != digest:
            raise ControlFailure(f"reviewed-source-{index}")
    cases = [
        ("tests/event-path-perf/results/2026-11-04-esp32s3/board/build/build.json", imu),
        ("tests/event-path-perf/results/2026-11-04-esp32s3/board/runs/pass-3-ready-std-try/build.json", pthread),
        ("tests/channel-progress/results/2026-11-04-esp32s3/runs/crossbeam/select-published/run/acceptance.json", pthread),
        ("tests/channel-progress/results/2026-11-04-esp32s3/fifo-physical.json", pthread),
    ]
    for index, (relative, (field, digest, _blob)) in enumerate(cases):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        line = '    ' + json.dumps(field) + ': ' + json.dumps(digest) + ',\n'
        # Include ordinary multiline JSON and CRLF. Gitleaks 8.30.1's line
        # slice can include the preceding newline; first-line-only controls
        # would miss that production-history difference.
        for variant, contents in enumerate((line, '{\n' + line + '}\n',
                                            '{\r\n' + line.replace('\n', '\r\n') + '}\r\n')):
            path.write_bytes(contents.encode('utf-8'))
            findings, _ = execute(scanner, "dir", source, config)
            if findings:
                raise ControlFailure(f"reviewed-hash-allow-{index}-{variant}")
        # A changed value, real-shaped token or different field is never exempt.
        for control, (key, value, rule) in enumerate([
                (field, alphabet * 4, "generic-api-key"),
                (field, token, "github-pat"),
                ("api_key", digest, "generic-api-key")]):
            path.write_text(json.dumps(key) + ': ' + json.dumps(value) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            if not any(item.get("RuleID") == rule for item in findings) or value in output:
                raise ControlFailure(f"reviewed-hash-reject-{index}-{control}")
        # Neither a second field on the line nor the same field outside the
        # bounded benchmark path may inherit the exception.
        path.write_text(line.rstrip() + ' "api_key": "' + alphabet * 4 + '"\n')
        findings, output = execute(scanner, "dir", source, config)
        if not any(item.get("RuleID") == "generic-api-key" for item in findings) or alphabet * 4 in output:
            raise ControlFailure(f"reviewed-hash-line-{index}")
        path.unlink()
        outside = source / "unreviewed.json"
        outside.write_text(line)
        findings, output = execute(scanner, "dir", source, config)
        if not any(item.get("RuleID") == "generic-api-key" for item in findings) or digest in output:
            raise ControlFailure(f"reviewed-hash-path-{index}")
        outside.unlink()



def reviewed_dropbear_history_controls(scanner, source, config, alphabet):
    """Protect exact historical false positives without hiding new secrets."""
    auth = "netutils/dropbear/port/nuttx_auth.c"
    patch = "netutils/dropbear/patch/0002-use-nuttx-passwd-auth.patch"
    digests = {auth: "bf80ad81244d769c98a3888742d4c8d829fc475593573f262e505db1477a3936", patch: "47f56660a9a2ef7fe1c1d218079e952c73d0ddd520ad6fc8eb1d49238bb93c47"}
    doc_line = "| Resource cost | Matched baseline, dependencies selected but not running, listening, authenticated shell, handshake/rekey/failure peaks, post-disconnect and post-stop |"
    # Fail closed if any reviewed historical source changes or is unavailable.
    for idx, (blob, kind) in enumerate([
        ("0e459071db6015776f5852d1916ee62a494d4757", "fixture"),
        ("99905d0c3206804f7c5ffafb125a1d2629525571", "guide"),
    ]):
        result = subprocess.run(["git", "cat-file", "blob", blob], cwd=ROOT,
                                capture_output=True, check=False, timeout=10)
        if result.returncode:
            raise ControlFailure(f"dropbear-object-{idx}")
        if kind == "fixture":
            original = json.loads(result.stdout)["apps_preimages"]
            if any(original.get(k) != v for k, v in digests.items()):
                raise ControlFailure("dropbear-hash-identity")
        elif doc_line not in result.stdout.decode("utf-8").splitlines():
            raise ControlFailure("dropbear-guide-identity")
    cases = [("tests/host/fixtures/dropbear-native/upstream.json",
              '    "' + k + '": "' + v + '",') for k, v in digests.items()]
    cases.append(("docs/dropbear-ssh.md", doc_line))
    for index, (relative, line) in enumerate(cases):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        for variant, contents in enumerate((line + "\n", "{\n" + line + "\n}\n",
                                            "{\r\n" + line + "\r\n}\r\n")):
            path.write_bytes(contents.encode("utf-8"))
            findings, _ = execute(scanner, "dir", source, config)
            if findings:
                raise ControlFailure(f"dropbear-allow-{index}-{variant}")
        # Extra API keys at precisely these paths must still be detected.
        path.write_text(line + ' api_key = "' + alphabet * 4 + '"\n')
        findings, output = execute(scanner, "dir", source, config)
        if not any(f.get("RuleID") == "generic-api-key" for f in findings) or alphabet * 4 in output:
            raise ControlFailure(f"dropbear-reject-{index}")
        path.unlink()

def self_test(scanner):
    config = ROOT / "security/gitleaks.toml"
    with tempfile.TemporaryDirectory(prefix="nxrs-secret-control-") as directory:
        source = Path(directory)
        # Positive controls must deterministically exceed the pinned rules'
        # entropy filters. Random hex strings can be legitimate nonmatches.
        alphabet = "0123456789abcdef"
        token = "gh" + "p_" + (alphabet * 3)[:36]
        # Synthetic: never provisioned with a service or persisted in the repository.
        path = source / "synthetic.txt"
        path.write_text('access_token = "' + token + '"\n')
        findings, output = execute(scanner, "dir", source, config)
        if not any(item.get("RuleID") == "github-pat" for item in findings) or token in output:
            raise ValueError("synthetic-secret detection/redaction control failed")
        # Every source-hash exclusion must still detect actual-shaped tokens.
        path.unlink()
        for relative in ['tests/rtos-bench/upstream.json','tests/detection/results/control/provenance.json',
                         'tests/detection/results/control/firmware.json','tests/detection/results/control/validation.json',
                         'tests/csi-radio/results/control.json',
                         'tests/std-channel-perf/results/control/build.json',
                         'tests/std-channel-perf/results/control/run/analysis.json']:
            path = source / relative
            path.parent.mkdir(parents=True,exist_ok=True)
            for field, value, rule in [('access_token', token, 'github-pat'),
                                       ('api_key', alphabet * 4, 'generic-api-key')]:
                path.write_text(field + ' = "' + value + '"\n')
                findings, output = execute(scanner, "dir", source, config)
                if not any(item.get("RuleID") == rule for item in findings) or value in output:
                    raise ValueError("hash-field exclusion hides a token or redaction failed")
            path.write_text('{"description":"synthetic clean file"}\n')
        reviewed_hash_controls(scanner, source, config, token, alphabet)
        reviewed_dropbear_history_controls(scanner, source, config, alphabet)
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
    except ControlFailure as error:
        # These identifiers contain only fixed local labels and loop indexes.
        print(f"Secret self-test failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        # Even exception text can contain secret-bearing remote input or scanner output.
        print("Secret check failed; see safe finding locations above or verify tool/configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
