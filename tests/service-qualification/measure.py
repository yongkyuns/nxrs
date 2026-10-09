#!/usr/bin/env python3
"""Measure frozen paired local images, then restore/verify a private backup."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from build import digest, load, ROOT
from evidence import kernel_header_identity, psram_enabled, psram_identity
from results import parse

sys.path.insert(0, str(ROOT / "tests/service-footprint"))
from serial_io import command, read_prompt
from measure_device import open_serial, flash_command, parse_free

HERE = Path(__file__).resolve().parent


def invocation_timeout(events, period_us):
    # Sub-tick usleep requests are not a sub-tick offered rate. Allow for the
    # frozen board's 1 ms timer resolution, without changing its pacing.
    return 10 + events * max(period_us, 1000) / 1e6 * 2


def validate_pair(c, rust):
    records = {}
    for language, folder in (("c", c), ("rust", rust)):
        record = json.loads((folder / "build-provenance.json").read_text())
        if record["language"] != language or record["command"] != "sq_" + language:
            raise ValueError("firmware language/entry differs")
        if set(record["artifacts"]) != {"app.elf", "image.bin", "resolved.config"}:
            raise ValueError("incomplete firmware artifacts")
        for name, expected in record["artifacts"].items():
            if digest(folder / name) != expected: raise ValueError("frozen firmware changed")
        for name, expected in record["source_sha256"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts or digest(ROOT / name) != expected:
                raise ValueError("firmware source changed")
        if "psram_enabled" in record and (
                type(record["psram_enabled"]) is not bool or
                record["psram_enabled"] != psram_enabled((folder / "resolved.config").read_text())):
            raise ValueError("PSRAM metadata differs from frozen configuration")
        records[language] = record
    kernel_header_identity(records)
    psram_identity(records)
    for field in ("config_identity", "kernel_archives", "c_flags", "c_compiler_sha256", "thread_stack"):
        if records["c"][field] != records["rust"][field]:
            raise ValueError("C/Rust inputs differ: " + field)
    if records["c"].get("diagnostic_perfmon", False) != records["rust"].get("diagnostic_perfmon", False):
        raise ValueError("C/Rust hardware-counter instrumentation differs")
    return records


def measure(args):
    recovery_events = getattr(args, "recovery_events", 0)
    if type(recovery_events) is not int or not 0 <= recovery_events <= 100000:
        raise ValueError("invalid recovery event count")
    if recovery_events and (args.source != "messages" or args.pm_mode):
        raise ValueError("recovery capture requires ordinary message traffic")
    records = validate_pair(args.c.resolve(), args.rust.resolve())
    if any(record.get(field) for record in records.values()
           for field in ("diagnostic_trace", "diagnostic_worker_switch", "diagnostic_entry_switch")):
        raise ValueError("latency trace and same-image control images are not supported")
    if any(record.get("diagnostic_faults") for record in records.values()):
        raise ValueError("fault fixture is not a timing/footprint image")
    perfmon = records["c"].get("diagnostic_perfmon", False)
    if args.pm_mode and not perfmon:
        raise ValueError("counter mode requires a hardware-counter image")
    backup = args.backup.resolve()
    if backup.stat().st_size != 16777216 or backup.stat().st_mode & 0o077:
        raise ValueError("complete private 16 MiB board backup required")
    if digest(backup) != args.backup_sha256:
        raise ValueError("backup hash differs")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    restore = load("sq_restore", ROOT / "tests/zephyr-comparison/run_matrix.py").restore
    harness_files = (Path(__file__).resolve(), HERE / "evidence.py", HERE / "build.py",
                     HERE / "results.py", HERE / "publish.py",
                     ROOT / "tests/service-footprint/serial_io.py",
                     ROOT / "tests/service-footprint/measure_device.py",
                     ROOT / "tests/zephyr-comparison/run_matrix.py")
    if perfmon:
        harness_files += (HERE / "perfmon_results.py",)
    identities = {str(path.relative_to(ROOT)): digest(path) for path in harness_files}
    report = dict(schema=1, builds=records, runs=[], failure=None, restoration_error=None,
                  restoration_verified=False,
                  paired_kernel_headers_verified=True, harness_sha256=identities,
                  source=args.source, period_us=args.period_us, events=args.events,
                  recovery_events=recovery_events, blocks=args.blocks, services=args.services,
                  ram_budget_bytes=250000, flash_budget_bytes=2000000,
                  perfmon_mode=(args.pm_mode or "fetch") if perfmon else None,
                  backup_sha256=args.backup_sha256)
    failure, restoration_failure = None, None
    touched = False
    try:
        # Refuse to overwrite an unrecognised board state. This also verifies
        # transport/recovery before the first qualification flash.
        check = subprocess.run([args.flasher, "--chip", "esp32s3", "--port", args.port,
                                "--baud", "460800", "verify_flash", "0x0", str(backup)],
                               capture_output=True, text=True, timeout=300)
        (out / "preflight.log").write_text(check.stdout + check.stderr)
        check.check_returncode()
        for block in range(args.blocks):
            for language in (("c", "rust") if block % 2 == 0 else ("rust", "c")):
                if args.language != "both" and language != args.language:
                    continue
                folder = args.c if language == "c" else args.rust
                for services in args.services:
                    if validate_pair(args.c.resolve(), args.rust.resolve()) != records:
                        raise ValueError("frozen build provenance changed during capture")
                    label = f"{language}-{services}-block-{block}"
                    # Boot fresh for each topology so allocator high-water
                    # history cannot carry over between three and twenty owners.
                    touched = True
                    flash = subprocess.run(flash_command(args.flasher, args.port, folder / "image.bin"),
                                           capture_output=True, text=True, timeout=180)
                    (out / (label + "-flash.log")).write_text(flash.stdout + flash.stderr)
                    flash.check_returncode()
                    fd = open_serial(args.port)
                    try:
                        try: boot = read_prompt(fd, 5)
                        except TimeoutError: boot = command(fd, "", 35)
                        before = command(fd, "free", 10)
                        selector = b""
                        if perfmon:
                            selector += command(fd, "set SQ_PM_MODE " + report["perfmon_mode"], 10)
                        invocation = f"sq_{language} {services} {args.events} {args.period_us}"
                        if args.source == "gpio": invocation += " gpio"
                        started = time.monotonic()
                        raw = command(fd, invocation, invocation_timeout(args.events, args.period_us))
                        duration = time.monotonic() - started
                        after = command(fd, "free", 10)
                        (out / (label + ".txt")).write_bytes(boot + before + selector + raw + after)
                        values = parse(raw.decode(errors="replace"), services=services,
                                       events=args.events, source=args.source)
                        if perfmon:
                            from perfmon_results import parse_perfmon
                            values["perfmon"] = parse_perfmon(raw.decode(errors="replace"),
                                                              mode=report["perfmon_mode"])
                        values.update(language=language, block=block, command_wall_seconds=duration,
                                      nsh_before=parse_free(before), nsh_after=parse_free(after))
                        # Include IRAM, initialized RAM and BSS, plus the live
                        # heap at ALL queue slots full. No stack subtraction.
                        values["full_capacity_ram_bytes"] = (
                            records[language]["accounting"]["resident_ram_bytes"] + values["memory"]["full"])
                        values["ram_headroom_bytes"] = 250000 - values["full_capacity_ram_bytes"]
                        if recovery_events:
                            # Same boot, fresh app invocation: verify normal-rate
                            # delivery and heap recovery without another flash.
                            started = time.monotonic()
                            recovery_raw = command(fd, f"sq_{language} {services} {recovery_events} 2000",
                                                   invocation_timeout(recovery_events, 2000))
                            recovery_duration = time.monotonic() - started
                            recovery_after = command(fd, "free", 10)
                            (out / (label + "-recovery.txt")).write_bytes(recovery_raw + recovery_after)
                            recovery = parse(recovery_raw.decode(errors="replace"), services=services,
                                             events=recovery_events, source="messages")
                            recovery.update(period_us=2000, command_wall_seconds=recovery_duration,
                                            nsh_before=values["nsh_after"], nsh_after=parse_free(recovery_after))
                            values["recovery"] = recovery
                        report["runs"].append(values)
                        print("SERVICE_DEVICE_PASS", label, "ram=", values["full_capacity_ram_bytes"],
                              "mean_us=", round(values["mean_us"], 3), flush=True)
                    finally:
                        os.close(fd)
        if validate_pair(args.c.resolve(), args.rust.resolve()) != records:
            raise ValueError("frozen build provenance changed during capture")
        if any(digest(ROOT / name) != expected for name, expected in identities.items()):
            raise ValueError("measurement harness changed during capture")
    except BaseException as error:
        failure = error
        report["failure"] = type(error).__name__ + ": " + str(error)
    finally:
        try:
            if touched:
                restore(args.flasher, args.port, backup, out)
                report["restoration_verified"] = True
        except BaseException as error:
            restoration_failure = error
            report["restoration_error"] = type(error).__name__ + ": " + str(error)
        finally:
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if restoration_failure is not None:
        raise restoration_failure from failure
    if failure is not None:
        raise failure
    print("SERVICE_MATRIX_PASS firmware_restored=true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("c", "rust", "backup", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("port", "flasher", "backup-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--blocks", type=int, default=3)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--period-us", type=int, default=2000)
    parser.add_argument("--recovery-events", type=int, default=0,
                        help="follow each run with this many 2 ms messages on the same boot")
    parser.add_argument("--services", type=int, action="append")
    parser.add_argument("--source", choices=("messages", "gpio"), default="messages")
    parser.add_argument("--language", choices=("both", "c", "rust"), default="both")
    parser.add_argument("--pm-mode", choices=("fetch", "all", "data", "instructions"),
                        help="whole-run hardware counter selector; actual selector is verified")
    args = parser.parse_args()
    if args.blocks < 1 or not 1 <= args.events <= 100000 or not 100 <= args.period_us <= 100000:
        parser.error("invalid run count/event count/period")
    if not 0 <= args.recovery_events <= 100000:
        parser.error("invalid recovery event count")
    args.services = args.services or [3, 20]
    if len(set(args.services)) != len(args.services) or any(not 3 <= n <= 20 for n in args.services):
        parser.error("distinct service counts from 3 to 20 required")
    measure(args)


if __name__ == "__main__":
    main()
