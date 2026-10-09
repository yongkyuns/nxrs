#!/usr/bin/env python3
"""Prepare layout controls and collect a protected local compiler diagnosis."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import termios

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "service-footprint"))
import measure_device as serial


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load("compiler_probe_matrix", HERE / "run_matrix.py")
# Xtensa ENTRY must be word aligned; two-byte entry offsets get silently padded.
OFFSETS = tuple(range(0, 32, 4))
MODES = {"short": (512, 9), "normal": (400000, 3),
         "locked": (400000, 3), "concurrent": (400000, 3)}


def function_assembly(text, symbol):
    """Retain one generated function and its immediate literal declarations."""
    lines = text.splitlines()
    labels = [i for i, line in enumerate(lines)
              if re.match(re.escape(symbol) + r":(?:\s|$)", line)]
    if len(labels) != 1:
        raise ValueError(f"expected one function: {symbol}")
    label = labels[0]
    starts = [i for i in range(label) if ".literal_position" in lines[i]]
    ends = [i for i in range(label, len(lines))
            if re.match(r"\s*\.size\s+" + re.escape(symbol) + r"\s*,", lines[i])]
    if not starts or not ends:
        raise ValueError(f"missing literal/size boundaries: {symbol}")
    return lines[starts[-1]:ends[0] + 1]


def clone_function(lines, symbol, name, section, offset):
    """Change only symbols, section and entry padding, never instructions."""
    result = [f'\t.section {section},"ax",@progbits']
    before_entry = True
    for line in lines:
        if re.match(r"\s*\.(?:text|section)\b", line):
            continue
        if before_entry and re.match(r"\s*\.(?:align|p2align)\b", line):
            continue
        if re.match(re.escape(symbol) + r":(?:\s|$)", line):
            result.append("\t.balign 32")
            if offset:
                result.append(f"\t.space {offset},0")
            before_entry = False
        line = re.sub(r"\b" + re.escape(symbol) + r"\b", name, line)
        line = re.sub(r"\.L[A-Za-z0-9_.$]+", lambda m: ".L" + name + m[0][2:], line)
        result.append(line)
    return "\n".join(result) + "\n"


def prepare(args):
    inputs = {"c_gcc": (args.c_gcc, "probe_c"),
              "c_llvm": (args.c_llvm, "probe_c"),
              "rust_llvm": (args.rust_llvm, "probe_rust")}
    extracted = {key: function_assembly(path.read_text(), symbol)
                 for key, (path, symbol) in inputs.items()}
    args.out.mkdir(parents=True, exist_ok=False)
    assembly, declarations, entries = [], [], []
    for key, lines in extracted.items():
        symbol = inputs[key][1]
        for placement in ("flash", "iram"):
            for offset in OFFSETS:
                name = f"cp_{key}_{placement}_{offset:02d}"
                section = (".text." if placement == "flash" else ".iram1.") + name
                assembly.append(clone_function(lines, symbol, name, section, offset))
                declarations.append(f"uint32_t {name}(uint32_t, uint32_t, uint32_t);")
                entries.append(f'  {{"{name}", {name}}},')
    (args.out / "compiler-probe-layout.s").write_text("\n".join(assembly))
    (args.out / "compiler-probe-table.h").write_text(
        "\n".join(declarations) + "\nstatic const struct probe_case probe_cases[] = {\n"
        + "\n".join(entries) + f"\n}};\n#define PROBE_CASE_COUNT {len(entries)}u\n")
    proof = {"schema": 1, "cases": len(entries), "offsets": list(OFFSETS),
             "source_sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest()
                               for key, (path, _) in inputs.items()},
             "artifacts": {name: hashlib.sha256((args.out / name).read_bytes()).hexdigest()
                           for name in ("compiler-probe-layout.s", "compiler-probe-table.h")}}
    (args.out / "layout-provenance.json").write_text(json.dumps(proof, indent=2) + "\n")


def reference(value, token, count):
    for iteration in range(count):
        rotated = ((value << 5) | (value >> 27)) & 0xffffffff
        value = (rotated * 0x9e3779b9 + (token ^ ((iteration * 0x7f4a7c15) & 0xffffffff))) & 0xffffffff
    return value


def complete(data):
    return re.search(rb"(?m)^CP_DONE [^\r\n]+\r?\n[\s\S]*nsh> ", data) is not None


def validate(raw, mode):
    text = serial.ANSI_ESCAPE.sub(b"", raw).replace(b"\r", b"").decode("ascii")
    expected_cases = {f"cp_{compiler}_{placement}_{offset:02d}"
                      for compiler in ("c_gcc", "c_llvm", "rust_llvm")
                      for placement in ("flash", "iram") for offset in OFFSETS}
    count, repeats = MODES[mode]
    oracle = {repeat: reference(0x12345678 + repeat, 0x87654321 ^ repeat, count)
              for repeat in range(repeats)}
    rows, seen = [], set()
    for line in text.splitlines():
        if not line.startswith("CP_ROW "):
            continue
        fields = dict(part.split("=", 1) for part in line.split()[1:])
        if set(fields) != {"mode", "repeat", "case", "address", "iterations", "cycles", "result", "expected"}:
            raise ValueError("invalid probe row fields")
        for key in ("repeat", "address", "iterations", "cycles", "result", "expected"):
            fields[key] = int(fields[key])
        identity = (fields["case"], fields["repeat"])
        if (fields["mode"] != mode or fields["case"] not in expected_cases
                or fields["repeat"] not in oracle or identity in seen
                or fields["iterations"] != count or fields["cycles"] <= 0
                or fields["result"] != oracle[fields["repeat"]]
                or fields["expected"] != fields["result"]):
            raise ValueError("probe result/count/identity mismatch")
        placement = fields["case"].split("_")[-2]
        lower, upper = (0x42000000, 0x44000000) if placement == "flash" else (0x40370000, 0x403e0000)
        if not lower <= fields["address"] < upper or fields["address"] % 32 != int(fields["case"][-2:]):
            raise ValueError("probe placement/alignment mismatch")
        seen.add(identity)
        rows.append(fields)
    if len(seen) != len(expected_cases) * repeats:
        raise ValueError("incomplete probe output")
    done = re.findall(r"^CP_DONE (.*)$", text, re.MULTILINE)
    if len(done) != 1:
        raise ValueError("missing probe completion")
    summary = dict(part.split("=", 1) for part in done[0].split())
    if (set(summary) != {"mode", "cases", "repeats", "errors", "competitor_jobs"}
            or summary["mode"] != mode or int(summary["cases"]) != len(expected_cases)
            or int(summary["repeats"]) != repeats or int(summary["errors"]) != 0
            or (int(summary["competitor_jobs"]) > 0) != (mode == "concurrent")):
        raise ValueError("invalid probe completion")
    return {"mode": mode, "rows": rows, "competitor_jobs": int(summary["competitor_jobs"])}


def measure(args):
    backup_hash = matrix.backup_identity(args.backup)
    image_hash = hashlib.sha256(args.image.read_bytes()).hexdigest()
    args.out.mkdir(parents=True, exist_ok=False)
    args.out.chmod(0o700)
    record = {"schema": 1, "backup_sha256": backup_hash, "image_sha256": image_hash,
              "runs": [], "failure": None, "restore_verified": False, "restore_error": None}
    try:
        flashed = subprocess.run(serial.flash_command(args.flasher, args.port, args.image),
                                 capture_output=True, text=True, timeout=60)
        (args.out / "flash.log").write_text(flashed.stdout + flashed.stderr)
        flashed.check_returncode()
        fd = serial.open_serial(args.port)
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            os.write(fd, b"\r")
            serial.read_prompt(fd, 30)
            for mode in args.mode or list(MODES):
                os.write(fd, f"compiler_probe {mode}\r".encode())
                # Boot/NSH may leave an extra prompt buffered; completion must
                # include our marker, not just the first prompt received.
                raw = matrix.measure._read_serial_until(
                    fd, 60, complete,
                    "compiler probe completion")
                (args.out / f"{mode}.serial").write_bytes(raw)
                record["runs"].append({**validate(raw, mode),
                                       "raw_sha256": hashlib.sha256(raw).hexdigest()})
        finally:
            os.close(fd)
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            matrix.restore(args.flasher, args.port, args.backup, args.out)
            if matrix.backup_identity(args.backup) != backup_hash:
                raise ValueError("backup changed during restoration")
            record["restore_verified"] = True
        except Exception as exc:
            record["restore_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            (args.out / "measurement.json").write_text(json.dumps(record, separators=(",", ":")) + "\n")


def cohort(directory, build_path):
    """Export only validated numeric evidence, not private paths or serial logs."""
    record = json.loads((directory / "measurement.json").read_text())
    build = json.loads(build_path.read_text())
    if (record["failure"] or record["restore_error"] or not record["restore_verified"]
            or record["image_sha256"] != build["artifacts"]["image.bin"]
            or {run["mode"] for run in record["runs"]} != set(MODES)
            or len(record["runs"]) != len(MODES)):
        raise ValueError("incomplete or unverified measurement cohort")
    modes, addresses = {}, {}
    for run in record["runs"]:
        raw = (directory / (run["mode"] + ".serial")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != run["raw_sha256"]:
            raise ValueError("serial evidence hash mismatch")
        parsed = validate(raw, run["mode"])
        if parsed != {key: run[key] for key in parsed}:
            raise ValueError("measurement record differs from serial evidence")
        groups = {}
        for row in sorted(parsed["rows"], key=lambda r: (r["case"], r["repeat"])):
            groups.setdefault(row["case"], []).append(row["cycles"])
            if addresses.setdefault(row["case"], row["address"]) != row["address"]:
                raise ValueError("function address changed within cohort")
        modes[run["mode"]] = {"iterations": MODES[run["mode"]][0],
                              "cycles": groups, "raw_sha256": run["raw_sha256"],
                              "competitor_jobs": parsed["competitor_jobs"]}
    ledger = build["compiler_patch_ledger"]
    return {"image_sha256": record["image_sha256"], "restore_verified": True,
            "configuration_sha256": build["configuration_sha256"],
            "sources": {name: digest for name, digest in build["sources"].items()
                        if name.endswith((".c", ".rs"))},
            "generated_assembly": build["layout"]["source_sha256"],
            "patches": [{"name": p["name"], "sha256": p["sha256"]}
                        for p in ledger["patches"]],
            "addresses": addresses, "modes": modes}


def export(args):
    before, after = cohort(args.before, args.before_build), cohort(args.after, args.after_build)
    if (before["sources"] != after["sources"] or
            before["configuration_sha256"] != after["configuration_sha256"]):
        raise ValueError("compiler cohorts do not have matched firmware sources/configuration")
    pins = json.loads((HERE.parents[1] / "platform/rust-llvm/upstream.json").read_text())
    expected_patches = [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                        for path in sorted((HERE.parents[1] / "platform/rust-llvm/patches").glob("*.patch"))]
    if after["patches"] != expected_patches or before["patches"] != expected_patches[:-1]:
        raise ValueError("measured patchsets differ from the five-patch control and current series")
    expected = pins["qualification_tests"]["total"]
    log = args.llvm_test_log.read_bytes()
    if not re.search(rb"(?m)^\s*Passed: " + str(expected).encode() + rb"(?:\s|$)", log):
        raise ValueError("LLVM qualification log does not pass the pinned test count")
    data = {"schema": 1, "status": "evaluation-only", "cpu_mhz": 240,
            "llvm_revision": pins["revision"], "rust_revision": pins["rust_revision"],
            "llvm_tests": {"passed": expected, "log_sha256": hashlib.sha256(log).hexdigest()},
            "tool_provenance": json.loads(args.tool_provenance.read_text()),
            "before": before, "after": after}
    text = json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n"
    if any(private in text for private in ("/Users/", "/home/", "usbmodem", "device-before.bin")):
        raise ValueError("private metadata in public report")
    with args.out.open("x") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("prepare")
    for name in ("c-gcc", "c-llvm", "rust-llvm", "out"):
        source.add_argument("--" + name, type=Path, required=True)
    device = commands.add_parser("measure")
    for name in ("image", "backup", "out"):
        device.add_argument("--" + name, type=Path, required=True)
    for name in ("port", "flasher"):
        device.add_argument("--" + name, required=True)
    device.add_argument("--mode", choices=MODES, action="append")
    report = commands.add_parser("export")
    for name in ("before", "after", "before-build", "after-build", "llvm-test-log", "tool-provenance", "out"):
        report.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "measure" and args.mode and len(set(args.mode)) != len(args.mode):
        parser.error("measurement modes must be distinct")
    {"prepare": prepare, "measure": measure, "export": export}[args.command](args)


if __name__ == "__main__":
    main()
