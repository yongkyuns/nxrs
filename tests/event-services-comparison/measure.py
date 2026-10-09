#!/usr/bin/env python3
"""Flash one frozen event-service image and collect validated local runs."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PROFILES = ("normal", "burst", "overload")
PLATFORMS = ("nuttx-c", "nuttx-rust", "zephyr-c", "embassy")
LAYOUTS = ("three", "one")
EXPECTED_ATTEMPTS = {
    "normal": 3900,
    "burst": 6240,
    "overload": 192600,
    "work-short": 3900,
    "work-long": 3900,
    "work-medium": 3900,
    "hal": 3900,
    "io-wait": 3900,
}
MEASUREMENT_CONTRACT_VERSION = 2
PROMPTS = {
    "nuttx-c": b"nsh> ",
    "nuttx-rust": b"nsh> ",
    "zephyr-c": b"event> ",
    "embassy": b"event> ",
}
COMMANDS = {
    "nuttx-c": "es_c",
    "nuttx-rust": "es_rust",
    "zephyr-c": "normal",
    "embassy": "normal",
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load("event_services_serial", HERE.parent / "zephyr-comparison" / "measure.py")
footprint = load(
    "event_services_footprint", HERE.parent / "service-footprint" / "measure_device.py"
)
ANSI_ESCAPE = footprint.ANSI_ESCAPE


class MeasurementOutputError(ValueError):
    """A validation failure that retains the unmodified serial bytes."""

    def __init__(self, message, raw_output, free_raw_output=b""):
        super().__init__(message)
        self.raw_output = raw_output
        self.free_raw_output = free_raw_output


class SerialReadTimeout(TimeoutError):
    """A serial read timeout carrying every byte received during that read."""

    def __init__(self, message, raw_output):
        super().__init__(message)
        self.raw_output = raw_output


def rotated_profiles(profiles, run_index):
    offset = run_index % len(profiles)
    return list(profiles[offset:]) + list(profiles[:offset])


def _read_serial_until(fd, timeout, complete, description):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        readable, _, _ = select.select(
            [fd], [], [], min(0.1, max(0, deadline - time.monotonic()))
        )
        if readable:
            try:
                data.extend(os.read(fd, 4096))
            except BlockingIOError:
                continue
            if complete(data):
                return bytes(data)
    raise SerialReadTimeout(f"timed out waiting for {description}", bytes(data))


def read_until_prompt(fd, prompt, timeout=30):
    return _read_serial_until(fd, timeout, lambda data: prompt in data, repr(prompt))


def read_until_completion(fd, prompt, timeout=120):
    """Read a whole invocation; a prompt alone cannot truncate marker rows."""
    return _read_serial_until(
        fd,
        timeout,
        lambda data: b"ES_RESULT " in data
        and b"ES_COMMAND_EXIT " in data
        and prompt in data,
        "event-service completion markers and prompt",
    )


def _strict_rows(text, marker, count):
    rows = shared.marker_rows(text, marker, count)
    for row in rows:
        if not row:
            raise ValueError(f"{marker}: empty or malformed row")
    return rows


def _number(row, key, label):
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}: {key} must be a nonnegative integer")
    return value


def normalize_output(output):
    """Remove terminal CSI escapes and only a line-leading NSH prompt."""
    raw = (
        output
        if isinstance(output, bytes)
        else str(output).encode("latin-1", errors="replace")
    )
    clean = ANSI_ESCAPE.sub(b"", raw).replace(b"\r", b"")
    clean = re.sub(rb"(?m)^nsh> ", b"", clean)
    return clean.decode("latin-1")


def validate_free_memory(values):
    """Accept only a complete, internally consistent NuttX Umem snapshot."""
    keys = ("total", "used", "free", "maxused", "maxfree", "nused", "nfree")
    if not isinstance(values, dict) or set(values) != set(keys):
        raise ValueError("free memory snapshot has an invalid field set")
    for key in keys:
        _number(values, key, "free memory")
    if values["used"] > values["total"] or values["free"] > values["total"]:
        raise ValueError("free memory values exceed total")
    if values["used"] + values["free"] != values["total"]:
        raise ValueError("free memory used/free values do not sum to total")
    if values["maxused"] > values["total"] or values["maxfree"] > values["total"]:
        raise ValueError("free memory high-water values exceed total")
    return values


def validate_output(output, profile, layout, platform, *, require_queue_metrics=True):
    """Parse and reconcile one invocation's complete raw serial output."""
    if (
        profile not in EXPECTED_ATTEMPTS
        or layout not in LAYOUTS
        or platform not in PLATFORMS
    ):
        raise ValueError("invalid profile, layout or platform")
    text = normalize_output(output)
    if "ES_FAIL" in text:
        raise ValueError("firmware reported ES_FAIL")
    if text.splitlines().count("ES_PASS") != 1:
        raise ValueError("expected exactly one ES_PASS")
    exit_row = _strict_rows(text, "ES_COMMAND_EXIT", 1)[0]
    if exit_row != {"status": 0}:
        raise ValueError("command did not exit successfully")

    services = _strict_rows(text, "ES_SERVICE", 20)
    service_by_id = {}
    service_numeric = (
        "id",
        "received",
        "start_p99_us",
        "start_max_us",
        "finish_p99_us",
        "control_p99_us",
        "missed_control",
        "missed_data",
        "missed_status",
        "rejected",
    )
    for row in services:
        for key in service_numeric:
            _number(row, key, "ES_SERVICE")
        queue_keys = ("queue_p99_us", "queue_max_us")
        present = [key in row for key in queue_keys]
        if require_queue_metrics and not all(present):
            raise ValueError("ES_SERVICE queue-start latency metrics are required")
        if any(present) and not all(present):
            raise ValueError(
                "ES_SERVICE queue-start latency fields must appear together"
            )
        if all(present):
            for key in queue_keys:
                _number(row, key, "ES_SERVICE")
            if row["queue_p99_us"] > row["queue_max_us"]:
                raise ValueError("service queue p99 exceeds maximum")
        if row["id"] in service_by_id:
            raise ValueError("duplicate service id")
        if row["start_p99_us"] > row["start_max_us"]:
            raise ValueError("service start p99 exceeds maximum")
        service_by_id[row["id"]] = row
    if set(service_by_id) != set(range(20)):
        raise ValueError("service ids must be exactly 0 through 19")

    resources = _strict_rows(text, "ES_RESOURCES", 1)[0]
    memory = _strict_rows(text, "ES_MEMORY", 1)[0]
    result = _strict_rows(text, "ES_RESULT", 1)[0]
    for key, value in resources.items():
        if key not in ("note", "heap_note"):
            _number(resources, key, "ES_RESOURCES")
    for key in ("application_state", "diagnostics", "heap_before", "heap_live"):
        _number(memory, key, "ES_MEMORY")

    if result.get("platform") != platform or result.get("profile") != profile:
        raise ValueError("platform/profile marker mismatch")
    fixed = {
        "services": 20,
        "queues": 60 if layout == "three" else 20,
        "event_bytes": 64,
        "slots": 480,
    }
    for key, expected in fixed.items():
        if _number(result, key, "ES_RESULT") != expected:
            raise ValueError(f"ES_RESULT {key} mismatch")
    numeric = (
        "attempted",
        "accepted",
        "received",
        "rejected",
        "errors",
        "missed",
        "publication_p99_us",
        "publication_max_us",
        "start_p99_us",
        "start_max_us",
        "finish_p99_us",
        "finish_max_us",
        "control_p99_us",
        "control_max_us",
        "worst_service_p99_us",
        "queue_peak_observed",
        "last_finish_us",
        "delivery_ok",
        "capacity_ok",
        "deadlines_ok",
    )
    for key in numeric:
        _number(result, key, "ES_RESULT")
    for quantile, maximum in (
        ("publication_p99_us", "publication_max_us"),
        ("start_p99_us", "start_max_us"),
        ("finish_p99_us", "finish_max_us"),
        ("control_p99_us", "control_max_us"),
    ):
        if result[quantile] > result[maximum]:
            raise ValueError(f"{quantile} exceeds {maximum}")
    queue_keys = ("queue_p99_us", "queue_max_us")
    queue_present = [key in result for key in queue_keys]
    if require_queue_metrics and not all(queue_present):
        raise ValueError("ES_RESULT queue-start latency metrics are required")
    if any(queue_present) and not all(queue_present):
        raise ValueError("ES_RESULT queue-start latency fields must appear together")
    if all(queue_present):
        for key in queue_keys:
            _number(result, key, "ES_RESULT")
        if result["queue_p99_us"] > result["queue_max_us"]:
            raise ValueError("result queue p99 exceeds maximum")

    expected_attempts = EXPECTED_ATTEMPTS[profile]
    if result["attempted"] != expected_attempts:
        raise ValueError("profile attempted-event count mismatch")
    if result["accepted"] + result["rejected"] != result["attempted"]:
        raise ValueError("accepted plus rejected does not equal attempted")
    if result["received"] != result["accepted"]:
        raise ValueError("received does not equal accepted")
    if result["errors"] != 0 or result["delivery_ok"] != 1:
        raise ValueError("protocol delivery failed")
    if result["capacity_ok"] != int(result["rejected"] == 0):
        raise ValueError("capacity flag disagrees with rejected count")
    if result["deadlines_ok"] != int(result["missed"] == 0):
        raise ValueError("deadline flag disagrees with missed count")
    capacity = 8 if layout == "three" else 24
    if result["queue_peak_observed"] > capacity:
        raise ValueError("observed queue peak exceeds layout capacity")
    if sum(row["received"] for row in services) != result["received"]:
        raise ValueError("service received counts do not sum to result")
    if sum(row["rejected"] for row in services) != result["rejected"]:
        raise ValueError("service rejected counts do not sum to result")
    missed_sum = sum(
        row["missed_control"] + row["missed_data"] + row["missed_status"]
        for row in services
    )
    if missed_sum != result["missed"]:
        raise ValueError("service deadline misses do not sum to result")
    if result["worst_service_p99_us"] != max(row["start_p99_us"] for row in services):
        raise ValueError("worst service p99 does not match service rows")
    if resources["queue_buffers"] != 30_720:
        raise ValueError("queue buffer byte count mismatch")

    return {
        "result": result,
        "services": [service_by_id[i] for i in range(20)],
        "resources": resources,
        "memory": memory,
    }


def _send_paced(fd, command):
    data = command.encode("ascii") + b"\r"
    for index, byte in enumerate(data):
        os.write(fd, bytes((byte,)))
        if index + 1 < len(data):
            time.sleep(0.005)


def _nuttx_command(platform, profile):
    return f"{COMMANDS[platform]} {profile}"


def console_prompt(platform, nuttx_console="nsh"):
    return b"event> " if platform.startswith("nuttx-") and nuttx_console == "event" else PROMPTS[platform]


def capture_free_memory(fd, platform, nuttx_console="nsh", output=b""):
    """Post-run allocator observation; minimal images must provide valid data."""
    if not platform.startswith("nuttx-"):
        return b"", None
    if nuttx_console == "event":
        os.write(fd, b"memory\r")
    else:
        _send_paced(fd, "free")
    try:
        raw = read_until_prompt(fd, console_prompt(platform, nuttx_console), 10)
    except SerialReadTimeout as exc:
        raise MeasurementOutputError(str(exc), output + exc.raw_output, exc.raw_output) from exc
    try:
        memory = validate_free_memory(footprint.parse_free(raw))
    except (ValueError, KeyError) as exc:
        if nuttx_console == "event":
            raise MeasurementOutputError(str(exc), output, raw) from exc
        # Preserve older NSH observations whose free output was not parseable.
        memory = None
    return raw, memory


def run_one(fd, profile, layout, platform, nuttx_console="nsh"):
    prompt = console_prompt(platform, nuttx_console)
    if platform.startswith("nuttx-") and nuttx_console == "nsh":
        # NSH keeps the conditional open across interactive commands. This is
        # the same status-capture sequence used by the existing device helper.
        chunks = bytearray()
        for command in (
            f"if {_nuttx_command(platform, profile)}",
            "then echo ES_COMMAND_EXIT status=0",
            "else echo ES_COMMAND_EXIT status=1",
            "fi",
        ):
            _send_paced(fd, command)
            try:
                chunks.extend(read_until_prompt(fd, prompt, 120))
            except SerialReadTimeout as exc:
                raise MeasurementOutputError(
                    str(exc), bytes(chunks) + exc.raw_output
                ) from exc
        output = bytes(chunks)
    else:
        os.write(fd, profile.encode("ascii") + b"\r")
        try:
            output = read_until_completion(fd, prompt, 120)
        except SerialReadTimeout as exc:
            raise MeasurementOutputError(str(exc), exc.raw_output) from exc
    free_raw, memory_free = capture_free_memory(fd, platform, nuttx_console, output)
    try:
        validated = validate_output(output, profile, layout, platform)
    except Exception as exc:
        raise MeasurementOutputError(str(exc), output, free_raw) from exc
    raw_text = output.decode("latin-1")
    return {
        "profile": profile,
        **validated,
        "raw_output": raw_text,
        "raw_output_sha256": hashlib.sha256(output).hexdigest(),
        "free_raw_output": free_raw.decode("latin-1"),
        "free_memory": memory_free,
    }


def verify_measurement_record(
    record, *, image_sha256, platform, layout, profiles, runs, nuttx_console="nsh"
):
    if nuttx_console not in ("nsh", "event") or record.get("nuttx_console", "nsh") != nuttx_console:
        raise ValueError("measurement console identity mismatch")
    if record.get("contract_version") != MEASUREMENT_CONTRACT_VERSION:
        raise ValueError("unsupported or missing measurement contract version")
    if record.get("failure") is not None or record.get("completed_runs") != runs * len(
        profiles
    ):
        raise ValueError("incomplete device measurement")
    if (
        record.get("requested_runs") != runs
        or record.get("image_sha256") != image_sha256
    ):
        raise ValueError("measurement identity mismatch")
    expected_order = [
        profile for i in range(runs) for profile in rotated_profiles(profiles, i)
    ]
    entries = record.get("runs")
    if not isinstance(entries, list) or len(entries) != len(expected_order):
        raise ValueError("measurement run list length mismatch")
    checked = []
    for entry, expected_profile in zip(entries, expected_order):
        if not isinstance(entry, dict):
            raise ValueError("measurement run entry must be an object")
        raw = entry.get("raw_output")
        try:
            raw_bytes = raw.encode("latin-1") if isinstance(raw, str) else None
        except UnicodeEncodeError:
            raw_bytes = None
        if raw_bytes is None or hashlib.sha256(raw_bytes).hexdigest() != entry.get(
            "raw_output_sha256"
        ):
            raise ValueError("raw serial output hash mismatch")
        parsed = validate_output(raw, expected_profile, layout, platform)
        for field, value in parsed.items():
            if entry.get(field) != value:
                raise ValueError(f"stored {field} disagrees with raw serial output")
        if entry.get("profile") != expected_profile:
            raise ValueError("profile rotation mismatch")
        free_raw = entry.get("free_raw_output")
        if not isinstance(free_raw, str) or "free_memory" not in entry:
            raise ValueError("free memory observation fields are missing")
        if platform.startswith("nuttx-") and not free_raw:
            raise ValueError("NuttX free output is missing")
        if platform.startswith("nuttx-"):
            try:
                expected_free = validate_free_memory(
                    footprint.parse_free(free_raw.encode("latin-1"))
                )
            except (UnicodeEncodeError, ValueError, KeyError) as exc:
                if nuttx_console == "event":
                    raise ValueError("minimal NuttX memory snapshot is invalid") from exc
                expected_free = None
        else:
            expected_free = None
        if not platform.startswith("nuttx-") and free_raw not in (None, ""):
            raise ValueError("unexpected free output for this platform")
        if entry.get("free_memory") != expected_free:
            raise ValueError("stored free_memory disagrees with raw free output")
        checked.append(
            {"profile": expected_profile, **parsed, "free_memory": expected_free}
        )
    return checked


def flash_command(flasher, port, image):
    return [
        flasher,
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--baud",
        "460800",
        "write_flash",
        "--flash_mode",
        "keep",
        "--flash_freq",
        "keep",
        "--flash_size",
        "keep",
        "0x0",
        str(image),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--layout", required=True, choices=LAYOUTS)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--nuttx-console", choices=("nsh", "event"), default="nsh")
    args = parser.parse_args()
    profiles = args.profile or list(PROFILES)
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
        "contract_version": MEASUREMENT_CONTRACT_VERSION,
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
        "nuttx_console": args.nuttx_console,
    }
    transcript = bytearray()
    fd = None
    prompt = console_prompt(args.platform, args.nuttx_console)
    try:
        flash = subprocess.run(
            flash_command(args.flasher, args.port, image),
            capture_output=True,
            text=True,
            timeout=180,
        )
        (args.out / "flash.log").write_text(flash.stdout + flash.stderr)
        flash.check_returncode()
        record["flashed"] = True
        fd = shared.open_serial(args.port)
        boot_output = bytearray()
        try:
            boot_output.extend(read_until_prompt(fd, prompt, 5))
        except SerialReadTimeout as first_timeout:
            boot_output.extend(first_timeout.raw_output)
            os.write(fd, b"\r")
            try:
                boot_output.extend(read_until_prompt(fd, prompt, 20))
            except SerialReadTimeout as retry_timeout:
                boot_output.extend(retry_timeout.raw_output)
                raise MeasurementOutputError(
                    str(retry_timeout), bytes(boot_output)
                ) from retry_timeout
        transcript.extend(boot_output)
        for run_index in range(args.runs):
            for profile in rotated_profiles(profiles, run_index):
                entry = run_one(fd, profile, args.layout, args.platform, args.nuttx_console)
                transcript.extend(entry["raw_output"].encode("latin-1"))
                if entry["free_raw_output"]:
                    transcript.extend(
                        entry["free_raw_output"].encode("latin-1", errors="replace")
                    )
                record["runs"].append(entry)
                record["completed_runs"] += 1
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        failed_output = getattr(exc, "raw_output", None)
        if failed_output is not None:
            record["failure_raw_output"] = failed_output.decode("latin-1")
            transcript.extend(failed_output)
            failed_free = getattr(exc, "free_raw_output", b"")
            if failed_free:
                record["failure_free_raw_output"] = failed_free.decode("latin-1")
                transcript.extend(failed_free)
        transcript.extend(f"\nMEASUREMENT_FAILED: {record['failure']}\n".encode())
        raise
    finally:
        if fd is not None:
            os.close(fd)
        serial_path = args.out / "serial.log"
        serial_path.write_bytes(transcript)
        report_path = args.out / "measurement.json"
        report_path.write_text(json.dumps(record, indent=2) + "\n")
        serial_path.chmod(0o600)
        report_path.chmod(0o600)
        flash_log = args.out / "flash.log"
        if flash_log.exists():
            flash_log.chmod(0o600)
    print(f"EVENT_SERVICES_MEASUREMENT_PASS runs={record['completed_runs']}")


if __name__ == "__main__":
    main()
