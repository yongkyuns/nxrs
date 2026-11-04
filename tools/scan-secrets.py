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
        ]
        digest_root = 'tests/std-channel-perf/results/2026-11-03-esp32s3-simpleboot'
        digest_evidence = [
            f'{digest_root}/runs/{case}/{kind}.json'
            for case in ('latency-tight-1', 'latency-rom-digest-tight-1',
                         'latency-rom-digest-tight-2', 'latency-rom-digest-tight-3',
                         'latency-rom-digest-seed-1-1')
            for kind in ('build', 'analysis')
        ] + [f'{digest_root}/firmware-receipts/{case}/build.json'
             for case in ('latency', 'latency-rom-digest')]
        latency_root = 'tests/std-channel-perf/results/2026-11-03-esp32s3-latency-controls'
        latency_evidence = [f'{latency_root}/firmware-receipts/build.json'] + [
            f'{latency_root}/runs/{case}/{kind}.json'
            for case in ('01-tight', '02-seed-1-a', '03-seed-17', '04-fixed-100-w100-a',
                         '05-fixed-100-w101-a', '06-fixed-1000-w100-a', '07-fixed-1000-w101-a',
                         '08-seed-113', '09-seed-1-b', '10-fixed-1000-w101-b',
                         '11-fixed-1000-w100-b', '12-fixed-100-w101-b', '13-fixed-100-w100-b')
            for kind in ('build', 'analysis')
        ]
        progress_root = 'tests/channel-progress/results/2026-11-04-esp32s3'
        progress_evidence = [
            f'{progress_root}/runs/{family}/{case}/run/{kind}.json'
            for family, cases in (
                ('crossbeam', ('select-reserved', 'select-published')),
                ('std', ('mutex-admit-held', 'mutex-admit-released', 'mutex-take-held',
                         'mutex-take-released', 'condvar-close-wait',
                         'condvar-publish-before-wait', 'condvar-wait-publish')),
                ('fifo', ('fifo-select-reserved', 'fifo-select-published',
                          'fifo-try-recv-reserved', 'fifo-try-recv-published',
                          'fifo-try-send-reserved', 'fifo-try-send-reclaimed')),
            )
            for case in cases for kind in ('build', 'analysis', 'acceptance')
        ] + [f'{progress_root}/{family}-{kind}.json'
             for family in ('std', 'fifo') for kind in ('no-repair', 'physical')]
        exact_hash_evidence = digest_evidence + latency_evidence + progress_evidence

        def detected_at(paths, fields, rule, hidden, *, indent=2):
            # Scan independent file controls together, checking every location;
            # hundreds of scanner startups would exhaust the CI job budget.
            for relative in paths:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(fields, indent=indent) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            detected = {item['File'] for item in findings if item['RuleID'] == rule}
            if detected != set(paths) or any(value in output for value in hidden):
                raise ValueError("hash-field control missed a location or redaction failed")
            for relative in paths:
                (source / relative).write_text('{"description":"synthetic clean file"}\n')

        exclusion_paths = [
            'tests/rtos-bench/upstream.json', 'tests/detection/results/control/provenance.json',
            'tests/detection/results/control/firmware.json', 'tests/detection/results/control/validation.json',
            'tests/csi-radio/results/control.json', *channel_evidence, *exact_hash_evidence,
        ]
        for field, value, rule in [('access_token', token, 'github-pat'),
                                   ('api_key', alphabet * 4, 'generic-api-key')]:
            detected_at(exclusion_paths, {field: value}, rule, (value,))
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
        for relative in [
            'tests/std-channel-perf/results/2026-11-02-qemu/after-clock-fix/witness-reserved/credentials.json',
            'tests/std-channel-perf/results/2026-11-03-qemu/after-clock-fix/witness-reserved/build.json',
        ]:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({source_field: source_hash}, indent=2) + '\n')
            findings, output = execute(scanner, "dir", source, config)
            if not any(item.get("RuleID") == 'generic-api-key' for item in findings) or source_hash in output:
                raise ValueError("source-hash exclusion escaped its evidence path or redaction failed")
            path.unlink()
        # The newer P0/P1 archives repeat the same public patch bytes. Their
        # separate exceptions cover only the reviewed paths and one value.
        public_hash = hashlib.sha256((ROOT / source_field).read_bytes()).hexdigest()
        for relative in exact_hash_evidence:
            path = source / relative
            path.write_text(json.dumps({source_field: public_hash}, indent=2) + '\n')
        findings, _ = execute(scanner, "dir", source, config)
        if findings:
            raise ValueError("reviewed exact archive source hash was not recognized")
        for relative in exact_hash_evidence:
            (source / relative).write_text('{"description":"synthetic clean file"}\n')
        for fields, rule in (
            ({source_field: token}, 'github-pat'),
            ({source_field: source_hash}, 'generic-api-key'),
            ({source_field.replace('0006-', '0007-'): public_hash}, 'generic-api-key'),
            ({source_field: public_hash, 'api_key': source_hash}, 'generic-api-key'),
        ):
            # Single-field controls use the admitted line shape, so a broader
            # hash exception cannot pass merely because JSON braces prevented
            # matching. Neighboring fields must remain together on one line.
            detected_at(exact_hash_evidence, fields, rule, (token, source_hash),
                        indent=2 if len(fields) == 1 else None)
        wrong_paths = (
            f'{digest_root}/runs/latency-rom-digest-tight-4/build.json',
            f'{digest_root}/runs/latency-tight-1/credentials.json',
            f'{digest_root}/firmware-receipts/latency/analysis.json',
            digest_evidence[0].replace('2026-11-03', '2026-11-04'),
            f'{latency_root}/firmware-receipts/analysis.json',
            f'{latency_root}/runs/01-tight/acceptance.json',
            f'{latency_root}/runs/14-tight/build.json',
            f'{latency_root}/runs/04-fixed-100-w101-a/build.json',
            latency_evidence[1].replace('2026-11-03', '2026-11-04'),
            f'{progress_root}/runs/crossbeam/direct-reserved/run/build.json',
            f'{progress_root}/runs/std/fifo-select-reserved/run/build.json',
            f'{progress_root}/runs/fifo/fifo-try-send-published/run/build.json',
            f'{progress_root}/runs/std/mutex-admit-held/run/credentials.json',
            f'{progress_root}/runs/other/select-reserved/run/build.json',
            f'{progress_root}/std-report.json',
            f'{progress_root}/fifo-no-repair.json.bak',
            progress_evidence[0].replace('2026-11-04', '2026-11-05'),
        )
        detected_at(wrong_paths, {source_field: public_hash}, 'generic-api-key', ())
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
