#!/usr/bin/env python3
"""Repeat normal C/Rust app lifecycles on one boot, then restore the board.

No target changes or fault injection. Private outputs include serial logs;
public_report exports only identities and aggregate heap/delivery evidence.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from measure import ROOT, command, digest, load, open_serial, parse_free, read_prompt, validate_pair
from measure_device import flash_command
from publish import public_report as footprint_report
from results import parse

HERE = Path(__file__).resolve().parent


def summaries(report):
    """Require every requested invocation before summarizing same-boot heap."""
    if not 0 <= report["warmup_rounds"] < report["rounds"] or report["blocks"] < 1:
        raise ValueError("invalid restart matrix")
    expected = [(block, language, round_, services)
                for block in range(report["blocks"])
                for language in (("c", "rust") if block % 2 == 0 else ("rust", "c"))
                for round_ in range(report["rounds"]) for services in (3, 20)]
    actual = [(row["block"], row["language"], row["round"], row["result"]["services"])
              for row in report["runs"]]
    if actual != expected:
        raise ValueError("incomplete, duplicated or reordered restart matrix")
    for row in report["runs"]:
        result = row["result"]
        if (row["done"]["status"] != 0 or result["errors"] != 0 or
                result["events"] != report["events"] or result["received"] != report["events"] or
                result["source"] != "messages"):
            raise ValueError("failed restart invocation")
    result = []
    for block in range(report["blocks"]):
        for language in ("c", "rust"):
            rows = [row for row in report["runs"] if row["block"] == block and row["language"] == language]
            steady = [row for row in rows if row["round"] >= report["warmup_rounds"]]
            used = [row["nsh_after"]["used"] for row in steady]
            allocations = [row["nsh_after"]["nused"] for row in steady]
            result.append(dict(block=block, language=language, calls=len(rows),
                               delivered_events=sum(row["result"]["received"] for row in rows),
                               first_post_heap_bytes=rows[0]["nsh_after"]["used"],
                               steady_samples=len(steady), steady_heap_min_bytes=min(used),
                               steady_heap_max_bytes=max(used), steady_heap_net_change_bytes=used[-1] - used[0],
                               steady_allocations_min=min(allocations), steady_allocations_max=max(allocations),
                               observed_steady_heap_flat=(min(used) == max(used) and min(allocations) == max(allocations))))
    return result


def public_report(report):
    summary = summaries(report)
    if report.get("restoration_error") is not None:
        raise ValueError("restoration failed")
    # Reuse the existing path-free firmware packager and success checks.
    public = footprint_report([report])
    del public["runs"]
    public.update(same_boot_per_language_block=True, same_process=False, fault_injection=False,
                  rounds=report["rounds"], blocks=report["blocks"], warmup_rounds=report["warmup_rounds"],
                  events_per_call=report["events"], calls=sum(row["calls"] for row in summary),
                  delivered_events=sum(row["delivered_events"] for row in summary),
                  restoration_verified=True, summaries=summary, harness_sha256=report["harness_sha256"])
    return public


def measure(args, capture=None, *, diagnostic="faults"):
    """Protected flash/capture/restore lifecycle; optional diagnostic capture.

    A supplied capture runs once per language/block and requires its diagnostic images.
    Normal restart capture remains the default and rejects all diagnostics.
    """
    if diagnostic not in ("faults", "pressure") or (capture is None and diagnostic != "faults"):
        raise ValueError("invalid diagnostic capture mode")
    records = validate_pair(args.c, args.rust)
    if any(record.get("diagnostic_trace") or record.get("diagnostic_perfmon") or
           record.get("diagnostic_hot_iram") or
           bool(record.get("diagnostic_faults")) != (capture is not None and diagnostic == "faults") or
           bool(record.get("diagnostic_pressure")) != (capture is not None and diagnostic == "pressure") or
           record.get("diagnostic_layout_padding_bytes") is not None
           for record in records.values()):
        raise ValueError("images do not match the requested capture mode")
    backup = args.backup.resolve()
    if backup.stat().st_size != 16777216 or backup.stat().st_mode & 0o077 or digest(backup) != args.backup_sha256:
        raise ValueError("matching private complete 16 MiB backup required")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    harness_files = (Path(__file__).resolve(), HERE / "evidence.py", HERE / "results.py",
                     HERE / "measure.py", HERE / "publish.py",
                     HERE / "build.py", ROOT / "tests/service-footprint/serial_io.py",
                     ROOT / "tests/service-footprint/measure_device.py", ROOT / "tests/zephyr-comparison/run_matrix.py")
    if capture is not None:
        harness_files += ((HERE / "device_faults.py", HERE / "lifecycle.py") if diagnostic == "faults"
                         else (HERE / "pressure.py",))
    identities = {str(path.relative_to(ROOT)): digest(path) for path in harness_files}
    report = dict(schema=1, source="messages", period_us=2000 if capture is None else 100, events=args.events,
                  blocks=args.blocks, rounds=args.rounds, warmup_rounds=args.warmup_rounds,
                  builds=records, runs=[], harness_sha256=identities,
                  failure=None, restoration_error=None, restoration_verified=False)
    report["fault_injection"] = capture is not None
    report["diagnostic_capture"] = diagnostic if capture is not None else None
    restore = load("sq_restart_restore", ROOT / "tests/zephyr-comparison/run_matrix.py").restore
    failure, restoration_failure, touched = None, None, False
    try:
        check = subprocess.run([args.flasher, "--chip", "esp32s3", "--port", args.port,
                                "--baud", "460800", "verify_flash", "0x0", str(backup)],
                               capture_output=True, text=True, timeout=300)
        (out / "preflight.log").write_text(check.stdout + check.stderr)
        check.check_returncode()
        for block in range(args.blocks):
            for language in (("c", "rust") if block % 2 == 0 else ("rust", "c")):
                # Flash once per language/block, never between its app calls.
                validate_pair(args.c, args.rust)
                touched = True
                flash = subprocess.run(flash_command(args.flasher, args.port, getattr(args, language) / "image.bin"),
                                       capture_output=True, text=True, timeout=180)
                label = f"{language}-block-{block}"
                (out / (label + "-flash.log")).write_text(flash.stdout + flash.stderr)
                flash.check_returncode()
                fd = open_serial(args.port)
                try:
                    try:
                        boot = read_prompt(fd, 5)
                    except TimeoutError:
                        boot = command(fd, "", 35)
                    (out / (label + "-boot.txt")).write_bytes(boot)
                    if capture is not None:
                        report["runs"].append(capture(fd, args, language, block, out, label))
                        continue
                    for round_ in range(args.rounds):
                        for services in (3, 20):
                            before = command(fd, "free", 10)
                            raw = command(fd, f"sq_{language} {services} {args.events} 2000", 10 + args.events * .004)
                            after = command(fd, "free", 10)
                            name = f"{label}-round-{round_}-{services}.txt"
                            (out / name).write_bytes(before + raw + after)
                            values = parse(raw.decode(errors="replace"), services=services,
                                           events=args.events, source="messages")
                            values.update(block=block, language=language, round=round_,
                                          nsh_before=parse_free(before), nsh_after=parse_free(after))
                            values["full_capacity_ram_bytes"] = (
                                records[language]["accounting"]["resident_ram_bytes"] + values["memory"]["full"])
                            report["runs"].append(values)
                        print("SERVICE_RESTART_ROUND_PASS", language, block, round_, flush=True)
                finally:
                    os.close(fd)
        validate_pair(args.c, args.rust)
        if any(digest(ROOT / name) != expected for name, expected in identities.items()):
            raise ValueError("measurement harness changed during capture")
        if capture is None:
            report["summaries"] = summaries(report)
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
    print("SERVICE_DEVICE_RESTART_PASS firmware_restored=true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("c", "rust", "backup", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("port", "flasher", "backup-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--warmup-rounds", type=int, default=2)
    parser.add_argument("--events", type=int, default=100)
    args = parser.parse_args()
    if not (1 <= args.rounds <= 100 and 1 <= args.blocks <= 10 and
            0 <= args.warmup_rounds < args.rounds and 1 <= args.events <= 100000):
        parser.error("invalid bounded restart matrix")
    measure(args)


if __name__ == "__main__":
    main()
