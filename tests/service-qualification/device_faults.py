#!/usr/bin/env python3
"""Capture diagnostic shutdown faults, never a size or latency benchmark."""
import argparse
import hashlib
import json
from pathlib import Path

from evidence import kernel_header_identity, require_restored
from lifecycle import validate
from results import _ANSI_ESCAPE, parse
from restart_device import command, measure

HERE = Path(__file__).resolve().parent
REPETITIONS = 3
SCENARIOS = ("fail-stop", "fail-join", "fail-close")


def parse_faults(text):
    lines = iter(line.strip() for line in _ANSI_ESCAPE.sub("", text).splitlines()
                 if line.strip().startswith("SQ_"))

    def take(kind, fields=(), strings=()):
        line = next(lines, "")
        parts = line.split()
        if len(parts) != len(fields) + 1 or not parts or parts[0] != kind:
            raise ValueError("missing/malformed " + kind)
        result = {}
        for field, token in zip(fields, parts[1:]):
            key, separator, value = token.partition("=")
            if key != field or not separator or not value:
                raise ValueError("malformed " + kind + " field")
            if field not in strings:
                if not value.isascii() or not value.isdecimal():
                    raise ValueError("invalid " + kind + " number")
                value = int(value)
            result[field] = value
        return result

    def runtime_call():
        body = []
        for line in lines:
            body.append(line)
            if line.partition(" ")[0] == "SQ_DONE":
                return body
            if line.partition(" ")[0] not in ("SQ_MEMORY", "SQ_RESULT", "SQ_CLEANUP"):
                raise ValueError("unexpected runtime record")
        raise ValueError("incomplete runtime call")

    take("SQ_DEVICE_WARMUP")
    warmup = parse("\n".join(runtime_call()), services=20, events=100, source="messages")
    baseline = take("SQ_DEVICE_BASELINE", ("heap",))["heap"]
    profiles = []
    for scenario in SCENARIOS:
        if take("SQ_DEVICE_BEGIN", ("scenario",), ("scenario",))["scenario"] != scenario:
            raise ValueError("unexpected profile order")
        body, maximum_ms = [], 0
        positions = (0, 1, 30, 59) if scenario == "fail-close" else (0, 7, 19)
        stages = ("failed", "recovery") if scenario == "fail-close" else ("failed", "retry", "recovery")
        for _ in range(REPETITIONS):
            for position in positions:
                for stage in stages:
                    if take("SQ_DEVICE_CASE", ("position", "stage"), ("stage",)) != dict(position=position, stage=stage):
                        raise ValueError("wrong fault position/stage")
                    body.extend(runtime_call())
                    owned = take("SQ_OWNED", ("descriptors", "handles", "names", "heap", "fault_hits", "elapsed_ms"))
                    retained = stage != "recovery" and scenario != "fail-close"
                    expected_hits = 1 if stage == "failed" or scenario == "fail-close" else 2
                    if (owned["descriptors"] != (60 if retained else 0) or
                            owned["handles"] != (1 if retained else 0) or
                            owned["names"] != (60 if retained else 0) or
                            owned["fault_hits"] != expected_hits or owned["elapsed_ms"] >= 2000 or
                            (owned["heap"] < baseline if retained else owned["heap"] != baseline)):
                        raise ValueError("wrong retained/recovered resources or fault hits")
                    maximum_ms = max(maximum_ms, owned["elapsed_ms"])
        calls, failures, recoveries = ((24, 12, 12) if scenario == "fail-close" else (27, 18, 9))
        summary = take("SQ_DEVICE_FAULT", ("scenario", "repetitions", "calls", "failures", "recoveries",
                                          "descriptors", "handles", "names", "heap_growth"), ("scenario",))
        expected = dict(scenario=scenario, repetitions=REPETITIONS, calls=calls, failures=failures,
                        recoveries=recoveries, descriptors=0, handles=0, names=0, heap_growth=0)
        if summary != expected:
            raise ValueError("wrong device profile summary")
        # Reuse the host's complete business-delivery and failure-shape checks.
        # Device retries stay in one real owner; no substituted foreign PID.
        body.append(f"SQ_LIFECYCLE scenario={scenario} repetitions={REPETITIONS} calls={calls} "
                    f"failures={failures} recoveries={recoveries} fd_delta=0 thread_delta=0 queues_left=0")
        checked = validate("\n".join(body), scenario, REPETITIONS, foreign_owner=False)
        profiles.append(dict(summary, verified_recovery_events=checked["delivered_events"], max_call_ms=maximum_ms))
    batch = take("SQ_DEVICE_BATCH", ("repetitions", "calls", "failures", "recoveries",
                                    "heap_before", "heap_after", "status"))
    expected = dict(repetitions=REPETITIONS, calls=79, failures=48, recoveries=30,
                    heap_before=baseline, heap_after=baseline, status=0)
    if batch != expected or next(lines, None) is not None:
        raise ValueError("incomplete/failed/duplicate batch")
    return dict(batch, verified_warmup_events=warmup["result"]["received"],
                verified_recovery_events=sum(row["verified_recovery_events"] for row in profiles),
                profiles=profiles)


def capture(fd, args, language, block, out, label):
    raw = command(fd, f"sq_{language} faults", 120)
    (out / (label + "-faults.txt")).write_bytes(raw)
    result = parse_faults(raw.decode(errors="replace"))
    print("SERVICE_DEVICE_FAULT_PASS", language, block, "calls=", result["calls"], flush=True)
    return dict(language=language, block=block, **result)


def public_report(report):
    require_restored(report)
    header = kernel_header_identity(report["builds"])
    if (isinstance(report["blocks"], bool) or not isinstance(report["blocks"], int) or
            not 1 <= report["blocks"] <= 10 or set(report["builds"]) != {"c", "rust"}):
        raise ValueError("invalid paired diagnostic matrix")
    expected = [(block, language) for block in range(report["blocks"])
                for language in (("c", "rust") if block % 2 == 0 else ("rust", "c"))]
    if (not report.get("fault_injection") or
            [(row["block"], row["language"]) for row in report["runs"]] != expected):
        raise ValueError("failed/incomplete/unrestored diagnostic matrix")
    summaries = []
    row_fields = ("block", "language", "repetitions", "calls", "failures", "recoveries", "heap_before",
                  "heap_after", "status", "verified_warmup_events", "verified_recovery_events")
    profile_fields = ("scenario", "repetitions", "calls", "failures", "recoveries", "descriptors",
                      "handles", "names", "heap_growth", "verified_recovery_events", "max_call_ms")
    for row in report["runs"]:
        if (row["calls"] != 79 or row["failures"] != 48 or row["recoveries"] != 30 or
                row["repetitions"] != REPETITIONS or row["status"] != 0 or row["heap_before"] <= 0 or
                row["heap_before"] != row["heap_after"] or
                row["verified_warmup_events"] != 100 or row["verified_recovery_events"] != 3000):
            raise ValueError("failed diagnostic batch")
        if len(row["profiles"]) != 3:
            raise ValueError("incomplete diagnostic profiles")
        profiles = []
        for scenario, profile in zip(SCENARIOS, row["profiles"]):
            calls, failures, recoveries = (24, 12, 12) if scenario == "fail-close" else (27, 18, 9)
            expected_profile = dict(scenario=scenario, repetitions=REPETITIONS, calls=calls,
                                    failures=failures, recoveries=recoveries, descriptors=0, handles=0,
                                    names=0, heap_growth=0, verified_recovery_events=recoveries * 100)
            if (any(profile[key] != value for key, value in expected_profile.items()) or
                    not 0 <= profile["max_call_ms"] < 2000):
                raise ValueError("failed diagnostic profile")
            profiles.append({key: profile[key] for key in profile_fields})
        summaries.append(dict({key: row[key] for key in row_fields}, profiles=profiles))
    builds = {}
    for language, build in report["builds"].items():
        if not build.get("diagnostic_faults") or any(
                Path(name).is_absolute() or ".." in Path(name).parts for name in build["source_sha256"]):
            raise ValueError("not a public diagnostic source inventory")
        proof = build.get("compiler_input")
        builds[language] = dict(artifacts=build["artifacts"], source_sha256=build["source_sha256"],
                                config_identity=build["config_identity"],
                                kernel_header_sha256=header,
                                kernel_inventory_sha256=hashlib.sha256(json.dumps(build["kernel_archives"], sort_keys=True).encode()).hexdigest(),
                                compiler_package_sha256=proof["compiler_package_sha256"] if proof else None)
    return dict(schema=1, target="ESP32-S3 / NuttX", diagnostic_fault_injection=True,
                performance_claim=False, same_coordinator=True, foreign_task_recovery_qualified=False,
                interrupt_qualified=False, restoration_verified=True,
                paired_kernel_headers_verified=True, builds=builds,
                calls=sum(row["calls"] for row in report["runs"]),
                expected_failures=sum(row["failures"] for row in report["runs"]),
                recoveries=sum(row["recoveries"] for row in report["runs"]),
                verified_events=sum(row["verified_warmup_events"] + row["verified_recovery_events"] for row in report["runs"]),
                summaries=summaries, harness_sha256=report["harness_sha256"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("c", "rust", "backup", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("port", "flasher", "backup-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--blocks", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.blocks <= 10:
        parser.error("blocks must be between 1 and 10")
    args.rounds, args.warmup_rounds, args.events = 1, 0, 100
    measure(args, capture=capture)
    print("SERVICE_DEVICE_FAULT_MATRIX_PASS firmware_restored=true")


if __name__ == "__main__":
    main()
