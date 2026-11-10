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
        # Directory controls must use repository-relative paths, like git mode.
        # Absolute source arguments bypass anchored allowlist paths and would
        # make exclusion tests pass without actually exercising the exclusions.
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
        channel_evidence = [
            'tests/std-channel-perf/results/2026-11-02-qemu/after-clock-fix/witness-reserved/build.json',
            'tests/std-channel-perf/results/2026-11-02-qemu/after-clock-fix/witness-reserved/run/analysis.json',
            'tests/std-channel-perf/results/2026-11-02-esp32s3/runs/witness-reserved-1/build.json',
            'tests/std-channel-perf/results/2026-11-02-esp32s3/runs/witness-reserved-1/analysis.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-latency-controls/firmware-receipts/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-latency-controls/runs/01-tight/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-latency-controls/runs/01-tight/analysis.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/firmware-receipts/latency-rom-digest/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/runs/latency-rom-digest-tight-1/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/runs/latency-rom-digest-tight-1/analysis.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/runs/crossbeam/select-reserved/run/build.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/runs/std/mutex-admit-held/run/analysis.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/runs/fifo/fifo-select-published/run/acceptance.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/std-no-repair.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/std-physical.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/fifo-no-repair.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/fifo-physical.json',
        ]
        for relative in ['tests/rtos-bench/upstream.json','tests/detection/results/control/provenance.json',
                         'tests/detection/results/control/firmware.json','tests/detection/results/control/validation.json',
                         'tests/csi-radio/results/control.json', *channel_evidence]:
            path = source / relative
            path.parent.mkdir(parents=True,exist_ok=True)
            for field, value, rule in [('access_token', token, 'github-pat'),
                                       ('api_key', alphabet * 4, 'generic-api-key')]:
                path.write_text(json.dumps({field: value}, indent=2) + '\n')
                findings, output = execute(scanner, "dir", source, config)
                if not any(item.get("RuleID") == rule for item in findings) or value in output:
                    raise ValueError("hash-field exclusion hides a token or redaction failed")
            path.write_text('{"description":"synthetic clean file"}\n')
        # The reviewed exception must admit precisely the source-hash line,
        # without suppressing sensitive fields on the same line or nearby files.
        source_field = 'upstream/nuttx/patches/0006-flat-build-global-pthread-keys.patch'
        source_hash = alphabet * 4
        for relative in channel_evidence:
            path = source / relative
            path.write_text(json.dumps({source_field: source_hash}, indent=2) + '\n')
            findings, _ = execute(scanner, "dir", source, config)
            if findings:
                raise ValueError("reviewed source-hash exclusion control failed")
            path.write_text(json.dumps({source_field: source_hash, 'api_key': source_hash}) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            if not any(item.get("RuleID") == 'generic-api-key' for item in findings) or source_hash in output:
                raise ValueError("source-hash line exclusion hides another field or redaction failed")
            path.write_text('{"description":"synthetic clean file"}\n')
        # The path exception does not broaden the exact field/value shape.
        # Exercise these at an admitted new cohort-report path.
        path = source / channel_evidence[-1]
        for field, value in [
            (source_field.replace('0006-', '0007-'), source_hash),
            (source_field, source_hash[:-1]),
            (source_field, source_hash.replace('f', 'g')),
        ]:
            path.write_text(json.dumps({field: value}, indent=2) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            if not any(item.get("RuleID") == 'generic-api-key' for item in findings) or value in output:
                raise ValueError("source-hash exclusion escaped its field/value shape or redaction failed")
        path.write_text('{"description":"synthetic clean file"}\n')
        for relative in [
            'tests/std-channel-perf/results/2026-11-02-qemu/after-clock-fix/witness-reserved/credentials.json',
            'tests/std-channel-perf/results/2026-11-03-qemu/after-clock-fix/witness-reserved/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-other/runs/01-tight/build.json',
            'tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot/runs/latency-rom-digest-tight-1/acceptance.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/runs/std/mutex-admit-held/run/credentials.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/runs/other/example/run/build.json',
            'tests/channel-progress/results/2026-11-04-esp32s3/std-report.json',
            'tests/channel-progress/results/2026-11-05-esp32s3/runs/std/mutex-admit-held/run/build.json',
        ]:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({source_field: source_hash}, indent=2) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            if not any(item.get("RuleID") == 'generic-api-key' for item in findings) or source_hash in output:
                raise ValueError("source-hash exclusion escaped its evidence path or redaction failed")
            path.unlink()
        # P2's completed physical archive has nineteen reviewed build receipts.
        # The two admitted values were checked against their public Git source
        # blobs; every other value/field/path remains subject to the same scan.
        event_root = 'tests/event-path-perf/results/2026-11-04-esp32s3/board'
        event_evidence = [f'{event_root}/build/build.json'] + [
            f'{event_root}/runs/pass-{number}-{state}-{method}-try/build.json'
            for number in (1, 2, 3) for state in ('ready', 'blocked')
            for method in ('std', 'messaging', 'crossbeam')
        ]
        # These are historical archive values, independent of later legitimate
        # edits to the current source files. Their Git-blob provenance was
        # checked before adding this exact-value exception.
        reviewed_hashes = (
            '051b8789dfd02ecf97bf0b22538fca53c7fc4cf10791a2f7fa7461cd28a0a528',
            '776a412266a0ec3d30b2aef62f11ad05119b53dcc71bc836b33dfd0e93f84101',
        )
        public_fields = dict(zip(('hal/imu/src/api.rs', source_field), reviewed_hashes))

        def detected_at(paths, fields, rule, hidden, *, indent=2):
            # Reuse P2's batched control pattern, checking every location rather
            # than launching a separate scanner for each repeated receipt.
            for relative in paths:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(fields, indent=indent) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            detected = {item['File'] for item in findings if item['RuleID'] == rule}
            if detected != set(paths) or any(value in output for value in hidden):
                raise ValueError("exact public-hash control missed a location or redaction failed")
            for relative in paths:
                (source / relative).write_text('{"description":"synthetic clean file"}\n')

        for relative in event_evidence:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(public_fields, indent=2) + '\n')
        findings, _ = execute(scanner, "dir", source, config)
        if findings:
            raise ValueError("reviewed P2 public source hashes were not recognized")
        for relative in event_evidence:
            (source / relative).write_text('{"description":"synthetic clean file"}\n')
        for field, value, rule in (
            ('access_token', token, 'github-pat'), ('api_key', source_hash, 'generic-api-key'),
        ):
            detected_at(event_evidence, {**public_fields, field: value}, rule, (value,))
        for field, public_hash in public_fields.items():
            changed_hash = public_hash[:-1] + ('0' if public_hash[-1] != '0' else '1')
            wrong_field = field.replace('api.rs', 'api2.rs').replace('0006-', '0007-')
            for fields, rule, hidden in (
                ({field: token}, 'github-pat', (token,)),
                ({field: changed_hash}, 'generic-api-key', (changed_hash,)),
                ({wrong_field: public_hash}, 'generic-api-key', ()),
                ({field: public_hash, 'api_key': source_hash}, 'generic-api-key', (source_hash,)),
            ):
                detected_at(event_evidence, fields, rule, hidden,
                            indent=2 if len(fields) == 1 else None)
        wrong_paths = (
            f'{event_root}/runs/pass-0-ready-std-try/build.json',
            f'{event_root}/runs/pass-4-ready-std-try/build.json',
            f'{event_root}/runs/pass-1-other-std-try/build.json',
            f'{event_root}/runs/pass-1-ready-other-try/build.json',
            f'{event_root}/runs/pass-1-ready-std-try/analysis.json',
            f'{event_root}/runs/pass-1-ready-std-try/credentials.json',
            f'{event_root}/runs/pass-1-ready-std-try/build.json.bak',
            f'{event_root}/build/analysis.json',
            f'{event_root}/build.json',
            event_evidence[0].replace('2026-11-04', '2026-11-05'),
        )
        detected_at(wrong_paths, public_fields, 'generic-api-key', ())
        for relative in wrong_paths:
            (source / relative).unlink()
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
