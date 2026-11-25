#!/usr/bin/env python3
"""Validate one captured RAM control. Host bytes never become target RAM."""
import argparse
import hashlib
import json
from pathlib import Path
import re


PHASES = {
    "baseline": ["baseline", "released"],
    "threads": ["baseline", "handle-storage", "workers-parked", "released"],
    "queues": ["baseline", "empty", "full", "drained", "released"],
    "services": ["baseline", "empty", "handle-storage", "workers-parked", "full",
                 "drained", "first-wait-complete", "repeat-wait-complete", "joined", "released"],
}
NATIVE_SAMPLE_FIELDS = ("native_heap_used", "native_heap_free", "native_heap_arena",
                        "main_stack_usable", "main_stack_observed")


def integer(value):
    return type(value) is int and value >= 0


def object_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field in RAM evidence")
        result[key] = value
    return result


def parse(text, expected_domain):
    if expected_domain not in ("host-functional", "nuttx-target"):
        raise ValueError("an explicit supported measurement domain is required")
    cases, samples, workers, reporting_stacks = [], [], [], []
    ended = False
    command_exited = False
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")
    text = re.sub(r"(?m)^nsh> ", "", text)
    if re.search(r"RAM_FAIL|\bpanic(?:ked)?\b|\bassertion failed\b|\bassert failed\b|\bGuru Meditation\b|\bFATAL ERROR\b", text, re.I):
        raise ValueError("capture contains a firmware failure, including after RAM_END")
    for line in text.splitlines():
        line = line.strip()
        for prefix, output in (("RAM_CASE ", cases), ("RAM_SAMPLE ", samples),
                               ("RAM_WORKER ", workers), ("RAM_REPORT_STACK ", reporting_stacks)):
            if line.startswith(prefix):
                if ended:
                    raise ValueError("records after RAM_END")
                value = json.loads(line[len(prefix):], object_pairs_hook=object_fields)
                if not isinstance(value, dict):
                    raise ValueError("RAM records must be JSON objects")
                output.append(value)
                break
        else:
            if line.startswith("RAM_END"):
                if line != "RAM_END valid=true" or ended:
                    raise ValueError("invalid/duplicate completion marker")
                ended = True
            elif line.startswith("RAM_COMMAND_EXIT"):
                if (expected_domain != "nuttx-target" or not ended or command_exited or
                        line != "RAM_COMMAND_EXIT status=0"):
                    raise ValueError("target command failed, exited before completion, or has duplicate status")
                command_exited = True
    if len(cases) != 1 or len(reporting_stacks) != 1 or not ended:
        raise ValueError("capture must contain exactly one completed invocation")
    case = cases[0]
    if type(case.get("schema")) is not int or case["schema"] != 1 or case.get("domain") != expected_domain:
        raise ValueError("schema/domain mismatch")
    if case.get("valid") is not True or case.get("payload_valid") is not True:
        raise ValueError("fixture did not validate payload/lifecycle")
    kind = case.get("case")
    if kind not in PHASES or [sample.get("phase") for sample in samples] != PHASES[kind]:
        raise ValueError("missing, duplicated or reordered measurement phases")
    for key in ("owners", "workers", "queues", "slots_per_queue", "event_bytes",
                "payload_capacity_bytes", "stack_request_per_worker", "stack_request_total", "workers_joined"):
        if not integer(case.get(key)):
            raise ValueError(f"invalid numeric case field: {key}")
    if case["owners"] not in (0, 1, 5, 10, 20) or case["event_bytes"] != 64:
        raise ValueError("unmatched workload dimensions")
    expected_workers = case["owners"] if kind in ("threads", "services") else 0
    if case["workers"] != expected_workers or case.get("workers_joined") != expected_workers:
        raise ValueError("worker lifecycle count mismatch")
    if len(workers) != expected_workers or [w.get("index") for w in workers] != list(range(expected_workers)):
        raise ValueError("incomplete worker observations")
    if case["stack_request_total"] != expected_workers * case["stack_request_per_worker"]:
        raise ValueError("stack request accounting mismatch")
    if case["payload_capacity_bytes"] != case["queues"] * case["slots_per_queue"] * 64:
        raise ValueError("payload capacity accounting mismatch")
    if kind in ("queues", "services") and case["payload_capacity_bytes"] != case["owners"] * 3 * 8 * 64:
        raise ValueError("queue capacities do not match the experiment")
    if kind in ("baseline", "threads"):
        if case["queues"] or case["slots_per_queue"] or case.get("transport") != "none":
            raise ValueError("baseline/thread-only control unexpectedly contains queues")
    else:
        transport = case.get("transport")
        if transport not in ("nxrs-std-inbox", "crossbeam-select-candidate"):
            raise ValueError("unrecognized channel implementation")
        merged = kind == "services" and transport == "nxrs-std-inbox"
        if case["queues"] != case["owners"] * (1 if merged else 3) or case["slots_per_queue"] != (24 if merged else 8):
            raise ValueError("queue topology was relabelled")
    if kind == "baseline" and (case["owners"] or case["stack_request_per_worker"]):
        raise ValueError("baseline has workload reservations")
    if kind != "baseline" and not case["stack_request_per_worker"]:
        raise ValueError("missing explicit stack request")
    native = expected_domain == "nuttx-target"
    if native and not command_exited:
        raise ValueError("target capture requires successful shell exit after RAM_END")
    if case.get("native_heap_available") is not native or case.get("native_stack_available") is not native:
        raise ValueError("native observation availability mismatch")
    previous = None
    for sample in samples:
        for key in ("rust_live", "rust_blocks", "rust_peak_process", "alloc", "zeroed", "realloc", "dealloc"):
            if not integer(sample.get(key)):
                raise ValueError(f"invalid allocator field: {key}")
        if sample["rust_peak_process"] < sample["rust_live"]:
            raise ValueError("live allocations exceed process peak")
        if previous and any(sample[key] < previous[key] for key in ("rust_peak_process", "alloc", "zeroed", "realloc", "dealloc")):
            raise ValueError("allocator counters went backwards")
        for key in NATIVE_SAMPLE_FIELDS:
            if native and not integer(sample.get(key)) or not native and sample.get(key) is not None:
                raise ValueError("host/target accounting was mixed")
        if native and not 0 < sample["main_stack_observed"] <= sample["main_stack_usable"]:
            raise ValueError("invalid native main stack observation")
        if native:
            if sample["native_heap_used"] + sample["native_heap_free"] != sample["native_heap_arena"]:
                raise ValueError("native heap used/free bytes do not sum to its arena")
            if previous and (sample["main_stack_usable"] != previous["main_stack_usable"] or
                             sample["main_stack_observed"] < previous["main_stack_observed"]):
                raise ValueError("main stack capacity changed or its cumulative watermark decreased")
        previous = sample
    for worker in workers:
        if any(not integer(worker.get(key)) for key in ("index", "received", "timeouts")):
            raise ValueError("invalid worker lifecycle observation")
        if worker.get("received") != (24 if kind == "services" else 0):
            raise ValueError("worker delivery count mismatch")
        if worker.get("timeouts") != (9 if kind == "services" else 0):
            raise ValueError("blocking wait qualification incomplete")
        if native:
            if not integer(worker.get("stack_usable")) or not integer(worker.get("stack_observed")):
                raise ValueError("missing native worker stack observation")
            if not 0 < worker["stack_observed"] <= worker["stack_usable"]:
                raise ValueError("invalid native worker stack observation")
        elif worker.get("stack_usable") is not None or worker.get("stack_observed") is not None:
            raise ValueError("host stack requests must not be presented as measured stack RAM")
    reporting_stack = reporting_stacks[0]
    if native:
        if not integer(reporting_stack.get("stack_usable")) or not integer(reporting_stack.get("stack_observed")):
            raise ValueError("missing main reporting stack observation")
        if not 0 < reporting_stack["stack_observed"] <= reporting_stack["stack_usable"]:
            raise ValueError("invalid main reporting stack observation")
        if (reporting_stack["stack_usable"] != samples[-1]["main_stack_usable"] or
                reporting_stack["stack_observed"] < samples[-1]["main_stack_observed"]):
            raise ValueError("reporting stack disagrees with the cumulative main stack observation")
    elif reporting_stack.get("stack_usable") is not None or reporting_stack.get("stack_observed") is not None:
        raise ValueError("host report stack measurement is unavailable")
    initial, released = samples[0], samples[-1]
    for key, field in (("released_rust_bytes_delta", "rust_live"), ("released_rust_blocks_delta", "rust_blocks")):
        if type(case.get(key)) is not int or case[key] != released[field] - initial[field]:
            raise ValueError("released resource accounting mismatch")
    if kind in ("queues", "baseline") and (case["released_rust_bytes_delta"] or case["released_rust_blocks_delta"]):
        raise ValueError("queue-only/baseline allocations were not reclaimed")
    deltas = [{"phase": sample["phase"],
               "rust_requested_bytes_from_baseline": sample["rust_live"] - initial["rust_live"],
               "native_heap_used_from_baseline": sample["native_heap_used"] - initial["native_heap_used"] if native else None}
              for sample in samples]
    return {"case": case, "samples": samples, "workers": workers, "deltas": deltas,
            "reporting_stack": reporting_stack,
            "accounting": {
                "rust_bytes_are_subset_of_native_heap": native,
                "do_not_add_rust_bytes_to_native_heap": True,
                "host_thread_stack_memory_measured": False,
                "stack_requests_are_not_measured_usage": True,
                "process_peak_is_not_a_reset_per_phase_peak": True,
                "physical_stack_bound_proven": False,
                "linked_sections_included": False,
            }}


def verify_target(text, build_dir, readelf, *, binary, case, owners, stack_bytes):
    """Bind one successfully exited target capture to its build and invocation."""
    from target_evidence import verify_build
    report = parse(text, "nuttx-target")
    expected = {"case": case, "owners": owners, "stack_request_per_worker": stack_bytes}
    kinds = {"ram-baseline": ("baseline",), "ram-threads": ("threads",),
             "ram-std-inbox": ("queues", "services"),
             "ram-crossbeam-select": ("queues", "services")}
    transports = {"ram-baseline": "none", "ram-threads": "none",
                  "ram-std-inbox": "nxrs-std-inbox",
                  "ram-crossbeam-select": "crossbeam-select-candidate"}
    if (binary not in kinds or case not in kinds[binary] or
            not integer(owners) or not integer(stack_bytes) or
            any(report["case"].get(key) != value for key, value in expected.items()) or
            report["case"]["transport"] != transports[binary]):
        raise ValueError("captured RAM invocation disagrees with the requested binary/case/count/stack")
    report["build_identity"] = verify_build(build_dir, readelf, binary)
    report["raw_output_sha256"] = hashlib.sha256(text.encode("latin-1")).hexdigest()
    report["accounting"]["linked_sections_included"] = True
    report["validation_scope"] = "frozen build and transcript; operator must retain flash/readback and fresh-boot provenance"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--domain", choices=("host-functional", "nuttx-target"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--build", type=Path, help="completed canonical NuttX build directory")
    parser.add_argument("--readelf")
    parser.add_argument("--binary")
    parser.add_argument("--case", choices=PHASES)
    parser.add_argument("--owners", type=int)
    parser.add_argument("--stack-bytes", type=int)
    args = parser.parse_args()
    text = args.input.read_bytes().decode("latin-1")
    target_values = (args.build, args.readelf, args.binary, args.case, args.owners, args.stack_bytes)
    if args.domain == "nuttx-target":
        if any(value is None for value in target_values):
            parser.error("target replay requires --build --readelf --binary --case --owners --stack-bytes")
        report = verify_target(text, args.build, args.readelf, binary=args.binary,
                               case=args.case, owners=args.owners, stack_bytes=args.stack_bytes)
    else:
        if any(value is not None for value in target_values):
            parser.error("target build arguments cannot be attached to host evidence")
        report = parse(text, args.domain)
    with args.out.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
