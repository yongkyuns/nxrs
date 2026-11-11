#!/usr/bin/env python3
"""Build/capture/replay finite current-source owner witnesses; never flash a board."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = {
    # attempted, admitted, rejected, handled, discarded, joined, native waits
    "std-blocked-delivery": (16, 16, 0, 16, 0, 2, 16),
    "std-full-progress": (3, 2, 1, 2, 0, 2, 0),
    "std-disconnect": (0, 0, 0, 0, 0, 1, 1),
    "owner-independent-inputs": (3, 3, 0, 3, 0, 3, 3),
    "owner-full-stop": (4, 2, 2, 0, 2, 1, 0),
    "owner-fairness-deadline": (23, 23, 0, 21, 2, 2, 0),
    "owner-failure-recovery": (2, 1, 1, 0, 1, 1, 0),
}
ROLES = {
    "std-blocked-delivery": [("std-producer", 120), ("std-receiver", 180), ("coordinator", 220)],
    "std-full-progress": [("std-producer", 180), ("std-receiver", 120), ("coordinator", 220)],
    "std-disconnect": [("std-receiver", 180), ("coordinator", 220)],
    "owner-independent-inputs": [("important-provider", 180), ("ordinary-provider", 180), ("selected-owner", 180), ("coordinator", 180)],
    "owner-full-stop": [("selected-owner", 180), ("coordinator", 180)],
    "owner-fairness-deadline": [("selected-owner", 180), ("selected-owner", 180), ("coordinator", 180)],
    "owner-failure-recovery": [("selected-owner", 180), ("coordinator", 180)],
}
FIELDS = ("attempted", "admitted", "rejected", "handled", "discarded", "joined", "required_waits")
FAULT = re.compile(r"\b(panic|panicked|assertion|hardfault|dump_assert|watchdog expired|segmentation fault|aborted|fatal error)\b", re.I)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def unique_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field {key}")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def contained(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("unsafe evidence path")
    candidate = root / path
    if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
        raise ValueError("symlink evidence is not supported")
    if not candidate.is_file():
        raise ValueError(f"missing evidence {relative}")
    return candidate


def inventory():
    # Includes actual owner/controller, transitive in-repo support, normal
    # frontend, ABI/SDK/kernel patches and every product profile. Captured files
    # are exact build inputs, not a claim that every one links into the image.
    paths = []
    for directory in (HERE, ROOT / "tests/nuttx-std", ROOT / "hal", ROOT / "shared",
                      ROOT / "driver", ROOT / "platform", ROOT / "upstream"):
        paths.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
                     and not any(part in ("target", "__pycache__", "results") for part in p.parts)
                     and (p.suffix in (".rs", ".c", ".h", ".toml", ".lock", ".py", ".sh", ".json", ".ld", ".patch", ".defs")
                          or p.name in ("Makefile", "Kconfig")))
    paths += [ROOT / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", ".cargo/config.toml")]
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(paths))}


def inputs(kind, profile=None):
    return {"kind": kind, "profile": profile, "build_nonce": secrets.token_hex(16),
            "sources": inventory(), "command": "owner_qualification", "main_stack": 16384,
            "worker_stack": 16384 if kind == "nuttx-target" else 131072,
            "priority_inheritance": False if kind == "nuttx-target" else None,
            "gitlinks": subprocess.check_output(["git", "ls-files", "--stage", "external/nuttx", "external/nuttx-apps"], cwd=ROOT, text=True)}


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def contains_runtime_identity(path, build_id):
    data = path.read_bytes()
    if path.suffix == ".uf2":
        # UF2 inserts a header every 256 payload bytes. The marker may straddle
        # blocks, so inspect contiguous flash payload rather than container bytes.
        blocks = []
        if len(data) % 512:
            raise ValueError("truncated UF2 image")
        for offset in range(0, len(data), 512):
            block = data[offset:offset + 512]
            if block[:8] != b"UF2\n\x57\x51\x5d\x9e" or block[508:] != b"\x30\x6f\xb1\x0a":
                raise ValueError("invalid UF2 image")
            size = int.from_bytes(block[16:20], "little")
            if not 0 < size <= 476:
                raise ValueError("invalid UF2 payload length")
            blocks.append((int.from_bytes(block[12:16], "little"), block[32:32 + size]))
        spans = []
        last = None
        for address, payload in sorted(blocks):
            if last != address:
                spans.append(bytearray())
            spans[-1].extend(payload)
            last = address + len(payload)
        return any(build_id.encode() in span for span in spans)
    return build_id.encode() in data


def retain_build(out, specification, artifacts, executable=None, image=None):
    retained = out / "retained"
    retained.mkdir()
    for relative, expected in specification["sources"].items():
        source = ROOT / relative
        if digest(source) != expected:
            raise ValueError("source changed during build")
        destination = retained / "sources" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    records = {}
    for label, source in artifacts.items():
        destination = retained / "artifacts" / label
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        records[str(destination.relative_to(out))] = {"sha256": digest(destination), "bytes": destination.stat().st_size}
    report = {"schema": "owner-qualification-build-v1", "build_id": identity(specification),
              "inputs": specification, "artifacts": records,
              "executable": f"retained/artifacts/{executable}" if executable else None,
              "image": f"retained/artifacts/{image}" if image else None,
              "physical_execution_verified": False}
    save(out / "build.json", report)
    validate_build(out)
    return report


def validate_build(root):
    report = unique_json(contained(root, "build.json").read_text())
    if report.get("schema") != "owner-qualification-build-v1" or report.get("build_id") != identity(report["inputs"]):
        raise ValueError("invalid build identity")
    if report.get("physical_execution_verified") is not False:
        raise ValueError("build receipt cannot establish physical execution")
    if report["inputs"]["kind"] not in ("host-functional", "nuttx-target") or not report["inputs"]["sources"]:
        raise ValueError("unknown build domain or empty source identity")
    if not report.get("executable") or (report["inputs"]["kind"] == "nuttx-target" and not report.get("image")):
        raise ValueError("selected executable/image missing")
    for relative, expected in report["inputs"]["sources"].items():
        if digest(contained(root, "retained/sources/" + relative)) != expected:
            raise ValueError("changed retained source")
    for relative, expected in report["artifacts"].items():
        path = contained(root, relative)
        if digest(path) != expected["sha256"] or path.stat().st_size != expected["bytes"]:
            raise ValueError("changed build artifact")
    for field in ("executable", "image"):
        if report.get(field):
            path = contained(root, report[field])
            if report[field] not in report["artifacts"] or not contains_runtime_identity(path, report["build_id"]):
                raise ValueError("runtime identity is not in the selected image/executable")
    return report


def inspect(build_root):
    root = build_root.resolve()
    build = validate_build(root)
    def artifact(field):
        path = build[field]
        return {"path": str(contained(root, path)), "relative_path": path,
                **build["artifacts"][path]} if path else None
    return {"schema": "owner-qualification-inspect-v1", "build_id": build["build_id"],
            "build_sha256": digest(root / "build.json"), "domain": build["inputs"]["kind"],
            "profile": build["inputs"]["profile"], "source_count": len(build["inputs"]["sources"]),
            "source_manifest_sha256": identity(build["inputs"]["sources"]),
            "executable": artifact("executable"),
            "elf": artifact("executable") if build["inputs"]["kind"] == "nuttx-target" else None,
            "image": artifact("image"), "cases": list(CASES), "case_count": len(CASES),
            "requires_fresh_boot": build["inputs"]["kind"] == "nuttx-target",
            "physical_execution_verified": False, "timing_qualified": False, "production_qualified": False}


def parse(text, build, case, token, physical=False):
    if case not in CASES or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", token):
        raise ValueError("unknown case or invalid token")
    if FAULT.search(text):
        raise ValueError("native/fixture failure in retained console")
    text = text.replace("\x1b[K", "").replace("\x1b[2K", "").replace("\r", "")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
        raise ValueError("unexpected console control bytes")
    build_id = build["build_id"]
    begin = f"OWNER_QUALIFICATION_BEGIN {build_id} {case} {token}"
    end = f"OWNER_QUALIFICATION_END {build_id} {case} {token}"
    lines = text.splitlines()
    if lines.count(begin) != 1 or lines.count(end) != 1 or sum(line.startswith("OWNER_QUALIFICATION_BEGIN ") for line in lines) != 1 or sum(line.startswith("OWNER_QUALIFICATION_END ") for line in lines) != 1:
        raise ValueError("missing/duplicate/wrong invocation boundary")
    values = [line.removeprefix("OWNER_QUALIFICATION_RESULT ") for line in lines if line.startswith("OWNER_QUALIFICATION_RESULT ")]
    if len(values) != 1:
        raise ValueError("expected one result")
    report = unique_json(values[0])
    if lines.index(begin) >= lines.index("OWNER_QUALIFICATION_RESULT " + values[0]) or lines.index(end) <= lines.index("OWNER_QUALIFICATION_RESULT " + values[0]):
        raise ValueError("invalid result ordering")
    domain = build["inputs"]["kind"]
    if any(type(report.get(key)) is not type(value) or report.get(key) != value for key, value in {"schema": "owner-qualification-v1", "case": case, "token": token,
            "build_id": build_id, "domain": domain, "pass": True,
            "native_scheduling": domain == "nuttx-target", "physical_execution_verified": False,
            "timing_qualified": False, "production_qualified": False,
            "quiescence_basis": "all-fixture-workers-joined"}.items()):
        raise ValueError("incorrect domain, identity or unsupported qualification")
    observed = report["observations"]
    if any(type(value) is not int or value < 0 for key, value in observed.items()
           if key not in ("participants", "endpoints_closed")):
        raise ValueError("invalid observation count")
    if any(type(observed[field]) is not int for field in FIELDS) or tuple(observed[field] for field in FIELDS) != CASES[case]:
        raise ValueError("incomplete work, wait or joined cleanup")
    if type(observed["native_waits"]) is not int or observed["native_waits"] != (CASES[case][-1] if domain == "nuttx-target" else 0):
        raise ValueError("incorrect native wait evidence")
    if observed["endpoints_closed"] is not True:
        raise ValueError("incomplete endpoint retirement")
    participants = observed["participants"]
    if Counter((p["role"], p["requested_priority"]) for p in participants) != Counter(ROLES[case]):
        raise ValueError("missing or relabeled participant")
    for p in participants:
        if domain == "nuttx-target":
            if type(report["fifo_policy"]) is not int or any(p[phase]["policy"] != report["fifo_policy"] or p[phase]["priority"] != p["requested_priority"] or type(p[phase]["tid"]) is not int or p[phase]["tid"] <= 0 for phase in ("start", "end")):
                raise ValueError("participant did not retain its native schedule")
            if p["before"] != p["restored"] or p["start"]["tid"] != p["end"]["tid"]:
                raise ValueError("schedule was not restored")
        elif report["fifo_policy"] is not None or any(value is not None for phase in ("before", "start", "end", "restored") for value in p[phase].values()):
            raise ValueError("host run fabricated native scheduling")
    if case == "owner-independent-inputs" and (observed["important_observations"], observed["ordinary_observations"], observed["stop_requests"]) != (1, 2, 1):
        raise ValueError("independent inputs incomplete")
    if case == "owner-fairness-deadline" and (observed["fairness_links_before_ordinary"] > 7 or observed["deadline_observations"] != 1 or observed["stop_requests"] != 2):
        raise ValueError("fairness/deadline control failed")
    if case == "owner-failure-recovery" and (observed["failure_observations"], observed["stop_requests"]) != (3, 2):
        raise ValueError("failure/retry witnesses missing")
    if physical:
        if domain != "nuttx-target":
            raise ValueError("host execution cannot become target evidence")
        command = f"owner_qualification {case} {token}"
        before = text[:text.index(begin)]
        after = text[text.index(end) + len(end):]
        if "NuttX" not in before or before.count(command) != 1 or f"OQ_STATUS_{token}:0" not in after:
            raise ValueError("fresh boot, exact command or clean exit missing")
        if re.findall(rf"(?m)^OQ_STATUS_{re.escape(token)}:([^\n]+)$", after) != ["0"] or "nsh>" not in after.split(f"OQ_STATUS_{token}:0", 1)[1]:
            raise ValueError("missing terminal status/prompt")
        if len(re.findall(r"NuttShell \(NSH\)", text)) != 1:
            raise ValueError("missing or repeated fresh shell boot")
    return report


def build_target(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    specification = inputs("nuttx-target", args.profile)
    environment = os.environ.copy()
    environment["OWNER_QUALIFICATION_BUILD_ID"] = identity(specification)
    command = [args.cargo, "nxrs", "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--profile", args.profile, "--command", "owner_qualification", "--stack-size", "16384",
               "--target-c-source", str(HERE / "native.c"), "--kconfig-disable", "PRIORITY_INHERITANCE",
               "--out", str(out / "firmware")]
    with (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    firmware = out / "firmware"
    config = (firmware / "resolved.config").read_text()
    if "CONFIG_BUILD_FLAT=y\n" not in config or re.search(r"^CONFIG_(SMP|PRIORITY_INHERITANCE)=y$", config, re.M):
        raise ValueError("unsupported resolved execution mode")
    selected = []
    for line in (firmware / "binaries.sha256").read_text().splitlines()[:2]:
        expected, path = line.split("  ", 1)
        path = Path(path).resolve()
        path.relative_to(firmware)
        if digest(path) != expected:
            raise ValueError("normal frontend final image digest disagrees")
        selected.append(path)
    if len(selected) != 2 or selected[0].read_bytes()[:4] != b"\x7fELF":
        raise ValueError("normal frontend image inventory missing")
    image_name = "firmware" + selected[1].suffix
    artifacts = {"firmware.elf": selected[0], image_name: selected[1]}
    for path in firmware.iterdir():
        if path.is_file() and path.suffix in (".json", ".txt", ".config", ".sha256"):
            artifacts["frontend/" + path.name] = path
    retain_build(out, specification, artifacts, executable="firmware.elf", image=image_name)
    print(out / "build.json")


def host(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    specification = inputs("host-functional")
    environment = os.environ.copy()
    environment["OWNER_QUALIFICATION_BUILD_ID"] = identity(specification)
    command = [args.cargo, "build", "--locked", "--release", "--manifest-path", str(HERE / "Cargo.toml"), "--target-dir", str(out / "target")]
    with (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = out / "target/release/nxrs-owner-qualification"
    build = retain_build(out, specification, {"owner-qualification": binary}, executable="owner-qualification")
    for case in CASES:
        run = out / "runs" / case
        run.mkdir(parents=True)
        token = secrets.token_hex(12)
        try:
            process = subprocess.run([str(binary), case, token], cwd=ROOT, capture_output=True, timeout=20)
            console, status = process.stdout + process.stderr, process.returncode
        except subprocess.TimeoutExpired as error:
            console, status = (error.stdout or b"") + (error.stderr or b""), None
        (run / "console.log").write_bytes(console)
        receipt = {"schema": "owner-qualification-capture-v1", "case": case, "token": token,
                   "build_directory": "../..", "build_sha256": digest(out / "build.json"),
                   "console_sha256": digest(run / "console.log"), "runtime": "host", "exit_code": status,
                   "capture_complete": False, "physical_execution_verified": False}
        save(run / "capture.json", receipt)
        if status != 0:
            raise ValueError(f"host case failed: {case}; retained {run}")
        try:
            analysis = parse((run / "console.log").read_text(), build, case, token)
        except (ValueError, KeyError, TypeError) as error:
            receipt["error"] = str(error)
            save(run / "capture.json", receipt)
            raise
        save(run / "analysis.json", analysis)
        receipt["capture_complete"] = True
        save(run / "capture.json", receipt)
        replay(run, out)
    print(f"Passed {len(CASES)} fresh-process host cases; {out}")


def capture(args):
    import serial
    build_root = args.build.resolve()
    build = validate_build(build_root)
    if build["inputs"]["kind"] != "nuttx-target":
        raise ValueError("physical capture requires a NuttX build")
    raw_session = args.session.read_bytes()
    session = unique_json(raw_session.decode())
    expected = {"schema": "owner-qualification-session-v1", "case": args.case, "build_id": build["build_id"],
                "image_sha256": build["artifacts"][build["image"]]["sha256"], "fresh_boot": True, "flash_verified": True}
    if any(session.get(key) != value for key, value in expected.items()) or not session.get("measurement_session"):
        raise ValueError("session does not identify the selected fresh-boot image/case")
    token = session.get("token", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", token):
        raise ValueError("invalid run token")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "session.json").write_bytes(raw_session)
    # Retain exactly one immutable copy so standalone replay needs no checkout.
    (out / "build").mkdir()
    shutil.copyfile(build_root / "build.json", out / "build/build.json")
    shutil.copytree(build_root / "retained", out / "build/retained")
    receipt = {"schema": "owner-qualification-capture-v1", "case": args.case, "token": token,
               "build_directory": "build", "build_sha256": digest(out / "build/build.json"),
               "runtime": "physical-operator-asserted", "capture_complete": False,
               "physical_execution_verified": False, "session_sha256": digest(out / "session.json"),
               "pre_command_reconnects": 0}
    data = bytearray()
    try:
        print("Serial capture ready; reset into the verified image.", flush=True)
        deadline = time.monotonic() + 90
        sent = False
        status_sent = False
        finish_after = None
        port = None
        try:
            while time.monotonic() < deadline:
                if port is None:
                    try:
                        port = serial.Serial(args.port, 115200, timeout=0.1)
                    except OSError:
                        # Native USB may disappear and re-enumerate on reset.
                        # This retry is only allowed before any command write.
                        receipt["pre_command_reconnects"] += 1
                        time.sleep(0.1)
                        continue
                try:
                    chunk = port.read(4096)
                except OSError as error:
                    port.close()
                    port = None
                    if sent:
                        raise ValueError("serial disconnected after command submission") from error
                    receipt["pre_command_reconnects"] += 1
                    time.sleep(0.1)
                    continue
                data.extend(chunk)
                text = data.decode("utf-8", errors="strict")
                if FAULT.search(text):
                    raise ValueError("native/fixture failure; retained console")
                if len(data) > 1_048_576:
                    raise ValueError("console output limit")
                if not sent and "NuttShell (NSH)" in text and "nsh>" in text:
                    # A partial write may already start the case. Never reopen
                    # or retry a command after reaching this point.
                    sent = True
                    port.write(f"owner_qualification {args.case} {token}\n".encode())
                marker = f"OWNER_QUALIFICATION_END {build['build_id']} {args.case} {token}"
                if sent and not status_sent and marker in text and "nsh>" in text.split(marker, 1)[1]:
                    port.write(f"echo OQ_STATUS_{token}:$?\n".encode())
                    status_sent = True
                statuses = re.findall(rf"(?m)^OQ_STATUS_{re.escape(token)}:([^\r\n]+)[\r\n]", text)
                if status_sent and statuses and statuses != ["0"]:
                    raise ValueError("nonzero or ambiguous target exit status")
                if status_sent and statuses == ["0"] and "nsh>" in text.split(f"OQ_STATUS_{token}:0", 1)[1]:
                    if finish_after is None:
                        finish_after = time.monotonic() + 1
                    if time.monotonic() >= finish_after:
                        break
            else:
                raise ValueError("fresh-boot capture deadline")
        finally:
            if port is not None:
                port.close()
        report = parse(data.decode(), build, args.case, token, physical=True)
        receipt["capture_complete"] = True
        save(out / "analysis.json", report)
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        (out / "console.log").write_bytes(data)
        receipt["console_sha256"] = digest(out / "console.log")
        save(out / "capture.json", receipt)
    replay(out)
    print(out / "analysis.json")


def replay(run, artifact_root=None):
    run = run.resolve()
    capture_record = unique_json(contained(run, "capture.json").read_text())
    if capture_record.get("schema") != "owner-qualification-capture-v1" or capture_record.get("physical_execution_verified") is not False:
        raise ValueError("invalid capture identity or qualification")
    if capture_record.get("build_directory") == "../..":
        if artifact_root is None or run.parent.parent != artifact_root.resolve():
            raise ValueError("host corpus requires its explicit --artifact-root")
        build_root = artifact_root.resolve()
    elif capture_record.get("build_directory") == "build":
        build_root = run / "build"
    else:
        raise ValueError("unsupported build location")
    if digest(contained(build_root, "build.json")) != capture_record["build_sha256"]:
        raise ValueError("changed build receipt")
    build = validate_build(build_root)
    console = contained(run, "console.log")
    if digest(console) != capture_record["console_sha256"]:
        raise ValueError("changed console")
    if capture_record.get("capture_complete") is not True:
        raise ValueError("retained failed/incomplete capture")
    physical = capture_record["runtime"] == "physical-operator-asserted"
    if physical:
        if digest(contained(run, "session.json")) != capture_record["session_sha256"]:
            raise ValueError("changed physical session")
        session = unique_json((run / "session.json").read_text())
        expected = {"schema": "owner-qualification-session-v1", "case": capture_record["case"], "token": capture_record["token"],
                    "build_id": build["build_id"], "image_sha256": build["artifacts"][build["image"]]["sha256"],
                    "fresh_boot": True, "flash_verified": True}
        if any(session.get(key) != value for key, value in expected.items()) or not session.get("measurement_session"):
            raise ValueError("session/build/capture mismatch")
    elif capture_record["runtime"] != "host" or type(capture_record.get("exit_code")) is not int or capture_record.get("exit_code") != 0 or build["inputs"]["kind"] != "host-functional":
        raise ValueError("invalid host process exit")
    result = parse(console.read_text(), build, capture_record["case"], capture_record["token"], physical)
    if result != unique_json(contained(run, "analysis.json").read_text()):
        raise ValueError("saved analysis differs from fresh replay")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    b.add_argument("--profile", required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--cargo", default="cargo")
    h = sub.add_parser("host")
    h.add_argument("--out", type=Path, required=True)
    h.add_argument("--cargo", default="cargo")
    i = sub.add_parser("inspect", help="read-only validation of all retained build evidence")
    i.add_argument("--build", type=Path, required=True)
    c = sub.add_parser("capture")
    c.add_argument("--build", type=Path, required=True)
    c.add_argument("--case", choices=CASES, required=True)
    c.add_argument("--session", type=Path, required=True)
    c.add_argument("--port", required=True)
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("replay")
    r.add_argument("run", type=Path)
    r.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    if args.action == "build": build_target(args)
    elif args.action == "host": host(args)
    elif args.action == "capture": capture(args)
    elif args.action == "inspect": print(json.dumps(inspect(args.build), indent=2))
    else:
        report = replay(args.run, args.artifact_root)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"owner qualification refused: {error}", file=sys.stderr)
        raise SystemExit(1)
