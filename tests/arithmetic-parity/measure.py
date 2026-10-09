#!/usr/bin/env python3
"""Capture and validate arithmetic parity diagnostics from a local device."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import termios
import time

HERE = Path(__file__).resolve().parent
SERVICE_DIR = HERE.parent / "service-footprint"
RESTORE_MODULE_PATH = HERE.parent / "event-services-comparison" / "run_matrix.py"
REPEATS = 5
MAX_SAMPLES = 64
COMMAND_TIMEOUT = 600
DONE_PROMPT_RE = re.compile(
    rb"(?m)^AQ_DONE cases=\d+ samples=\d+ c_errors=\d+ rust_errors=\d+ "
    rb"repeats=5\r?\n(?:\r?\n)?nsh>[ \t]*"
    rb"(?:\x1b\[[0-?]*[ -/]*[@-~])*\Z")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))
DEVICE = _load_module("arithmetic_measure_device", SERVICE_DIR / "measure_device.py")
RESTORE = _load_module("arithmetic_restore", RESTORE_MODULE_PATH)

CASE_RE = re.compile(
    r"^AQ_CASE id=(\d+) count=(\d+) c_present=(\d+) c_errors=(\d+) "
    r"rust_errors=(\d+) pair_diff=(\d+) c_max_ulp=(\d+) rust_max_ulp=(\d+)$")
TIME_RE = re.compile(
    r"^AQ_TIME id=(\d+) mode=(masked|normal) repeat=(\d+) "
    r"c_cycles=(\d+) rust_cycles=(\d+)$")
FAIL_RE = re.compile(
    r"^AQ_FAIL backend=(c|rust) id=(\d+) sample=(\d+) "
    r"actual_lo=0x([0-9a-fA-F]{16}) actual_hi=0x([0-9a-fA-F]{16}) "
    r"actual_flag=(\d+) expected_lo=0x([0-9a-fA-F]{16}) "
    r"expected_hi=0x([0-9a-fA-F]{16}) expected_flag=(\d+)$")
DONE_RE = re.compile(
    r"^AQ_DONE cases=(\d+) samples=(\d+) c_errors=(\d+) "
    r"rust_errors=(\d+) repeats=(\d+)$")


def _uint(value, maximum, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def _manifest(coverage):
    if (not isinstance(coverage, dict) or not isinstance(coverage.get("cases"), list) or
            coverage.get("samples_per_case") != MAX_SAMPLES):
        raise ValueError("invalid coverage manifest")
    rows = {}
    for expected_id, case in enumerate(coverage["cases"]):
        if not isinstance(case, dict) or case.get("id") != expected_id:
            raise ValueError("coverage case IDs must be contiguous and ordered")
        if (not isinstance(case.get("name"), str) or
                not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", case["name"])):
            raise ValueError("coverage case has no name")
        if not isinstance(case.get("c_available"), bool):
            raise ValueError(f"invalid C availability for case {expected_id}")
        vectors = case.get("expected")
        if not isinstance(vectors, list) or len(vectors) != MAX_SAMPLES:
            raise ValueError(f"case {expected_id} must define 64 expected vectors")
        for vector in vectors:
            if (not isinstance(vector, list) or len(vector) != 4 or
                    any(isinstance(v, bool) or not isinstance(v, int) for v in vector)):
                raise ValueError(f"invalid expected vector for case {expected_id}")
            _uint(vector[0], (1 << 64) - 1, "expected lo")
            _uint(vector[1], (1 << 64) - 1, "expected hi")
            _uint(vector[2], (1 << 32) - 1, "expected flag")
            _uint(vector[3], (1 << 32) - 1, "expected reserved")
        rows[expected_id] = case
    if not rows:
        raise ValueError("coverage manifest contains no cases")
    return rows


def parse(raw: bytes, coverage: dict) -> dict:
    """Parse one complete serial capture; reject malformed/incomplete records."""
    if not isinstance(raw, bytes):
        raise TypeError("raw capture must be bytes")
    cases = _manifest(coverage)
    # NSH appends an ANSI clear-line sequence after its interactive prompt.
    # Preserve original bytes separately; normalize only for protocol parsing.
    lines = DEVICE.ANSI_ESCAPE.sub(b"", raw).replace(b"\r", b"").decode("ascii", errors="strict").splitlines()
    prompt_rows = [i for i, line in enumerate(lines) if line.strip() == "nsh>"]
    parsed_cases = {}
    timings = {}
    failures = {}
    done = None
    for line in lines:
        if line.startswith("AQ_CASE"):
            match = CASE_RE.fullmatch(line)
            if not match:
                raise ValueError("malformed AQ_CASE record")
            values = list(map(int, match.groups()))
            ident, count, c_present, c_errors, rust_errors, pair_diff, c_ulp, r_ulp = values
            if ident not in cases or ident in parsed_cases:
                raise ValueError("unknown or duplicate AQ_CASE id")
            if count != MAX_SAMPLES or c_present not in (0, 1):
                raise ValueError(f"invalid count/presence for case {ident}")
            if bool(c_present) != cases[ident]["c_available"]:
                raise ValueError(f"C availability disagrees for case {ident}")
            if max(c_errors, rust_errors, pair_diff) > count:
                raise ValueError(f"error count exceeds samples for case {ident}")
            if max(c_ulp, r_ulp) > 0xffffffffffffffff:
                raise ValueError(f"invalid maximum ULP for case {ident}")
            if not c_present and (c_errors or c_ulp or pair_diff):
                raise ValueError(f"absent C backend has results for case {ident}")
            parsed_cases[ident] = {
                "id": ident, "name": cases[ident]["name"], "count": count,
                "c_present": bool(c_present), "c_errors": c_errors,
                "rust_errors": rust_errors, "pair_diff": pair_diff,
                "c_max_ulp": c_ulp, "rust_max_ulp": r_ulp,
                "correctness": c_errors == 0 and rust_errors == 0,
            }
        elif line.startswith("AQ_TIME"):
            match = TIME_RE.fullmatch(line)
            if not match:
                raise ValueError("malformed AQ_TIME record")
            ident, mode, repeat, c_cycles, rust_cycles = match.groups()
            ident, repeat, c_cycles, rust_cycles = map(int, (ident, repeat, c_cycles, rust_cycles))
            if ident not in cases or repeat >= REPEATS or max(c_cycles, rust_cycles) > 0xffffffff:
                raise ValueError("invalid AQ_TIME identity or cycle count")
            key = (ident, mode, repeat)
            if key in timings:
                raise ValueError("duplicate AQ_TIME record")
            timings[key] = {"repeat": repeat, "c_cycles": c_cycles,
                            "rust_cycles": rust_cycles}
        elif line.startswith("AQ_FAIL"):
            match = FAIL_RE.fullmatch(line)
            if not match:
                raise ValueError("malformed AQ_FAIL record")
            backend, ident, sample, alo, ahi, aflag, elo, ehi, eflag = match.groups()
            ident, sample, aflag, eflag = map(int, (ident, sample, aflag, eflag))
            _uint(aflag, (1 << 32) - 1, "actual flag")
            _uint(eflag, (1 << 32) - 1, "expected flag")
            actual = (int(alo, 16), int(ahi, 16), aflag)
            expected = (int(elo, 16), int(ehi, 16), eflag)
            if ident not in cases or sample >= MAX_SAMPLES:
                raise ValueError("invalid AQ_FAIL identity")
            if expected != tuple(cases[ident]["expected"][sample][:3]):
                raise ValueError("AQ_FAIL expected value disagrees with coverage vectors")
            key = (ident, backend)
            failures.setdefault(key, [])
            if len(failures[key]) >= 4 or any(row["sample"] == sample for row in failures[key]):
                raise ValueError("duplicate or excess AQ_FAIL record")
            failures[key].append({"sample": sample, "actual_lo": actual[0],
                                  "actual_hi": actual[1], "actual_flag": actual[2],
                                  "expected_lo": expected[0], "expected_hi": expected[1],
                                  "expected_flag": expected[2]})
        elif line.startswith("AQ_DONE"):
            match = DONE_RE.fullmatch(line)
            if not match or done is not None:
                raise ValueError("malformed or duplicate AQ_DONE record")
            done = tuple(map(int, match.groups()))
        elif line.startswith("AQ_"):
            raise ValueError("unknown arithmetic diagnostic record")

    if done is None:
        raise ValueError("AQ_DONE completion record missing")
    done_rows = [i for i, line in enumerate(lines) if line.startswith("AQ_DONE")]
    if not prompt_rows or not done_rows or prompt_rows[-1] <= done_rows[-1]:
        raise ValueError("final NSH prompt must follow AQ_DONE")
    if set(parsed_cases) != set(cases):
        raise ValueError("AQ_CASE rows are missing")
    total_samples = sum(row["count"] for row in parsed_cases.values())
    c_errors = sum(row["c_errors"] for row in parsed_cases.values())
    rust_errors = sum(row["rust_errors"] for row in parsed_cases.values())
    if done != (len(cases), total_samples, c_errors, rust_errors, REPEATS):
        raise ValueError("AQ_DONE totals do not match case rows")
    for ident, row in parsed_cases.items():
        if row["c_present"] is False and any(
                timings.get((ident, mode, repeat), {}).get("c_cycles") != 0
                for mode in ("masked", "normal") for repeat in range(REPEATS)):
            raise ValueError(f"absent C backend has nonzero cycles for case {ident}")
        for mode in ("masked", "normal"):
            values = [timings.get((ident, mode, repeat)) for repeat in range(REPEATS)]
            if any(value is None for value in values):
                raise ValueError(f"timing rows missing for case {ident}/{mode}")
            if any(value["rust_cycles"] == 0 for value in values):
                raise ValueError(f"Rust timing is zero for case {ident}/{mode}")
            if row["c_present"] and any(value["c_cycles"] == 0 for value in values):
                raise ValueError(f"C timing is zero for case {ident}/{mode}")
            row.setdefault("timings", {})[mode] = values
        for backend, field in (("c", "c_errors"), ("rust", "rust_errors")):
            got = len(failures.get((ident, backend), []))
            if got != min(row[field], 4):
                raise ValueError(f"AQ_FAIL count disagrees for case {ident}/{backend}")
            samples = [failure["sample"] for failure in failures.get((ident, backend), [])]
            if any(sample >= row["count"] for sample in samples) or samples != sorted(samples):
                raise ValueError(f"AQ_FAIL samples are out of range/order for case {ident}/{backend}")
        row["failures"] = {
            backend: failures.get((ident, backend), []) for backend in ("c", "rust")
        }
    return {"schema": 1, "passed": c_errors == 0 and rust_errors == 0,
            "correctness": {"passed": c_errors == 0 and rust_errors == 0,
                            "c_errors": c_errors, "rust_errors": rust_errors,
                            "pair_diff": sum(row["pair_diff"] for row in parsed_cases.values())},
            "cases": [parsed_cases[i] for i in range(len(cases))]}


def export_summary(report: dict) -> dict:
    """Return the public report fields, excluding paths and raw device output."""
    case_fields = ("id", "name", "count", "c_present", "c_errors", "rust_errors",
                   "pair_diff", "c_max_ulp", "rust_max_ulp", "correctness", "failures",
                   "timings")

    def clean_run(run):
        cleaned = {key: run[key] for key in
                   ("schema", "passed", "raw_capture", "raw_bytes", "raw_sha256",
                    "aggregate_offset") if key in run}
        if "correctness" in run:
            cleaned["correctness"] = {key: run["correctness"][key] for key in
                                      ("passed", "c_errors", "rust_errors", "pair_diff")}
        cleaned["cases"] = [{key: row[key] for key in case_fields if key in row}
                            for row in run.get("cases", [])]
        return cleaned

    result = {key: report[key] for key in
              ("schema", "passed", "image_sha256", "coverage_sha256",
               "aggregate_raw_sha256") if key in report}
    if "correctness" in report:
        result["correctness"] = {key: report["correctness"][key] for key in
                                 ("passed", "c_errors", "rust_errors", "pair_diff")}
    result["restoration"] = {key: report.get("restoration", {}).get(key)
                             for key in ("verified", "failure")}
    failure = report.get("failure")
    result["failure"] = (failure if isinstance(failure, str) and
                         re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", failure) else None)
    result["runs"] = [clean_run(run) for run in report.get("runs", [])]
    if "cases" in report:
        result["cases"] = [{key: row[key] for key in case_fields if key in row}
                            for row in report["cases"]]
    return result


def _private_write(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def claim_serial_exclusive(fd):
    """Prevent a second process from opening the measurement serial device."""
    fcntl.ioctl(fd, termios.TIOCEXCL)


def _read_serial_until(fd, command=None, timeout=COMMAND_TIMEOUT):
    """Send one NSH command and read through AQ_DONE and its following prompt."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    if command is not None and os.isatty(fd):
        termios.tcflush(fd, termios.TCIFLUSH)
    if command is not None:
        pending = memoryview(command.encode("ascii") + b"\r")
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out writing NSH command")
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                raise TimeoutError("timed out writing NSH command")
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("serial command write made no progress")
            pending = pending[written:]
    while True:
        # A read can end inside NSH's clear-line escape. Wait for its complete
        # suffix rather than returning a prompt the strict parser cannot read.
        if DONE_PROMPT_RE.search(data):
            return bytes(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for AQ_DONE followed by nsh>")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise TimeoutError("timed out waiting for AQ_DONE followed by nsh>")
        chunk = os.read(fd, 4096)
        if not chunk:
            raise EOFError("serial device closed before AQ_DONE and nsh>")
        data.extend(chunk)


def run_device(image, backup, out, port, flasher, coverage, runs=2,
               command="arithmetic_suite", *, device=None, restore_fn=None,
               runner=None, backup_check=None, coverage_sha256=None,
               serial_reader=None, exclusive_fn=None):
    """Run diagnostics and restore/verify the complete backup in all outcomes."""
    device = device or DEVICE
    restore_fn = restore_fn or RESTORE.restore
    runner = runner or subprocess.run
    backup_check = backup_check or RESTORE.backup_identity
    serial_reader = serial_reader or _read_serial_until
    exclusive_fn = exclusive_fn or claim_serial_exclusive
    image, backup, out = Path(image), Path(backup), Path(out)
    if runs < 1 or out.exists() or not image.is_file():
        raise ValueError("positive runs, existing image, and fresh output are required")
    manifest = _manifest(coverage)
    backup_hash = backup_check(backup)
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    out.mkdir(parents=True, mode=0o700)
    out.chmod(0o700)
    serial_fd = None
    transcript = bytearray()
    flash_output = b""
    run_reports = []
    run_raw = []
    failure = None
    failure_detail = None
    restore_error = None
    restore_detail = None
    try:
        if backup_check(backup) != backup_hash:
            raise ValueError("backup changed before flash")
        flashed = runner(device.flash_command(str(flasher), str(port), image),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        flash_output = flashed.stdout or b""
        if flashed.returncode:
            raise RuntimeError("image flash failed")
        serial_fd = device.open_serial(str(port))
        exclusive_fn(serial_fd)
        transcript.extend(device.command(serial_fd, "", 35))
        for run_index in range(runs):
            raw = serial_reader(serial_fd, command, COMMAND_TIMEOUT)
            aggregate_offset = len(transcript)
            transcript.extend(raw)
            run_raw.append(raw)
            parsed = parse(raw, coverage)
            parsed["raw_capture"] = run_index + 1
            parsed["raw_bytes"] = len(raw)
            parsed["raw_sha256"] = hashlib.sha256(raw).hexdigest()
            parsed["aggregate_offset"] = aggregate_offset
            run_reports.append(parsed)
    except Exception as exc:
        failure = type(exc).__name__
        failure_detail = f"{type(exc).__name__}: {exc}"
    finally:
        if serial_fd is not None:
            try:
                os.close(serial_fd)
            except OSError:
                pass
        try:
            restore_fn(str(flasher), str(port), backup, out)
            if backup_check(backup) != backup_hash:
                raise ValueError("backup changed during restoration")
        except Exception as exc:
            restore_error = type(exc).__name__
            restore_detail = f"{type(exc).__name__}: {exc}"
            failure = failure or restore_error
            failure_detail = failure_detail or restore_detail
        for index, raw in enumerate(run_raw, start=1):
            _private_write(out / f"run-{index:03d}.raw", raw)
        aggregate_raw_hash = hashlib.sha256(bytes(transcript)).hexdigest()
        _private_write(out / "serial.raw", bytes(transcript))
        _private_write(out / "flash.raw", flash_output)
        aggregate = {
            "schema": 1,
            "passed": failure is None and all(row["passed"] for row in run_reports),
            "image_sha256": image_hash,
            "aggregate_raw_sha256": aggregate_raw_hash,
            "coverage_sha256": coverage_sha256 or hashlib.sha256(
                json.dumps(coverage, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "restoration": {"verified": restore_error is None, "failure": restore_error},
            "failure": failure,
            "failure_detail": failure_detail,
            "runs": run_reports,
        }
        public = export_summary(aggregate)
        _private_write(out / "summary.json", (json.dumps(public, indent=2) + "\n").encode())
        proof = {key: aggregate[key] for key in
                 ("image_sha256", "coverage_sha256", "aggregate_raw_sha256",
                  "restoration", "failure", "failure_detail")}
        proof["restoration"]["diagnostic"] = restore_detail
        if proof["failure"] is None and run_reports and not aggregate["passed"]:
            proof["failure"] = "CorrectnessFailure"
            proof["failure_detail"] = "One or more runs reported correctness errors"
        proof["backup_sha256"] = backup_hash
        _private_write(out / "proof.json", (json.dumps(proof, indent=2) + "\n").encode())
    if restore_error:
        raise RuntimeError("firmware restoration or verification failed")
    if failure:
        raise RuntimeError(f"device measurement failed ({failure}); backup restored")
    return public


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--command", default="arithmetic_suite")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be positive")
    try:
        coverage_bytes = args.coverage.read_bytes()
        coverage = json.loads(coverage_bytes)
        report = run_device(args.image, args.backup, args.out, args.port,
                            args.flasher, coverage, args.runs, args.command,
                            coverage_sha256=hashlib.sha256(coverage_bytes).hexdigest())
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        parser.exit(2, f"measurement failed; see private proof in output if created ({type(exc).__name__})\n")
    correctness = [run["correctness"] for run in report["runs"]]
    c_errors = sum(row["c_errors"] for row in correctness)
    rust_errors = sum(row["rust_errors"] for row in correctness)
    pair_diff = sum(row["pair_diff"] for row in correctness)
    case_count = len(report["runs"][0]["cases"]) if report["runs"] else 0
    print(f"ARITHMETIC_MEASURE passed={str(report['passed']).lower()} "
          f"runs={len(report['runs'])} cases={case_count} "
          f"c_errors={c_errors} rust_errors={rust_errors} pair_diff={pair_diff} "
          f"restored={str(report['restoration']['verified']).lower()}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
