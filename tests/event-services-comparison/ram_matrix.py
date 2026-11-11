#!/usr/bin/env python3
"""Collect stack and allocation controls from frozen images, then restore flash.

This is a fresh measurement cohort. It does not rewrite the October results or
infer safe stack sizes from arithmetic. Private serial captures stay local.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ram_contract as contract


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load("ram_matrix_base", "run_matrix.py")
controls = load("ram_control_base", "control_measure.py")
measure = controls.measure
from rtos_harness.images import assert_stack_section, esp_idf_section_accounting, parse_sections
from rtos_harness.zephyr import resolved_config

metadata = load("ram_metadata_base", "report.py")
nuttx_config = load("ram_nuttx_config", "nuttx_profile.py")
DEFAULT_CASES = ("nuttx-c-three", "nuttx-rust-three", "zephyr-c-three", "embassy-three")


def profiles_for(platform):
    return (*contract.TRAFFIC_PROFILES, *(contract.ALLOC_PROFILES if platform != "embassy" else ()))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_configuration(provenance):
    config = provenance.get("configuration", {})
    if type(config.get("ram_contract")) is not int or config["ram_contract"] != 1 or config.get("stack_watermark") is not True:
        raise ValueError("RAM controls require fresh --stack-watermark builds with ram_contract=1")
    if config.get("psram") is True or type(config.get("cores")) is not int or config["cores"] != 1:
        raise ValueError("RAM controls require one core and internal RAM")
    # Section evidence is retained as well; the NuttX baseline profile may not
    # include a psram flag, so require its resolved configuration explicitly.
    expected = "shared_stack_bytes" if provenance["platform"] == "embassy" else "worker_stack_bytes"
    value = config.get(expected)
    minimum = 1024 if provenance["platform"] == "embassy" else 512
    if type(value) is not int or not minimum <= value <= 65536 or value % 16:
        raise ValueError("missing or invalid frozen stack reservation")
    if (config.get("slots") != 480 or config.get("event_bytes") != 64 or
            config.get("queues") != (20 if provenance.get("layout") == "one" else 60)):
        raise ValueError("RAM controls require the matched 480 x 64-byte capacity")
    sections = provenance.get("elf_sections", [])
    if not sections or any("A" in row["flags"] and row["size"] and row["address"] < 0x3E000000
                           and row["address"] + row["size"] > 0x3D000000 for row in sections):
        raise ValueError("missing internal-memory section evidence or PSRAM resident data")
    if provenance.get("section_accounting") != esp_idf_section_accounting(sections):
        raise ValueError("section totals disagree with retained ELF section evidence")
    return config


def frozen_image(directory, case, readelf):
    """Bind the declared RAM ledger to the actual frozen ELF and configuration."""
    item = matrix.frozen_image(directory, case)
    provenance = item["provenance"]
    config = validate_configuration(provenance)
    actual = parse_sections(subprocess.check_output(
        [str(readelf), "-W", "-S", str(item["elf"])], text=True))
    if actual != provenance["elf_sections"]:
        raise ValueError("ELF sections disagree with frozen RAM provenance")
    platform = provenance["platform"]
    if platform == "embassy":
        assert_stack_section(actual, config["shared_stack_bytes"])
    if platform != "embassy":
        if "resolved.config" not in provenance["artifacts"]:
            raise ValueError("native RAM measurement requires hash-pinned resolved.config")
        path = matrix._safe_artifact_path(directory, "resolved.config")
        values = resolved_config(path.read_text())
        if values.get("CONFIG_SMP", "n") != "n":
            raise ValueError("RAM measurement requires a resolved single-core configuration")
        if platform.startswith("nuttx-"):
            required = {"CONFIG_BUILD_FLAT": "y", "CONFIG_STACK_COLORATION": "y",
                        "CONFIG_USEC_PER_TICK": str(config["publication_timer_resolution_ms"] * 1000)}
            if (values.get("CONFIG_ESP32S3_SPIRAM", "n") != "n" or
                    any(values.get(key) != value for key, value in required.items())):
                raise ValueError("NuttX RAM configuration lacks the declared timer/internal-RAM/stack controls")
            if provenance.get("kernel_config_identity") != nuttx_config.helpers.config_identity(path):
                raise ValueError("NuttX kernel configuration identity disagrees with resolved.config")
        else:
            if (values.get("CONFIG_ESP_SPIRAM", "n") != "n" or
                    values.get("CONFIG_INIT_STACKS") != "y" or
                    values.get("CONFIG_THREAD_STACK_INFO") != "y" or
                    values != provenance.get("resolved_configuration")):
                raise ValueError("Zephyr resolved configuration disagrees with RAM observation evidence")
    return item


def build_identity(case, provenance):
    return {"case": case, "image_file": provenance["image_file"], "elf_file": provenance["elf_file"],
            "configuration": metadata._sanitize_metadata(provenance["configuration"]),
            "artifact_sha256": provenance["artifacts"],
            "source_sha256": metadata._source_hashes(provenance["source_sha256"]),
            "kernel_config_identity": provenance.get("kernel_config_identity"),
            "section_accounting": provenance["section_accounting"]}


def preflight(artifacts_root, readelf, cases):
    """Inspect all frozen builds without accessing a device or backup."""
    if not cases or len(set(cases)) != len(cases) or any(case not in matrix.CASES for case in cases):
        raise ValueError("distinct known RAM cases are required")
    return {"schema": 1, "kind": "event-services-ram-preflight",
            "device_io_performed": False, "physical_hardware_qualified": False,
            "cases": [build_identity(case, frozen_image(
                matrix.case_directory(Path(artifacts_root), case), case, readelf)["provenance"])
                      for case in cases]}


def validate_output(raw, profile, platform, layout, configuration):
    text = measure.normalize_output(raw)
    contract.reject_faults(text)
    if profile in contract.ALLOC_PROFILES:
        return contract.allocation_output(text, profile, platform, configuration)
    instrumentation = configuration.get("instrumentation", "full")
    if profile == "saturation":
        stack_bytes = (configuration["shared_stack_bytes"] if platform == "embassy"
                       else configuration["worker_stack_bytes"] * 20)
        parsed = controls.validate_saturation_output(raw, platform, layout, instrumentation,
                                                     stack_bytes=stack_bytes)
    else:
        parsed = controls.validate_traffic_output(raw, profile, layout, platform, instrumentation)
        controls.validate_build_timer([{"profile": profile, **parsed}], configuration)
        scheduling = contract.rows(text, "ES_SCHED")
        expected_policy = configuration.get("embassy_scheduling") if platform == "embassy" else "native"
        if (len(scheduling) != 1 or scheduling[0].get("policy") != expected_policy or
                scheduling[0].get("work_mode") != configuration.get("work_mode")):
            raise ValueError("scheduler policy disagrees with frozen build configuration")
        if parsed["result"]["capacity_ok"] != 1:
            raise ValueError("RAM qualification lost offered traffic to full queues")
    parsed["stacks"] = contract.stack_rows(text, platform, 20, configuration, require_observed=True)
    parsed["stack_summary"] = contract.stack_summary(parsed["stacks"])
    return parsed


def capture(fd, profile, platform, layout, configuration):
    console = configuration.get("console", "nsh")
    prompt = measure.console_prompt(platform, console)
    chunks = bytearray()
    try:
        if platform.startswith("nuttx-") and console == "nsh":
            for command in ("if " + measure._nuttx_command(platform, profile),
                            "then echo ES_COMMAND_EXIT status=0",
                            "else echo ES_COMMAND_EXIT status=1", "fi"):
                measure._send_paced(fd, command)
                chunks.extend(measure.read_until_prompt(fd, prompt, 120))
        else:
            os.write(fd, profile.encode("ascii") + b"\r")
            terminal = (b"ES_ALLOC_RESULT " if profile in contract.ALLOC_PROFILES
                        else b"ES_SAT_RESULT " if profile == "saturation" else b"ES_RESULT ")
            chunks.extend(measure._read_serial_until(
                fd, 120, lambda data: b"ES_FAIL " in data or
                re.search(rb"ES_COMMAND_EXIT status=[1-9][0-9]*", data) or
                (terminal in data and b"ES_COMMAND_EXIT " in data and prompt in data),
                "RAM qualification result and prompt"))
        raw = bytes(chunks)
        parsed = validate_output(raw, profile, platform, layout, configuration)
    except Exception as exc:
        raw = bytes(chunks) + getattr(exc, "raw_output", b"")
        raise measure.MeasurementOutputError(str(exc), raw) from exc
    free_raw = b""
    try:
        free_raw, free = measure.capture_free_memory(fd, platform, console, raw)
        contract.reject_faults(measure.normalize_output(free_raw))
        if free is not None and free["maxused"] < free["used"]:
            raise ValueError("native heap high water is below current usage")
    except measure.MeasurementOutputError as exc:
        # The legacy helper can carry the invocation in raw_output as well.
        # Keep each byte stream once, including a malformed/timed-out free row.
        raise measure.MeasurementOutputError(str(exc), raw, exc.free_raw_output) from exc
    except Exception as exc:
        raise measure.MeasurementOutputError(str(exc), raw, free_raw) from exc
    return {"profile": profile, **parsed, "free_memory": free,
            "raw_output": raw.decode("latin-1"),
            "raw_output_sha256": digest(raw),
            "free_raw_output": free_raw.decode("latin-1"),
            "free_raw_output_sha256": digest(free_raw)}


def checked_bytes(row, field):
    value = row.get(field)
    try:
        raw = value.encode("latin-1") if isinstance(value, str) else None
    except UnicodeEncodeError:
        raw = None
    if raw is None or digest(raw) != row.get(field + "_sha256"):
        raise ValueError(f"{field} hash mismatch")
    return raw


def verify_record(path, artifacts_root, readelf, *, expected_cases=None, expected_runs=None):
    """Replay a completed private matrix without opening a serial port or flashing."""
    path = Path(path)
    record = json.loads(path.read_text())
    if (not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 2 or
            record.get("kind") != "event-services-ram-controls" or
            record.get("environment") != "physical-esp32s3" or
            "failure" not in record or "restore_error" not in record or
            record.get("failure") is not None or record.get("restore_verified") is not True or
            record.get("restore_error") is not None or record.get("first_use_measurement") is not False):
        raise ValueError("RAM matrix failed, incomplete, unrestored, or has unsupported provenance")
    metadata._sha256(record.get("backup_sha256"), "backup identity")
    cases, repeats = record.get("requested_cases"), record.get("runs_per_case")
    if (not isinstance(cases, list) or not cases or any(case not in matrix.CASES for case in cases) or
            len(set(cases)) != len(cases) or type(repeats) is not int or repeats < 1):
        raise ValueError("invalid declared RAM matrix dimensions")
    if expected_cases is not None and cases != list(expected_cases):
        raise ValueError("RAM cases disagree with the requested campaign")
    if expected_runs is not None and repeats != expected_runs:
        raise ValueError("RAM repetitions disagree with the requested campaign")
    entries = record.get("cases")
    if (not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries) or
            [entry.get("case") for entry in entries] != cases):
        raise ValueError("missing, duplicated, or reordered RAM cases")
    exported = []
    for entry, case in zip(entries, cases):
        frozen = frozen_image(matrix.case_directory(Path(artifacts_root), case), case, readelf)
        provenance = frozen["provenance"]
        if entry.get("provenance") != provenance:
            raise ValueError("captured RAM build identity disagrees with frozen artifacts")
        serial_name = case + "-serial.log"
        if entry.get("serial_file") != serial_name:
            raise ValueError("invalid RAM serial evidence filename")
        serial = matrix._safe_artifact_path(path.parent, serial_name).read_bytes()
        if digest(serial) != entry.get("serial_sha256"):
            raise ValueError("RAM serial file hash mismatch")
        platform, layout = matrix.CASES[case]
        config = provenance["configuration"]
        order = [profile for i in range(repeats) for profile in measure.rotated_profiles(profiles_for(platform), i)]
        runs = entry.get("runs")
        if (not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs) or
                [run.get("profile") for run in runs] != order):
            raise ValueError("missing, duplicated, or reordered RAM profile invocations")
        checked = []
        captured = checked_bytes(entry, "boot_output")
        contract.reject_faults(measure.normalize_output(captured))
        for run, profile in zip(runs, order):
            raw, free_raw = checked_bytes(run, "raw_output"), checked_bytes(run, "free_raw_output")
            parsed = validate_output(raw, profile, platform, layout, config)
            expected_free = None
            if platform.startswith("nuttx-"):
                if not free_raw:
                    raise ValueError("NuttX native heap transcript is missing")
                contract.reject_faults(measure.normalize_output(free_raw))
                try:
                    expected_free = measure.validate_free_memory(measure.footprint.parse_free(free_raw))
                except (ValueError, KeyError):
                    if config.get("console", "nsh") == "event":
                        raise ValueError("NuttX native heap transcript is invalid")
                if expected_free is not None and expected_free["maxused"] < expected_free["used"]:
                    raise ValueError("native heap high water is below current usage")
            elif free_raw:
                raise ValueError("unexpected native heap transcript")
            expected = {"profile": profile, **parsed, "free_memory": expected_free,
                        "raw_output": raw.decode("latin-1"), "raw_output_sha256": digest(raw),
                        "free_raw_output": free_raw.decode("latin-1"), "free_raw_output_sha256": digest(free_raw)}
            if run != expected:
                raise ValueError("stored RAM numbers disagree with reparsed raw evidence")
            checked.append({"profile": profile, **parsed, "free_memory": expected_free})
            captured += raw + free_raw
        if captured != serial:
            raise ValueError("RAM serial file disagrees with the ordered invocation bytes")
        summary = image_summary(provenance, checked)
        if entry.get("memory_summary") != summary:
            raise ValueError("stored RAM summary disagrees with recomputed accounting")
        exported.append({**build_identity(case, provenance),
                         "runs": checked, "memory_summary": summary})
    return {"schema": 1, "kind": "event-services-ram-replay", "restore_verified": True,
            "first_use_measurement": False, "runs_per_case": repeats, "cases": exported,
            "validation_scope": "frozen artifacts and captured bytes; physical execution requires operator provenance",
            "production_minimum": False}


def image_summary(provenance, runs):
    sections = provenance["section_accounting"]
    static = sections["resident_ram_bytes"]
    resident = set(sections["resident_ram_sections"])
    code = sum(row["size"] for row in provenance["elf_sections"]
               if row["name"] in resident and "X" in row["flags"])
    native = provenance["platform"].startswith("nuttx-")
    heaps = [row["free_memory"]["maxused"] for row in runs if row.get("free_memory")]
    whole = static + max(heaps) if native and len(heaps) == len(runs) else None if native else static
    return {"resident_static_bytes": static, "ram_code_and_vectors_bytes": code,
            "observed_heap_peak_bytes": max(heaps) if heaps else None,
            "whole_image_observed_peak_bytes": whole,
            "stack_reservations_added_again": False,
            "production_minimum": False,
            "peak_scope": "cumulative since boot across all profiles and reporting",
            "note": "NuttX heap includes its worker stacks. Static Zephyr/Embassy reservations already belong to ELF RAM; do not add occupancy again."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--readelf", required=True)
    read_only = parser.add_mutually_exclusive_group()
    read_only.add_argument("--replay", type=Path, help="revalidate a private completed matrix; performs no device I/O")
    read_only.add_argument("--preflight", action="store_true", help="validate frozen builds; performs no device I/O")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--flasher")
    parser.add_argument("--case", choices=matrix.CASES, action="append")
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    cases = args.case or list(DEFAULT_CASES)
    if args.out.exists() or args.runs < 1 or len(set(cases)) != len(cases):
        parser.error("fresh output, positive run count, and distinct cases required")
    if (args.preflight or args.replay) and (args.backup or args.port or args.flasher):
        parser.error("read-only validation does not take device or backup arguments")
    if args.preflight:
        report = preflight(args.artifacts, args.readelf, cases)
        with args.out.open("x") as output:
            output.write(json.dumps(report, indent=2) + "\n")
        print(f"EVENT_SERVICES_RAM_PREFLIGHT_PASS cases={len(report['cases'])}")
        return
    if args.replay:
        report = verify_record(args.replay, args.artifacts, args.readelf,
                               expected_cases=cases, expected_runs=args.runs)
        with args.out.open("x") as output:
            output.write(json.dumps(report, indent=2) + "\n")
        print(f"EVENT_SERVICES_RAM_REPLAY_PASS cases={len(report['cases'])}")
        return
    if not args.backup or not args.port or not args.flasher:
        parser.error("collection requires --backup, --port and --flasher")
    frozen = {case: frozen_image(matrix.case_directory(args.artifacts, case), case, args.readelf) for case in cases}
    backup_hash = matrix.backup_identity(args.backup)
    args.out.mkdir(parents=True, mode=0o700)
    record = {"schema": 2, "kind": "event-services-ram-controls", "environment": "physical-esp32s3",
              "requested_cases": cases,
              "backup_sha256": backup_hash, "runs_per_case": args.runs, "cases": [],
              "invocation_state": "one boot per image; rotated profiles share prior workload and allocator state",
              "first_use_measurement": False,
              "failure": None, "restore_verified": False, "restore_error": None}
    with matrix.restored_session(record, args.out / "ram-matrix.json", args.backup,
            restore=lambda: matrix.restore(args.flasher, args.port, args.backup, args.out),
            identity=matrix.backup_identity):
        for case in cases:
            item = frozen[case]
            provenance = item["provenance"]
            config = provenance["configuration"]
            platform, layout = matrix.CASES[case]
            # Validate all pinned bytes again immediately before flashing.
            current = frozen_image(matrix.case_directory(args.artifacts, case), case, args.readelf)
            if current["provenance"] != provenance:
                raise ValueError("frozen build changed after RAM preflight")
            if matrix.backup_identity(args.backup) != backup_hash:
                raise ValueError("firmware backup changed")
            result = subprocess.run(measure.flash_command(args.flasher, args.port, item["image"]),
                                    text=True, capture_output=True, timeout=180)
            (args.out / f"{case}-flash.log").write_text(result.stdout + result.stderr)
            result.check_returncode()
            entry = {"case": case, "provenance": provenance, "runs": []}
            record["cases"].append(entry)
            fd = measure.shared.open_serial(args.port)
            transcript = bytearray()
            try:
                prompt = measure.console_prompt(platform, config.get("console", "nsh"))
                os.write(fd, b"\r")
                transcript.extend(measure.read_until_prompt(fd, prompt, 30))
                contract.reject_faults(measure.normalize_output(transcript))
                entry["boot_output"] = bytes(transcript).decode("latin-1")
                entry["boot_output_sha256"] = digest(transcript)
                profiles = profiles_for(platform)
                for iteration in range(args.runs):
                    for profile in measure.rotated_profiles(profiles, iteration):
                        run = capture(fd, profile, platform, layout, config)
                        entry["runs"].append(run)
                        transcript.extend(run["raw_output"].encode("latin-1"))
                        transcript.extend(run["free_raw_output"].encode("latin-1"))
                entry["memory_summary"] = image_summary(provenance, entry["runs"])
            except Exception as exc:
                transcript.extend(getattr(exc, "raw_output", b""))
                transcript.extend(getattr(exc, "free_raw_output", b""))
                raise
            finally:
                os.close(fd)
                path = args.out / f"{case}-serial.log"
                path.write_bytes(transcript)
                path.chmod(0o600)
                entry["serial_file"] = path.name
                entry["serial_sha256"] = digest(transcript)
    print(f"EVENT_SERVICES_RAM_PASS restored=true cases={len(record['cases'])}")


if __name__ == "__main__":
    main()
