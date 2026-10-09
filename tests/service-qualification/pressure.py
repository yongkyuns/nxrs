#!/usr/bin/env python3
"""Diagnostic queue saturation and cancellation, not a speed/footprint result."""
import argparse
import hashlib
import json
from pathlib import Path

from evidence import kernel_header_identity, psram_identity, require_restored
from restart_device import command, measure
from results import _ANSI_ESCAPE, _parse_row, parse, validate_memory

HERE = Path(__file__).resolve().parent
END_FIELDS = ("phase", "index", "source_ok", "forward_ok", "source_full", "forward_full",
              "control_full", "data_full", "terminal", "gates", "skipped", "cancelled",
              "sems", "stop_sent", "stop_full", "joined", "closed", "unlinked",
              "heap_before_drain", "deferred_reclaimed", "heap", "elapsed_ms", "status")
BATCH_FIELDS = ("repetitions", "calls", "expected_cancellations", "verified_events", "heap_before", "heap_after", "status")
STEPS = [("warmup", 0)] + [(phase, index) for index in range(3) for phase in ("load", "cancel", "recovery")]


def check_profile(row, baseline):
    if any(type(row[field]) is not int or row[field] < 0 for field in END_FIELDS if field != "phase"):
        raise ValueError("pressure counts must be unsigned integers")
    phase = row["phase"]
    if (phase not in ("warmup", "load", "cancel", "recovery") or row["sems"] != 2 or
            row["joined"] != 20 or row["closed"] != 60 or row["unlinked"] != 60 or
            not 0 < row["stop_sent"] <= 20 or row["stop_full"] != 0 or
            row["elapsed_ms"] >= 10000 or row["heap"] != baseline or
            row["heap_before_drain"] - row["deferred_reclaimed"] != row["heap"] or
            row["source_full"] + row["forward_full"] != row["control_full"] + row["data_full"]):
        raise ValueError("failed pressure resources/heap/accounting")
    if phase == "cancel":
        if (row["status"] != 1 or row["cancelled"] != 1 or row["source_full"] != 1 or
                not 0 < row["source_ok"] < 2048 or row["skipped"] != row["source_ok"] or
                row["forward_ok"] > row["source_ok"] * 19 or row["terminal"] > row["source_ok"] or
                not 1 <= row["gates"] <= 32):
            raise ValueError("cancellation was not triggered by real queue pressure")
    else:
        events = 2048 if phase == "load" else 100
        if (row["status"] != 0 or row["cancelled"] != 0 or row["source_ok"] != events or
                row["forward_ok"] != events * 19 or row["terminal"] != events or
                row["gates"] != (32 if phase == "load" else 0) or
                row["skipped"] != (events if phase == "load" else 0)):
            raise ValueError("incomplete pressure/recovery delivery")
        if phase == "load" and (row["source_full"] == 0 or row["forward_full"] == 0):
            raise ValueError("no observed producer/forwarder saturation")


def parse_pressure(text):
    lines = iter(line.strip() for line in _ANSI_ESCAPE.sub("", text).splitlines()
                 if line.strip().startswith("SQ_"))

    def take(kind, fields=(), strings=()):
        parts = next(lines, "").split()
        if not parts or parts[0] != kind or len(parts) != len(fields) + 1:
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

    profiles, baseline = [], None
    for phase, index in STEPS:
        if take("SQ_PRESSURE_BEGIN", ("phase", "index"), ("phase",)) != dict(phase=phase, index=index):
            raise ValueError("wrong pressure phase/order")
        body = []
        for line in lines:
            body.append(line)
            if line.partition(" ")[0] == "SQ_DONE":
                break
            if line.partition(" ")[0] not in ("SQ_MEMORY", "SQ_RESULT"):
                raise ValueError("unexpected pressure runtime record")
        kinds = [line.partition(" ")[0] for line in body]
        if phase == "cancel":
            if kinds != ["SQ_MEMORY", "SQ_DONE"] or _parse_row("SQ_DONE", body[-1])["status"] != 1:
                raise ValueError("unexpected cancellation record")
            validate_memory(_parse_row("SQ_MEMORY", body[0]), 20)
        else:
            parse("\n".join(body), services=20, events=2048 if phase == "load" else 100, source="messages")
        row = take("SQ_PRESSURE_END", END_FIELDS, ("phase",))
        if (row["phase"], row["index"]) != (phase, index):
            raise ValueError("mismatched pressure phase")
        if phase == "warmup":
            baseline = take("SQ_PRESSURE_BASELINE", ("heap",))["heap"]
        check_profile(row, baseline)
        profiles.append(row)
    batch = take("SQ_PRESSURE_BATCH", BATCH_FIELDS)
    expected = dict(repetitions=3, calls=10, expected_cancellations=3, verified_events=6544,
                    heap_before=baseline, heap_after=baseline, status=0)
    if batch != expected or next(lines, None) is not None:
        raise ValueError("incomplete/failed/duplicate pressure batch")
    return dict(batch, profiles=profiles)


def capture(fd, args, language, block, out, label):
    raw = command(fd, f"sq_{language} pressure", 120)
    (out / (label + "-pressure.txt")).write_bytes(raw)
    result = parse_pressure(raw.decode(errors="replace"))
    print("SERVICE_PRESSURE_PASS", language, block, "verified_events=", result["verified_events"], flush=True)
    return dict(language=language, block=block, **result)


def public_report(report):
    require_restored(report)
    builds = report["builds"]
    header, psram = kernel_header_identity(builds), psram_identity(builds)
    if (report.get("diagnostic_capture") != "pressure" or type(report.get("blocks")) is not int or
            not 1 <= report["blocks"] <= 10 or set(builds) != {"c", "rust"}):
        raise ValueError("paired diagnostic pressure capture required")
    for field in ("config_identity", "kernel_archives", "c_flags", "c_compiler_sha256", "thread_stack"):
        if builds["c"][field] != builds["rust"][field]:
            raise ValueError("paired pressure inputs differ")
    if builds["c"]["thread_stack"] != 4096:
        raise ValueError("pressure worker stacks changed")
    expected = [(block, language) for block in range(report["blocks"])
                for language in (("c", "rust") if block % 2 == 0 else ("rust", "c"))]
    if [(row["block"], row["language"]) for row in report["runs"]] != expected:
        raise ValueError("incomplete or reordered pressure matrix")
    summaries = []
    for row in report["runs"]:
        if ([(p["phase"], p["index"]) for p in row["profiles"]] != STEPS or
                any(row[key] != value for key, value in dict(repetitions=3, calls=10,
                    expected_cancellations=3, verified_events=6544, heap_after=row["heap_before"], status=0).items()) or
                row["heap_before"] <= 0):
            raise ValueError("failed diagnostic pressure batch")
        for profile in row["profiles"]:
            check_profile(profile, row["heap_before"])
        summaries.append(dict(block=row["block"], language=row["language"],
                              **{key: row[key] for key in BATCH_FIELDS},
                              profiles=[{key: p[key] for key in END_FIELDS} for p in row["profiles"]]))
    public_builds = {}
    for language, build in builds.items():
        if (not build.get("diagnostic_pressure") or build.get("diagnostic_faults") or
                build.get("diagnostic_perfmon") or build.get("diagnostic_hot_iram") or
                build.get("diagnostic_layout_padding_bytes") is not None or
                any(Path(name).is_absolute() or ".." in Path(name).parts
                    for inventory in (build["source_sha256"], build["artifacts"]) for name in inventory)):
            raise ValueError("not a separate public pressure image")
        proof = build.get("compiler_input")
        public_builds[language] = dict(artifacts=build["artifacts"], source_sha256=build["source_sha256"],
            config_identity=build["config_identity"], kernel_header_sha256=header,
            kernel_inventory_sha256=hashlib.sha256(json.dumps(build["kernel_archives"], sort_keys=True).encode()).hexdigest(),
            c_flags=build["c_flags"], c_compiler_sha256=build["c_compiler_sha256"], thread_stack_bytes=4096,
            compiler_package_sha256=proof["compiler_package_sha256"] if proof else None)
        if psram is not None:
            public_builds[language]["psram_enabled"] = psram
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in report["harness_sha256"]):
        raise ValueError("private pressure harness path")
    return dict(schema=1, target="ESP32-S3 / NuttX", diagnostic_pressure=True,
                performance_claim=False, ordinary_image_footprint_claim=False,
                same_coordinator=True, interrupt_qualified=False, arbitrary_overload_qualified=False,
                services=20, queues=60, event_bytes=16, queue_capacities=[1, 8, 8],
                sink_pause_us=20000, sink_pause_every_events=64, load_events_per_call=2048,
                producer_pacing_suppressed=True, retry_sleep_preserved=True,
                post_join_heap_delaylist_drained=True,
                initial_capacity_probe_excluded_from_pressure_counts=True,
                restoration_verified=True, paired_kernel_headers_verified=True,
                builds=public_builds, summaries=summaries,
                calls=sum(row["calls"] for row in summaries),
                expected_cancellations=sum(row["expected_cancellations"] for row in summaries),
                verified_events=sum(row["verified_events"] for row in summaries),
                harness_sha256=report["harness_sha256"])


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
    measure(args, capture=capture, diagnostic="pressure")


if __name__ == "__main__":
    main()
