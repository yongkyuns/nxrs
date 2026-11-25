#!/usr/bin/env python3
"""Freeze a finite board campaign and replay its complete evidence census.

This entry point performs no device I/O. Fixture runners own their protocols;
this file only checks the inventory, orders runs, and invokes their validators.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from rtos_harness.console import BOOT_MARKERS, fresh_shell_boot

MPSC = ROOT / "tests/mpsc-comparison/bench.py"
OWNER = ROOT / "tests/owner-qualification/workflow.py"
RAM = ROOT / "tests/ram-footprint-std"
NATIVE = ROOT / "tests/event-services-comparison/ram_matrix.py"
ARMS = ("nuttx-std", "nuttx-native", "zephyr-native")
BOARDS = ("esp32s3", "pico2w")
RAM_BINS = ("ram-baseline", "ram-threads", "ram-std-inbox", "ram-crossbeam-select")
DIAGNOSTIC_CASES = (
    "paced-p4-b16-q8", "stream-p4-b16-q8",
    "stream-p4-b64-q8", "backpressure-p4-b16-q8",
)
OWNER_CASES = (
    "std-blocked-delivery", "std-full-progress", "std-disconnect",
    "owner-independent-inputs", "owner-full-stop", "owner-fairness-deadline",
    "owner-failure-recovery",
)
NATIVE_CASES = ("nuttx-c-three", "nuttx-rust-three", "zephyr-c-three", "embassy-three")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field: " + key)
        result[key] = value
    return result


def read(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def location(value):
    require(isinstance(value, str) and bool(value), "missing artifact location")
    return (ROOT / value).resolve()


def write_new(path, value):
    with Path(path).open("x") as output:
        output.write(json.dumps(value, indent=2) + "\n")


def command_json(script, arguments, output_file=False):
    # Separate processes keep the fixture-local build/report module names apart.
    with tempfile.TemporaryDirectory(prefix="rtos-characterization-") as temporary:
        output = Path(temporary) / "result.json"
        command = [sys.executable, str(script), *map(str, arguments)]
        if output_file:
            command += ["--out", str(output)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=600)
        require(result.returncode == 0,
                f"{script.parent.name}/{script.name} rejected evidence:\n{result.stderr[-6000:]}\n{result.stdout[-1500:]}")
        return read(output) if output_file else json.loads(result.stdout, object_pairs_hook=unique)


def observers(board):
    return ("primary", "cpu", "irq") if board == "esp32s3" else ("primary",)


def expected_builds(boards):
    keys = [f"{board}/{observer}/{arm}" for board in boards
            for observer in observers(board) for arm in ARMS]
    keys += [f"{board}/owner" for board in boards]
    if "esp32s3" in boards:
        keys += ["ordinary-ram/" + binary for binary in RAM_BINS]
    return keys


def template(boards):
    return {"schema": "rtos-characterization-inputs-v1", "boards": list(boards),
            "passes": 3,
            "builds": {key: "target/campaign-ready/" +
                       ("owner/" + key.split("/")[0] if key.endswith("/owner") else
                        key if key.startswith("ordinary-ram/") else "mpsc/" + key)
                       for key in expected_builds(boards)},
            "readelf": "/absolute/path/to/xtensa-esp32s3-elf-readelf" if "esp32s3" in boards else None,
            "native_ram": "target/campaign-ready/native-ram" if "esp32s3" in boards else None}


def validate_manifest(value):
    require(value.get("schema") == "rtos-characterization-inputs-v1", "wrong input schema")
    boards = value.get("boards")
    require(isinstance(boards, list) and boards and len(set(boards)) == len(boards)
            and all(board in BOARDS for board in boards), "select distinct supported boards")
    require(type(value.get("passes")) is int and value["passes"] == 3,
            "the measured campaign requires three separate passes")
    require(isinstance(value.get("builds"), dict)
            and set(value["builds"]) == set(expected_builds(boards)),
            "missing, extra, or relabeled campaign build")
    for path in value["builds"].values():
        location(path)
    require((value.get("readelf") is not None) == ("esp32s3" in boards)
            and (value.get("native_ram") is not None) == ("esp32s3" in boards),
            "S3 RAM evidence and readelf must be selected together")


def run_list(value, mpsc_plans):
    result = []
    for board in value["boards"]:
        # Correctness witnesses precede the long performance campaign.
        result += [dict(id=f"{board}.owner.{case}", family="owner", board=board,
                        build=f"{board}/owner", case=case, fresh_boot=True)
                   for case in OWNER_CASES]
        for observer in observers(board):
            for row in mpsc_plans[observer]:
                if observer != "primary" and row["case"] not in DIAGNOSTIC_CASES:
                    continue
                arm = row["runtime"] + "-" + row["backend"]
                result.append(dict(id=f"{board}.{observer}.{arm}.{row['case']}.p{row['pass_number']}",
                                   family="mpsc", board=board, build=f"{board}/{observer}/{arm}",
                                   fresh_boot=True, **row))
    if "esp32s3" in value["boards"]:
        arms = [("ram-baseline", "baseline", 0)]
        arms += [(binary, case, owners) for binary, cases in (
            ("ram-threads", ("threads",)), ("ram-std-inbox", ("queues", "services")),
            ("ram-crossbeam-select", ("queues", "services")))
            for case in cases for owners in (0, 1, 5, 10, 20)]
        for number in range(1, value["passes"] + 1):
            # Rotate implementation groups while retaining each arm's increasing counts.
            binaries = list(RAM_BINS)
            offset = (number - 1) % len(binaries)
            for binary in binaries[offset:] + binaries[:offset]:
                for selected, case, owners in arms:
                    if selected != binary:
                        continue
                    result.append(dict(id=f"esp32s3.ramstd.{binary}.{case}.n{owners}.p{number}",
                                       family="ordinary-ram", board="esp32s3", build="ordinary-ram/" + binary,
                                       binary=binary, case=case, owners=owners,
                                       stack_bytes=0 if case == "baseline" else 4096,
                                       pass_number=number, fresh_boot=True))
            # --runs repeats profiles within a boot. Separate matrices supply fresh boots.
            result.append(dict(id=f"esp32s3.ramnative.p{number}", family="native-ram",
                               board="esp32s3", pass_number=number, runs_per_image=1,
                               cases=list(NATIVE_CASES), fresh_boot=True, boots=4))
    return result


def preflight(value):
    validate_manifest(value)
    builds, checks = {}, {}
    for key, path in value["builds"].items():
        root = location(path)
        if key.endswith("/owner"):
            record = command_json(OWNER, ["inspect", "--build", root])
            board = key.split("/")[0]
            expected = "esp32s3-ram-footprint" if board == "esp32s3" else "pico2w-std-qualification"
            require(record["domain"] == "nuttx-target" and record["profile"] == expected
                    and record["cases"] == list(OWNER_CASES), "wrong owner build/case inventory")
            builds[key] = record
        elif key.startswith("ordinary-ram/"):
            builds[key] = command_json(RAM / "target_evidence.py", [
                "--build", root, "--binary", key.split("/")[1],
                "--readelf", location(value["readelf"])], True)["build_identity"]
        else:
            board, observer, arm = key.split("/")
            runtime, backend = arm.split("-")
            record = command_json(MPSC, ["inspect", "--build-directory", root])
            require(all(record[field] == expected for field, expected in
                        (("board", board), ("observer", observer), ("runtime", runtime), ("backend", backend))),
                    "mislabeled MPSC image")
            builds[key] = {field: record[field] for field in
                           ("build_id", "board", "observer", "runtime", "backend", "image_file", "elf_file")}
            builds[key]["artifact_sha256"] = record["artifacts_sha256"]
            builds[key]["receipt_sha256"] = digest(root / "build.json")
    for board in value["boards"]:
        for observer in observers(board):
            prefix = f"{board}/{observer}/"
            checks[prefix] = command_json(MPSC, [
                "check-cohort", "--nuttx-build", location(value["builds"][prefix + "nuttx-std"]),
                "--nuttx-native-build", location(value["builds"][prefix + "nuttx-native"]),
                "--zephyr-build", location(value["builds"][prefix + "zephyr-native"])])
    if "esp32s3" in value["boards"]:
        ordinary = [builds["ordinary-ram/" + binary] for binary in RAM_BINS]
        for field in ("source_commit", "profile", "optimization_policy"):
            require(all(row[field] == ordinary[0][field] for row in ordinary),
                    "ordinary RAM builds have different source/profile/optimization")
        checks["native-ram"] = command_json(NATIVE, ["--preflight", "--artifacts", location(value["native_ram"]),
                                                    "--readelf", location(value["readelf"])], True)
        native = checks["native-ram"]["cases"]
        require([row["case"] for row in native] == list(NATIVE_CASES), "wrong native RAM matrix")
        require(native[0]["kernel_config_identity"] == native[1]["kernel_config_identity"],
                "native C/Rust RAM kernel configuration differs")
    plans = {observer: command_json(MPSC, ["plan", "--passes", 3, "--include-native-control", "--observer", observer])
             for observer in set(o for b in value["boards"] for o in observers(b))}
    runs = run_list(value, plans)
    report = dict(schema="rtos-characterization-ready-v1", inputs=value, builds=builds, checks=checks,
                  runs=runs, run_count=len(runs), fresh_boot_count=sum(row.get("boots", 1) for row in runs),
                  hardware_runs_complete=False, physical_timing_qualified=False,
                  matched_placement_qualified=False, production_qualified=False,
                  unavailable={"pico2w": ["cpu-accounting", "interrupt-alarm-observer"],
                               "all": ["energy", "external-clock-calibration", "real-sensor-integration",
                                       "hardware-isolation", "arbitrary-std-main-reentry"]})
    report["campaign_id"] = identity(report)
    return report


def validate_ready(ready):
    expected = dict(ready)
    campaign_id = expected.pop("campaign_id", None)
    require(campaign_id == identity(expected), "changed readiness plan")
    require(preflight(ready["inputs"]) == ready, "build/source/plan changed since preflight")


def evidence_census(ready, ledger):
    require(ledger.get("schema") == "rtos-characterization-ledger-v1"
            and ledger.get("campaign_id") == ready["campaign_id"], "wrong campaign ledger")
    rows = ledger.get("runs")
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "missing run ledger")
    require(Counter(row.get("id") for row in rows) == Counter(row["id"] for row in ready["runs"]),
            "missing, duplicate, or extra run evidence")
    paths = [location(row["evidence"]) for row in rows]
    require(len(set(paths)) == len(paths), "one capture reused for multiple runs")
    return {row["id"]: row for row in rows}


def session_scope(session, ready, run):
    require(session.get("campaign_id") == ready["campaign_id"]
            and session.get("run_id") == run["id"],
            "session belongs to a different campaign or run")


def replay(ready, ledger):
    validate_ready(ready)
    rows = evidence_census(ready, ledger)
    reports, groups = {}, defaultdict(list)
    value = ready["inputs"]
    for run in ready["runs"]:
        entry = rows[run["id"]]
        path = location(entry["evidence"])
        build = location(value["builds"][run["build"]]) if "build" in run else None
        if run["family"] == "mpsc":
            session_scope(read(path / "session.json"), ready, run)
            report = command_json(MPSC, ["replay", "--build-directory", build, "--output", path])
            for key in ("board", "observer", "runtime", "backend", "case", "pass_number"):
                require(report[key] == run[key], "MPSC evidence assigned to the wrong run")
            require(report["build_id"] == ready["builds"][run["build"]]["build_id"], "wrong MPSC build")
            groups[run["board"], run["observer"]].append(path)
        elif run["family"] == "owner":
            session_scope(read(path / "session.json"), ready, run)
            report = command_json(OWNER, ["replay", path])
            require(report["domain"] == "nuttx-target" and report["case"] == run["case"]
                    and report["build_id"] == ready["builds"][run["build"]]["build_id"], "wrong owner capture")
            require(read(path / "capture.json")["runtime"] == "physical-operator-asserted", "host owner capture")
        elif run["family"] == "ordinary-ram":
            session = read(location(entry["session"]))
            image_sha = ready["builds"][run["build"]]["artifact_sha256"]["nuttx/nuttx.merged.bin"]
            require(all(session.get(key) == expected for key, expected in {
                "schema": "ordinary-ram-session-v1", "run_id": run["id"], "campaign_id": ready["campaign_id"],
                "image_sha256": image_sha, "console_sha256": digest(path),
                "fresh_boot": True, "flash_verified": True, "fresh_backup_verified": True}.items()),
                "ordinary RAM session/image/console mismatch")
            console = path.read_text().replace("\x1b[K", "").replace("\x1b[2K", "")
            boot, _, rest = console.partition("nsh> if ramprobe")
            require(rest and fresh_shell_boot(boot) and not BOOT_MARKERS.search(rest),
                    "retain exactly one fresh NSH boot before the RAM command")
            report = command_json(RAM / "report.py", [
                "--input", path, "--domain", "nuttx-target", "--build", build,
                "--readelf", location(value["readelf"]), "--binary", run["binary"],
                "--case", run["case"], "--owners", run["owners"], "--stack-bytes", run["stack_bytes"]], True)
        else:
            session = read(location(entry["session"]))
            session_scope(session, ready, run)
            require(session.get("schema") == "native-ram-session-v1"
                    and session.get("matrix_sha256") == digest(path)
                    and session.get("fresh_boots") is True,
                    "native RAM fresh-boot session/record mismatch")
            report = command_json(NATIVE, ["--replay", path, "--artifacts", location(value["native_ram"]),
                                          "--readelf", location(value["readelf"]), "--runs", 1], True)
        reports[run["id"]] = report
    comparisons = {}
    for (board, observer), paths in groups.items():
        prefix = f"{board}/{observer}/"
        comparisons[prefix] = command_json(MPSC, ["compare",
            "--nuttx-build", location(value["builds"][prefix + "nuttx-std"]),
            "--nuttx-native-build", location(value["builds"][prefix + "nuttx-native"]),
            "--zephyr-build", location(value["builds"][prefix + "zephyr-native"]), *paths])
    restoration = ledger.get("restoration", {})
    require(set(restoration) == set(value["boards"])
            and all(row.get("verified") is True and isinstance(row.get("evidence"), str)
                    and row["evidence"] for row in restoration.values()), "missing board restoration attestation")
    return dict(schema="rtos-characterization-replay-v1", campaign_id=ready["campaign_id"],
                run_count=len(reports), complete_evidence_census=True, samples_pooled=False,
                physical_execution_basis="operator-attested image/boot plus validated firmware transcripts",
                physical_timing_qualified=False, matched_placement_qualified=False, production_qualified=False,
                reports=reports, comparisons=comparisons, restoration=restoration)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    sample = commands.add_parser("template")
    sample.add_argument("--board", choices=BOARDS, action="append")
    sample.add_argument("--out", type=Path, required=True)
    prepare = commands.add_parser("preflight")
    prepare.add_argument("--inputs", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    check = commands.add_parser("replay")
    check.add_argument("--ready", type=Path, required=True)
    check.add_argument("--ledger", type=Path, required=True)
    check.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    require(not args.out.exists(), "output must be new")
    if args.operation == "template":
        result = template(args.board or BOARDS)
        validate_manifest(result)
    elif args.operation == "preflight":
        result = preflight(read(args.inputs))
    else:
        result = replay(read(args.ready), read(args.ledger))
    write_new(args.out, result)
    print(json.dumps({"operation": args.operation, "output": str(args.out),
                      **{key: result[key] for key in ("campaign_id", "run_count", "fresh_boot_count") if key in result}}))


if __name__ == "__main__":
    main()
