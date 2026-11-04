#!/usr/bin/env python3
"""Run the finite P2 host corpus; preserve each actual execution and refusal."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_path_host_capture", HERE / "capture.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)
replay = capture.load("event_path_host_replay", HERE / "replay.py")
comparison = capture.load("event_path_host_compare", HERE / "compare.py")

NORMALIZED = (
    "ready-std-try", "ready-messaging-try", "ready-crossbeam-try", "full-messaging",
    "blocked-std-try", "blocked-messaging-try", "blocked-select-three", "fanin-messaging-4",
    "timer-provider-messaging",
)
SCHEDULED_FANIN = {
    "fanin-messaging-1-normal", "fanin-messaging-4-normal",
    "fanin-messaging-1-saturation", "fanin-messaging-4-saturation",
}
EXPECTED_RUNS = 42
EXPECTED_COMPARISONS = 7


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new; retain previous attempts")
    if not SCHEDULED_FANIN.issubset(capture.analysis.CASES):
        parser.error("case catalog must include all four scheduled messaging fan-in controls")
    if "timer-provider-messaging" not in capture.analysis.CASES:
        parser.error("case catalog must include the native timer/provider/handler control")
    cases = [(case, "bytes16") for case in capture.analysis.CASES if case != "fixed-peer"]
    cases += [(case, "imu16") for case in NORMALIZED]
    cases.append(("fixed-peer", "bytes16"))
    if len(cases) != EXPECTED_RUNS:
        parser.error(f"expected {EXPECTED_RUNS} host corpus runs; found {len(cases)}")
    if len(comparison.GROUPS) != EXPECTED_COMPARISONS:
        parser.error(f"expected {EXPECTED_COMPARISONS} fixed comparison groups")
    args.output.mkdir(parents=True)
    source = args.binary.resolve(strict=True)
    source_sha256 = capture.digest(source)
    binary = args.output.resolve() / "binary/event-path-perf"
    binary.parent.mkdir()
    shutil.copy2(source, binary)
    if capture.digest(binary) != source_sha256 or capture.digest(source) != source_sha256:
        parser.error("executable changed while retaining the corpus binary")
    results = []
    for case, payload in cases:
        out = args.output / f"{case}-{payload}"
        result = capture.capture(SimpleNamespace(
            runtime="host", binary=binary, case=case, samples=16, warmup=4,
            payload=payload, output=out, timeout=30, artifact_root=args.output.resolve(),
        ))
        expected_refusal = case == "fixed-peer"
        if expected_refusal:
            accepted = (result["capture_complete"] is False and result.get("returncode") == 1
                        and "fixed-peer-requires-single-core-fifo" in (out / "console.log").read_text())
        else:
            accepted = result["capture_complete"] is True
        replay_error = None
        try:
            checked = replay.replay(out, artifact_root=args.output)
            evidence_replayed = (checked["evidence_valid"] is True
                                 and checked["capture_complete"] == result["capture_complete"])
        except (OSError, ValueError, RuntimeError) as error:
            evidence_replayed = False
            replay_error = f"{type(error).__name__}: {error}"
        accepted = accepted and evidence_replayed
        results.append({"case": case, "payload": payload, "expected_refusal": expected_refusal,
                        "accepted": accepted, "evidence_replayed": evidence_replayed,
                        "replay_error": replay_error,
                        "capture": str(out.relative_to(args.output) / "capture.json")})
    comparisons = []
    comparison_directory = args.output / "comparisons"
    comparison_directory.mkdir()
    for group, members in comparison.GROUPS.items():
        error = None
        try:
            result = comparison.compare(group, [args.output / f"{case}-bytes16" for case in members],
                                        artifact_root=args.output)
        except (OSError, ValueError, RuntimeError) as failure:
            error = f"{type(failure).__name__}: {failure}"
            result = {"schema": "event-path-comparison-failure-v1", "group": group,
                      "matched": False, "error": error, "timing_qualified": False,
                      "execution_context_verified": False, "physical_execution_verified": False,
                      "physical_timing_qualified": False, "priority_progress_qualified": False}
        report = comparison_directory / f"{group}.json"
        report.write_text(json.dumps(result, indent=2) + "\n")
        comparisons.append({"group": group, "matched": result["matched"], "error": error,
                            "report": report.relative_to(args.output).as_posix()})
    summary = {"schema": "event-path-host-corpus-v1", "runtime_kind": "host-functional-only",
               "expected_runs": EXPECTED_RUNS,
               "expected_comparisons": EXPECTED_COMPARISONS, "comparisons": comparisons,
               "binary_file": "binary/event-path-perf", "binary_sha256": source_sha256,
               "success": (all(item["accepted"] for item in results)
                           and all(item["matched"] for item in comparisons)), "cases": results,
               "timing_qualified": False, "priority_progress_qualified": False}
    (args.output / "corpus.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"P2 host corpus: {sum(item['accepted'] for item in results)}/{len(results)} expectations satisfied")
    print(f"P2 comparison groups: {sum(item['matched'] for item in comparisons)}/{len(comparisons)} matched")
    if not summary["success"]:
        parser.exit(1, "P2 corpus failed; all raw outputs retained\n")


if __name__ == "__main__":
    main()

