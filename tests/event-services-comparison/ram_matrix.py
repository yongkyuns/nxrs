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
from rtos_harness.images import esp_idf_section_accounting


def validate_configuration(provenance):
    config = provenance.get("configuration", {})
    if config.get("ram_contract") != 1 or config.get("stack_watermark") is not True:
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


def validate_output(raw, profile, platform, layout, configuration):
    text = measure.normalize_output(raw)
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
        free_raw, free = measure.capture_free_memory(fd, platform, console, raw)
    except Exception as exc:
        raw = bytes(chunks) + getattr(exc, "raw_output", b"")
        raise measure.MeasurementOutputError(str(exc), raw) from exc
    return {"profile": profile, **parsed, "free_memory": free,
            "raw_output": raw.decode("latin-1"),
            "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
            "free_raw_output": free_raw.decode("latin-1")}


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
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=matrix.CASES, action="append")
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    cases = args.case or ["nuttx-c-three", "nuttx-rust-three", "zephyr-c-three", "embassy-three"]
    if args.out.exists() or args.runs < 1 or len(set(cases)) != len(cases):
        parser.error("fresh output, positive run count, and distinct cases required")
    frozen = {case: matrix.frozen_image(matrix.case_directory(args.artifacts, case), case) for case in cases}
    for value in frozen.values():
        validate_configuration(value["provenance"])
        if value["provenance"]["platform"].startswith("nuttx-"):
            config_path = value["image"].parent / "resolved.config"
            if ("resolved.config" not in value["provenance"]["artifacts"] or
                    "CONFIG_ESP32S3_SPIRAM=y" in config_path.read_text()):
                parser.error("NuttX RAM measurement requires pinned no-PSRAM resolved.config")
    backup_hash = matrix.backup_identity(args.backup)
    args.out.mkdir(parents=True, mode=0o700)
    record = {"schema": 1, "kind": "event-services-ram-controls", "environment": "physical-esp32s3",
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
            matrix.frozen_image(matrix.case_directory(args.artifacts, case), case)
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
                profiles = (*contract.TRAFFIC_PROFILES, *(contract.ALLOC_PROFILES if platform != "embassy" else ()))
                for iteration in range(args.runs):
                    for profile in measure.rotated_profiles(profiles, iteration):
                        run = capture(fd, profile, platform, layout, config)
                        entry["runs"].append(run)
                        transcript.extend(run["raw_output"].encode("latin-1"))
                        transcript.extend(run["free_raw_output"].encode("latin-1"))
                entry["memory_summary"] = image_summary(provenance, entry["runs"])
            except Exception as exc:
                transcript.extend(getattr(exc, "raw_output", b""))
                raise
            finally:
                os.close(fd)
                path = args.out / f"{case}-serial.log"
                path.write_bytes(transcript)
                path.chmod(0o600)
    print(f"EVENT_SERVICES_RAM_PASS restored=true cases={len(record['cases'])}")


if __name__ == "__main__":
    main()
