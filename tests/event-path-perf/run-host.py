#!/usr/bin/env python3
"""Run the finite P2 host corpus; preserve each actual execution and refusal."""
import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_path_host_capture", HERE / "capture.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)

NORMALIZED = (
    "ready-std-try", "ready-messaging-try", "ready-crossbeam-try", "full-messaging",
    "blocked-std-try", "blocked-messaging-try", "blocked-select-three", "fanin-messaging-4",
)
SCHEDULED_FANIN = {
    "fanin-messaging-1-normal", "fanin-messaging-4-normal",
    "fanin-messaging-1-saturation", "fanin-messaging-4-saturation",
}
EXPECTED_RUNS = 40


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new; retain previous attempts")
    if not SCHEDULED_FANIN.issubset(capture.analysis.CASES):
        parser.error("case catalog must include all four scheduled messaging fan-in controls")
    cases = [(case, "bytes16") for case in capture.analysis.CASES if case != "fixed-peer"]
    cases += [(case, "imu16") for case in NORMALIZED]
    cases.append(("fixed-peer", "bytes16"))
    if len(cases) != EXPECTED_RUNS:
        parser.error(f"expected {EXPECTED_RUNS} host corpus runs; found {len(cases)}")
    args.output.mkdir(parents=True)
    results = []
    for case, payload in cases:
        out = args.output / f"{case}-{payload}"
        result = capture.capture(SimpleNamespace(
            runtime="host", binary=args.binary, case=case, samples=16, warmup=4,
            payload=payload, output=out, timeout=30,
        ))
        expected_refusal = case == "fixed-peer"
        if expected_refusal:
            accepted = (result["capture_complete"] is False and result.get("returncode") == 1
                        and "fixed-peer-requires-single-core-fifo" in (out / "console.log").read_text())
        else:
            accepted = result["capture_complete"] is True
        results.append({"case": case, "payload": payload, "expected_refusal": expected_refusal,
                        "accepted": accepted, "capture": str(out.relative_to(args.output) / "capture.json")})
    summary = {"schema": "event-path-host-corpus-v1", "runtime_kind": "host-functional-only",
               "expected_runs": EXPECTED_RUNS,
               "success": all(item["accepted"] for item in results), "cases": results,
               "timing_qualified": False, "priority_progress_qualified": False}
    (args.output / "corpus.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"P2 host corpus: {sum(item['accepted'] for item in results)}/{len(results)} expectations satisfied")
    if not summary["success"]:
        parser.exit(1, "P2 corpus failed; all raw outputs retained\n")


if __name__ == "__main__":
    main()

