#!/usr/bin/env python3
"""Collect private, hash-checked traffic-control and saturation measurements."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "service-footprint"))
import measure_device as footprint


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


measure = load("event_services_legacy_measure", HERE / "measure.py")
CONTROL_TRAFFIC_PROFILES = (
    "normal",
    "burst",
    "overload",
    "work-short",
    "work-medium",
    "work-long",
    "hal",
    "io-wait",
)
CONTROL_PROFILES = (*CONTROL_TRAFFIC_PROFILES, "saturation")
CONTROL_FIELDS = (
    "timer_ms",
    "work_iterations",
    "work_jobs",
    "work_p99_us",
    "work_max_us",
    "hal_calls",
    "hal_errors",
    "diagnostic_bytes",
)
SAT_CYCLE_FIELDS = (
    "cycle",
    "empty_heap",
    "full_heap",
    "drained_heap",
    "filled",
    "drained",
    "overflow_rejected",
    "depth_full",
    "depth_zero",
    "errors",
)
SAT_RESULT_FIELDS = (
    "platform",
    "queues",
    "slots",
    "event_bytes",
    "cycles",
    "errors",
    "stacks",
)


def _rows(text, marker, expected):
    rows = measure.shared.marker_rows(text, marker, expected)
    if any(not row for row in rows):
        raise ValueError(f"{marker}: malformed marker row")
    return rows


def _number(row, key, label):
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}: {key} must be a nonnegative integer")
    return value


def validate_control_row(text, profile, rejected=0, service_received=None):
    text = measure.normalize_output(text)
    rows = _rows(text, "ES_CONTROL", 1)
    row = rows[0]
    if set(row) != set(CONTROL_FIELDS):
        raise ValueError("ES_CONTROL field set mismatch")
    for key in CONTROL_FIELDS:
        _number(row, key, "ES_CONTROL")
    if row["timer_ms"] not in (1, 10):
        raise ValueError("ES_CONTROL timer_ms must be 1 or 10")
    expected_iterations = {"work-short": 10_000, "work-medium": 100_000, "work-long": 400_000}.get(profile, 0)
    expected_jobs = 117 if expected_iterations else 0
    expected_hal_calls = 21 if profile == "hal" else 0
    expected = {
        "work_iterations": expected_iterations,
        "hal_errors": 0,
        "diagnostic_bytes": 276,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise ValueError("ES_CONTROL profile contract mismatch")
    # Full queues reject before handler execution. Preserve this capacity
    # failure instead of requiring rejected jobs to have run anyway.
    if row['work_jobs'] > expected_jobs or row['hal_calls'] > expected_hal_calls:
        raise ValueError('ES_CONTROL profile contract mismatch: too many jobs')
    if not rejected and (row['work_jobs'] != expected_jobs or row['hal_calls'] != expected_hal_calls):
        raise ValueError('ES_CONTROL profile contract mismatch: missing work without rejects')
    if expected_jobs - row['work_jobs'] > rejected or expected_hal_calls - row['hal_calls'] > rejected:
        raise ValueError('ES_CONTROL missing jobs exceed rejected sends')
    if service_received is not None:
        if expected_jobs and row['work_jobs'] < max(0, service_received - 78):
            raise ValueError('ES_CONTROL work jobs disagree with service-zero receives')
        if expected_hal_calls and row['hal_calls'] < max(0, service_received - 174):
            raise ValueError('ES_CONTROL HAL calls disagree with service-zero receives')
        if row['work_jobs'] > service_received or row['hal_calls'] > service_received:
            raise ValueError('ES_CONTROL jobs exceed service-zero receives')
    if row["work_p99_us"] > row["work_max_us"]:
        raise ValueError("ES_CONTROL work p99 exceeds maximum")
    if not row['work_jobs'] and (row["work_p99_us"] or row["work_max_us"]):
        raise ValueError("ES_CONTROL work latency must be zero without work jobs")
    if row['work_jobs'] and (row["work_p99_us"] == 0 or row["work_max_us"] == 0):
        raise ValueError("ES_CONTROL work latency must be positive when work ran")
    return row


def validate_traffic_output(output, profile, layout, platform):
    parsed = measure.validate_output(output, profile, layout, platform)
    text = measure.normalize_output(output)
    parsed["control"] = validate_control_row(text, profile, parsed['result']['rejected'], parsed['services'][0]['received'])
    return parsed


def validate_build_timer(checked_runs, configuration):
    if not isinstance(configuration, dict):
        raise ValueError("build configuration metadata is missing")
    expected = configuration.get("publication_timer_resolution_ms")
    if expected not in (1, 10):
        raise ValueError("build publication timer must be 1 or 10 ms")
    for row in checked_runs:
        if row["profile"] != "saturation" and row["control"]["timer_ms"] != expected:
            raise ValueError("ES_CONTROL timer_ms disagrees with build configuration")
    return expected


def validate_saturation_output(output, platform, layout):
    text = measure.normalize_output(output)
    if "ES_FAIL" in text or text.splitlines().count("ES_PASS") != 1:
        raise ValueError("saturation invocation did not pass")
    if _rows(text, "ES_COMMAND_EXIT", 1)[0] != {"status": 0}:
        raise ValueError("saturation command did not exit successfully")
    if measure.shared.marker_rows(text, "ES_RESULT", 0):
        raise ValueError("saturation must not emit traffic ES_RESULT rows")
    for marker in ("ES_SERVICE", "ES_MEMORY", "ES_CONTROL"):
        if measure.shared.marker_rows(text, marker, 0):
            raise ValueError(f"saturation must not emit {marker} rows")

    queues = 20 if layout == "one" else 60
    stack_bytes = 8192 if platform == "embassy" else 81920
    resources = _rows(text, "ES_RESOURCES", 3)
    static_resources = [
        {key: value for key, value in row.items() if key != "heap_allocated"}
        for row in resources
    ]
    if any(row != static_resources[0] for row in static_resources[1:]):
        raise ValueError("static saturation resources changed while stacks were held")
    for row in resources:
        for key, value in row.items():
            if key not in ("note", "heap_note"):
                _number(row, key, "ES_RESOURCES")
    if (
        resources[0].get("queue_buffers") != 30_720
        or resources[0].get("stack_storage") != stack_bytes
    ):
        raise ValueError("saturation resource topology mismatch")

    cycles = _rows(text, "ES_SAT_CYCLE", 3)
    checked_cycles = []
    required = set(SAT_CYCLE_FIELDS)
    for index, row in enumerate(cycles):
        if set(row) != required:
            raise ValueError("ES_SAT_CYCLE field set mismatch")
        for key in SAT_CYCLE_FIELDS:
            _number(row, key, "ES_SAT_CYCLE")
        expected = {
            "cycle": index,
            "filled": 480,
            "drained": 480,
            "overflow_rejected": queues,
            "depth_full": queues,
            "depth_zero": queues,
            "errors": 0,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise ValueError("saturation cycle qualification failed")
        checked_cycles.append(dict(row))

    result = _rows(text, "ES_SAT_RESULT", 1)[0]
    if set(result) != set(SAT_RESULT_FIELDS):
        raise ValueError("ES_SAT_RESULT field set mismatch")
    if result.get("platform") != platform:
        raise ValueError("saturation platform marker mismatch")
    for key in SAT_RESULT_FIELDS[1:]:
        _number(result, key, "ES_SAT_RESULT")
    expected_result = {
        "queues": queues,
        "slots": 480,
        "event_bytes": 64,
        "cycles": 3,
        "errors": 0,
        "stacks": stack_bytes,
    }
    if any(result[key] != value for key, value in expected_result.items()):
        raise ValueError("saturation result qualification failed")
    return {
        "result": dict(result),
        "cycles": checked_cycles,
        "resources": [dict(row) for row in resources],
    }


def _capture_free(fd, platform):
    if not platform.startswith("nuttx-"):
        return b"", None
    measure._send_paced(fd, "free")
    try:
        raw = measure.read_until_prompt(fd, measure.PROMPTS[platform], 10)
    except measure.SerialReadTimeout as exc:
        raise measure.MeasurementOutputError(
            str(exc), exc.raw_output, exc.raw_output
        ) from exc
    try:
        free = measure.validate_free_memory(footprint.parse_free(raw))
    except (ValueError, KeyError):
        free = None
    return raw, free


def _run_saturation(fd, platform, layout):
    prompt = measure.PROMPTS[platform]
    if platform.startswith("nuttx-"):
        chunks = bytearray()
        commands = (
            "if " + measure._nuttx_command(platform, "saturation"),
            "then echo ES_COMMAND_EXIT status=0",
            "else echo ES_COMMAND_EXIT status=1",
            "fi",
        )
        for command in commands:
            measure._send_paced(fd, command)
            try:
                chunks.extend(measure.read_until_prompt(fd, prompt, 120))
            except measure.SerialReadTimeout as exc:
                raise measure.MeasurementOutputError(
                    str(exc), bytes(chunks) + exc.raw_output
                ) from exc
        output = bytes(chunks)
    else:
        os.write(fd, b"saturation\r")
        try:
            output = measure._read_serial_until(
                fd,
                120,
                lambda data: b"ES_SAT_RESULT " in data
                and b"ES_COMMAND_EXIT " in data
                and prompt in data,
                "saturation completion markers and prompt",
            )
        except measure.SerialReadTimeout as exc:
            raise measure.MeasurementOutputError(str(exc), exc.raw_output) from exc
    try:
        parsed = validate_saturation_output(output, platform, layout)
    except Exception as exc:
        raise measure.MeasurementOutputError(str(exc), output) from exc
    free_raw, free_memory = _capture_free(fd, platform)
    return {
        "profile": "saturation",
        **parsed,
        "raw_output": output.decode("latin-1"),
        "raw_output_sha256": hashlib.sha256(output).hexdigest(),
        "free_raw_output": free_raw.decode("latin-1"),
        "free_memory": free_memory,
    }


def run_one(fd, profile, layout, platform):
    if profile == "saturation":
        return _run_saturation(fd, platform, layout)
    row = measure.run_one(fd, profile, layout, platform)
    try:
        row["control"] = validate_control_row(row["raw_output"], profile, row['result']['rejected'], row['services'][0]['received'])
    except Exception as exc:
        raise measure.MeasurementOutputError(
            str(exc),
            row["raw_output"].encode("latin-1"),
            row["free_raw_output"].encode("latin-1"),
        ) from exc
    return row


def _validate_free(raw, platform):
    if not isinstance(raw, str):
        raise ValueError("free memory raw output is missing")
    if platform.startswith("nuttx-"):
        if not raw:
            raise ValueError("NuttX free output is missing")
        try:
            values = measure.validate_free_memory(
                footprint.parse_free(raw.encode("latin-1"))
            )
        except (UnicodeEncodeError, ValueError, KeyError):
            values = None
    else:
        if raw:
            raise ValueError("non-NuttX run contains a free output")
        values = None
    return values


def verify_record(record, *, image_sha256, platform, layout, profiles, runs):
    if (
        record.get("contract_version") != 1
        or record.get("failure") is not None
        or record.get("completed_runs") != runs * len(profiles)
    ):
        raise ValueError("incomplete control measurement")
    if (
        record.get("requested_runs") != runs
        or record.get("image_sha256") != image_sha256
    ):
        raise ValueError("control measurement identity mismatch")
    expected_order = [
        profile
        for run in range(runs)
        for profile in measure.rotated_profiles(profiles, run)
    ]
    entries = record.get("runs")
    if not isinstance(entries, list) or len(entries) != len(expected_order):
        raise ValueError("control measurement run list length mismatch")
    checked = []
    for entry, profile in zip(entries, expected_order):
        if not isinstance(entry, dict) or entry.get("profile") != profile:
            raise ValueError("control measurement profile rotation mismatch")
        raw = entry.get("raw_output")
        if not isinstance(raw, str):
            raise ValueError("raw serial output is missing")
        try:
            raw_bytes = raw.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError("raw serial output is not byte preserving") from exc
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest != entry.get("raw_output_sha256"):
            raise ValueError("raw serial output hash mismatch")
        if profile == "saturation":
            parsed = validate_saturation_output(raw, platform, layout)
        else:
            parsed = validate_traffic_output(raw, profile, layout, platform)
        for key, value in parsed.items():
            if entry.get(key) != value:
                raise ValueError(f"stored {key} disagrees with raw output")
        free = _validate_free(entry.get("free_raw_output"), platform)
        if entry.get("free_memory") != free:
            raise ValueError("stored free memory disagrees with raw free output")
        checked.append(
            {
                "profile": profile,
                **parsed,
                "free_memory": free,
                "raw_output_sha256": digest,
            }
        )
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=measure.PLATFORMS)
    parser.add_argument("--layout", required=True, choices=measure.LAYOUTS)
    parser.add_argument("--profile", choices=CONTROL_PROFILES, action="append")
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    profiles = args.profile or list(CONTROL_PROFILES)
    if (
        args.out.exists()
        or args.runs < 1
        or not args.image.is_file()
        or len(set(profiles)) != len(profiles)
    ):
        parser.error(
            "fresh output, positive runs, real image and distinct profiles are required"
        )
    image = args.image.resolve()
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    args.out.mkdir(parents=True)
    args.out.chmod(0o700)
    record = {
        "schema": 1,
        "contract_version": 1,
        "image": str(image),
        "image_sha256": image_hash,
        "platform": args.platform,
        "layout": args.layout,
        "profiles": profiles,
        "requested_runs": args.runs,
        "completed_runs": 0,
        "runs": [],
        "failure": None,
        "flashed": False,
    }
    transcript = bytearray()
    fd = None
    try:
        result = subprocess.run(
            measure.flash_command(args.flasher, args.port, image),
            capture_output=True,
            text=True,
            timeout=180,
        )
        (args.out / "flash.log").write_text(result.stdout + result.stderr)
        result.check_returncode()
        record["flashed"] = True
        fd = measure.shared.open_serial(args.port)
        boot = bytearray()
        try:
            boot.extend(
                measure.read_until_prompt(fd, measure.PROMPTS[args.platform], 5)
            )
        except measure.SerialReadTimeout as first:
            boot.extend(first.raw_output)
            os.write(fd, b"\r")
            try:
                boot.extend(
                    measure.read_until_prompt(fd, measure.PROMPTS[args.platform], 20)
                )
            except measure.SerialReadTimeout as second:
                boot.extend(second.raw_output)
                raise measure.MeasurementOutputError(
                    str(second), bytes(boot)
                ) from second
        transcript.extend(boot)
        for run_index in range(args.runs):
            for profile in measure.rotated_profiles(profiles, run_index):
                entry = run_one(fd, profile, args.layout, args.platform)
                record["runs"].append(entry)
                record["completed_runs"] += 1
                transcript.extend(entry["raw_output"].encode("latin-1"))
                transcript.extend(entry["free_raw_output"].encode("latin-1"))
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        raw = getattr(exc, "raw_output", None)
        if raw is not None:
            record["failure_raw_output"] = raw.decode("latin-1")
            transcript.extend(raw)
        free_raw = getattr(exc, "free_raw_output", b"")
        if free_raw:
            record["failure_free_raw_output"] = free_raw.decode("latin-1")
            transcript.extend(free_raw)
        transcript.extend(
            f"\nCONTROL_MEASUREMENT_FAILED: {record['failure']}\n".encode()
        )
        raise
    finally:
        if fd is not None:
            os.close(fd)
        (args.out / "serial.log").write_bytes(transcript)
        (args.out / "measurement.json").write_text(json.dumps(record, indent=2) + "\n")
        for name in ("serial.log", "measurement.json", "flash.log"):
            path = args.out / name
            if path.exists():
                path.chmod(0o600)
    print(f"EVENT_SERVICES_CONTROL_MEASUREMENT_PASS runs={record['completed_runs']}")


if __name__ == "__main__":
    main()
