#!/usr/bin/env python3
"""Check actual product-encoder vectors with the existing dashboard decoder.

Only dashboard.decode_batch is compiled from its unchanged source AST, with
struct as its sole imported dependency. No BLE, TLS, UI, Swift, or physical
transport is exercised. Header/terminal checks here are a separate wire oracle.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "tests/nuttx-std/tracker-sim/dashboard.py"
FIXTURE = ROOT / "tests/mpsc-comparison/rust/tests/support/capture_peer.rs"
CASES = {
    "mixed-v1": (1, 9),
    "mixed-v2": (2, 9),
    "mixed-v3": (3, 9),
    "unanchored-v3": (3, 2),
    "raw-bits-v3": (3, 2),
    "overflow-v1": (1, 0),
    "overflow-v2": (2, 0),
    "overflow-v3": (3, 0),
}
COMPILED_SOURCES = {
    "capture_blob": "app/motion-tracker/src/communications/export.rs",
    "clock_blob": "app/motion-tracker/src/clock.rs",
    "production_gate_blob": "shared/messaging/src/pooled/gate.rs",
    "production_pool_blob": "shared/messaging/src/pooled.rs",
    "policy_blob": "app/motion-tracker/src/communications/export/policy.rs",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git_blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def load_decoder():
    source = DASHBOARD.read_text()
    tree = ast.parse(source, filename=str(DASHBOARD))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "decode_batch"]
    require(len(functions) == 1, "dashboard must contain one decode_batch")
    namespace = {"struct": struct}
    selected = ast.Module(body=functions, type_ignores=[])
    exec(compile(selected, str(DASHBOARD), "exec"), namespace)
    return namespace["decode_batch"], source


def tags(payload, version):
    """Inspect expected wire choices separately from actual decoder execution."""
    offset = 8 if version == 3 else 0
    result = []
    while offset < len(payload):
        tag = payload[offset]
        delta = version >= 2 and bool(tag & 0x80)
        counts = version == 3 and bool(tag & 0x40)
        no_utc = version == 3 and bool(tag & 0x20)
        source = tag & (0x1f if version == 3 else 0x7f)
        require(source in (1, 2, 3), "invalid wire source")
        require(not (source == 3 and counts), "GNSS cannot use native IMU counts")
        header = 2 if delta else 13 if version == 3 else 9
        offset += header + (20 if source == 3 else 12 if counts else 24)
        require(offset <= len(payload), "partial record in vector")
        result.append((delta, counts, no_utc))
    require(offset == len(payload), "unconsumed vector bytes")
    return result


def value_bits(source, values):
    if source == 3:
        require(len(values) == 3, "GNSS scalar count changed")
        return [struct.unpack("<Q", struct.pack("<d", value))[0] for value in values[:2]] + [
            struct.unpack("<I", struct.pack("<f", values[2]))[0]]
    require(len(values) == 6, "IMU scalar count changed")
    # Python computes native-count products as doubles; compare the declared
    # wire's f32 domain, including signed zero and the quiet-NaN payload.
    return [struct.unpack("<I", struct.pack("<f", value))[0] for value in values]


def check_row(row, decode_batch, fixture_blob):
    case = row.get("case")
    require(case in CASES, f"unexpected peer case: {case}")
    version, count = CASES[case]
    require(row.get("schema") == 1 and row.get("version") == version, f"{case}: schema/version")
    require(row.get("peer_fixture_blob") == fixture_blob, f"{case}: stale compiled peer fixture")
    require(row.get("swift_finite_domain") == (case != "raw-bits-v3"), f"{case}: Swift scope")
    frame = bytes.fromhex(row["frame_hex"])
    require(len(frame) >= 4 and frame[:2] == bytes([0xa5, version]), f"{case}: header")
    require(struct.unpack_from("<H", frame, 2)[0] == len(frame) - 4, f"{case}: frame length")
    require(len(row["expected"]) == count, f"{case}: expected record count")
    terminal = case.startswith("overflow-")
    require(row.get("terminal") is terminal, f"{case}: terminal classification")
    if terminal:
        require(row.get("accepted_before_terminal") == 512, f"{case}: overflow capacity")
        if version == 1:
            require(frame == b"\xa5\x01\x00\x00" and row["missing"] is None,
                    f"{case}: fixed overflow framing")
        else:
            require(len(frame) == 9 and frame[2:5] == b"\x05\x00\x03",
                    f"{case}: keyed overflow framing")
            require(struct.unpack_from("<I", frame, 5)[0] == 512,
                    f"{case}: first missing sequence")
            require(row["missing"] == {"source": 3, "sequence": 512},
                    f"{case}: first missing identity")
        return {"case": case, "version": version, "records": 0,
                "terminal": True, "frame_bytes": len(frame)}

    require(row.get("accepted_before_terminal") == 0 and row["missing"] is None,
            f"{case}: unexpected terminal metadata")
    require(33 <= len(frame) <= (604 if version == 3 else 532), f"{case}: peer frame bounds")
    scales = tuple(None if bits is None else struct.unpack("<ff", struct.pack("<II", *bits))
                   for bits in row["scales_bits"])
    actual = decode_batch(frame[4:], version, scales)
    actual_tags = tags(frame[4:], version)
    require(len(actual) == count and len(actual_tags) == count, f"{case}: decoded record count")
    for index, (record, expected, flags) in enumerate(zip(actual, row["expected"], actual_tags)):
        source, sequence, time_ms, utc_ms, values = record
        label = f"{case} record {index}"
        require((source, sequence, time_ms, utc_ms) == (
            expected["source"], expected["sequence"], expected["time_ms"], expected["utc_ms"]),
            f"{label}: source/sequence/time/UTC")
        require(value_bits(source, values) == expected["values_bits"], f"{label}: scalar bits")
        require(flags == (expected["delta"], expected["counts"], expected["no_utc"]),
                f"{label}: compression/UTC flags")
    if case.startswith("mixed-"):
        require([item[0] for item in actual] == [1, 2, 3] * 3, f"{case}: cross-source drain order")
        for source in (1, 2, 3):
            require([item[1] for item in actual if item[0] == source] == [0xffffffff, 0, 2],
                    f"{case}: per-source wrap/gap")
        require(actual[0][2] > actual[1][2], f"{case}: cross-source time reversal absent")
    if case in ("unanchored-v3", "raw-bits-v3"):
        require(struct.unpack_from("<q", frame, 4)[0] == -(1 << 63), f"{case}: missing UTC sentinel")
    return {"case": case, "version": version, "records": count,
            "terminal": False, "frame_bytes": len(frame)}


def validate(rows, metadata, decode_batch):
    require(len(rows) == len(CASES) and {row.get("case") for row in rows} == set(CASES),
            "missing, duplicate, or unexpected peer cases")
    require(metadata.get("runtime") == "host" and metadata.get("source_hash") == "git-blob-sha1",
            "peer vectors require host compiled-source metadata")
    for field, relative in COMPILED_SOURCES.items():
        require(metadata.get(field) == git_blob((ROOT / relative).read_bytes()),
                f"stale compiled source: {relative}")
    fixture_blob = git_blob(FIXTURE.read_bytes())
    return [check_row(row, decode_batch, fixture_blob) for row in rows]


def negative_controls(rows, metadata, decode_batch):
    controls = []
    mutations = {
        "changed-scalar-bit": lambda data: data[0]["expected"][0]["values_bits"].__setitem__(
            0, data[0]["expected"][0]["values_bits"][0] ^ 1),
        "changed-source": lambda data: data[0]["expected"][0].__setitem__("source", 2),
        "truncated-frame": lambda data: data[0].__setitem__("frame_hex", data[0]["frame_hex"][:-2]),
        "missing-case": lambda data: data.pop(),
        "stale-fixture": lambda data: data[0].__setitem__("peer_fixture_blob", "0" * 40),
        "wrong-overflow-key": lambda data: data[-1]["missing"].__setitem__("sequence", 513),
    }
    for name, mutate in mutations.items():
        damaged = copy.deepcopy(rows)
        mutate(damaged)
        try:
            validate(damaged, metadata, decode_batch)
        except (ValueError, AssertionError, KeyError, struct.error):
            controls.append(name)
        else:
            raise ValueError(f"negative control accepted: {name}")
    return controls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_log", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    raw = args.capture_log.read_bytes()
    lines = raw.decode().splitlines()
    rows = [json.loads(line.removeprefix("CAPTURE_PEER "))
            for line in lines if line.startswith("CAPTURE_PEER ")]
    metadata = [json.loads(line.removeprefix("CAPTURE_METADATA "))
                for line in lines if line.startswith("CAPTURE_METADATA ")]
    require(len(metadata) == 1, "expected one compiled-source metadata record")
    decode_batch, source = load_decoder()
    cases = validate(rows, metadata[0], decode_batch)
    controls = negative_controls(rows, metadata[0], decode_batch)
    report = {
        "schema": 1,
        "passed": True,
        "runtime": "host",
        "scope": "actual-product-encoder-to-existing-dashboard-decode_batch",
        "dashboard_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "peer_fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "capture_log_sha256": hashlib.sha256(raw).hexdigest(),
        "compiled_sources": {field: metadata[0][field] for field in COMPILED_SOURCES},
        "cases": cases,
        "records_decoded": sum(case["records"] for case in cases),
        "negative_controls_rejected": controls,
        "limits": [
            "Only the unchanged Python batch decoder runs; framing/terminal assertions are separate.",
            "No Swift execution, iPhone, TLS/BLE transport, native scheduling, or physical timing.",
            "Quiet-NaN preservation is a scalar transport control; Swift intentionally rejects nonfinite measurements.",
        ],
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
