#!/usr/bin/env python3
"""Replay the complete 19-case ARM/QEMU P1 artifact against its Git sources.

A complete diagnostic can retain demonstrated progress failures. No-repair
acceptance is requested for named families, never inferred for all P1 work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import analyze as evidence
import cohort
import fifo_cohort
import std_cohort

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCHEMA = "channel-progress-replay-v1"
INVENTORY_POLICY = "mps2-channel-progress-inputs-v1"
FAMILIES = {
    "crossbeam": ("mps2", cohort),
    "std": ("std-transfer/mps2/cases", std_cohort),
    "fifo": ("fifo/mps2/cases", fifo_cohort),
}
QUALIFICATION_FLAGS = std_cohort.QUALIFICATION_FLAGS
# This bounded inventory mirrors build.inventory for the non-inheriting MPS2
# mock profile. Derive recursive members from Git, not the receipt's assertions.
# A parity test makes a changed build recipe require an explicit policy update.
FIXED_INPUTS = (
    "Cargo.toml", "Cargo.lock", "hal/gnss/Cargo.toml", "hal/imu/Cargo.toml",
    "platform/nuttx/platforms/mps2-an521-mock.toml",
    "tests/std-channel-perf/native.c", "tests/std-channel-perf/native.h",
    "tests/std-channel-perf/rust/src/native.rs", "tests/std-channel-perf/build.py",
    "tests/std-channel-perf/prepare-std.py", "tools/build-nuttx-std-app.sh",
    "tools/platform_config.py", "tools/check-deployment.py", "tools/apply-nuttx-patches.py",
    "tools/nuttx-version.py", "platform/nuttx/init.py", "platform/nuttx/mcuboot.py",
    "tests/nuttx-std/prepare-std.py", "tests/nuttx-std/check-abi.py", "tests/nuttx-std/link.py",
)
INPUT_DIRECTORIES = (
    "platform/nuttx/std-app", "upstream/nuttx", "upstream/nuttx-apps",
    "upstream/rust-std", "upstream/libc",
)
VERIFIER_FILES = (
    "analyze.py", "capture.py", "cohort.py", "fifo_analyze.py", "fifo_cohort.py",
    "fifo_sources.py", "mutex_analyze.py", "prepare-crossbeam.py", "std_cohort.py", "replay.py",
)


def require(condition, message):
    evidence.require(condition, message)


def git(repository, *arguments, data=None):
    result = subprocess.run(["git", "-C", str(repository), *arguments], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, "Git source verification failed: " +
            result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def expected_source_paths(paths):
    """Select all inputs for the supported MPS2 recipe from a Git tree listing."""
    paths = set(paths)
    selected = set(FIXED_INPUTS)
    selected.update(name for name in paths if any(name.startswith(prefix + "/")
                                                for prefix in INPUT_DIRECTORIES))
    selected.update(name for name in paths if name.startswith("tests/channel-progress/")
                    and Path(name).suffix in (".rs", ".toml", ".py")
                    and "__pycache__" not in Path(name).parts)
    require(selected <= paths, "Git revision lacks required MPS2 build inputs: " +
            ", ".join(sorted(selected - paths)))
    return sorted(selected)


def git_source_inventory(repository, revision):
    require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
            "source revision must be a full Git commit SHA")
    require(git(repository, "rev-parse", revision + "^{commit}").decode().strip() == revision,
            "source revision is not its named Git commit")
    entries = {}
    for row in git(repository, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if row:
            header, raw_name = row.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            entries[raw_name.decode("utf-8")] = (mode, kind, oid)
    names = expected_source_paths(entries)
    for name in names:
        mode, kind, _ = entries[name]
        require(kind == "blob" and mode in ("100644", "100755"),
                "source inventory requires regular Git blobs: " + name)
    # One batch avoids launching a separate Git process for every source file.
    objects = git(repository, "cat-file", "--batch", data=(
        "\n".join(entries[name][2] for name in names) + "\n").encode("ascii"))
    inventory, offset, profile = {}, 0, None
    for name in names:
        end = objects.index(b"\n", offset)
        oid, kind, length = objects[offset:end].decode("ascii").split()
        require(oid == entries[name][2] and kind == "blob", "unexpected Git source object")
        length = int(length)
        data = objects[end + 1:end + 1 + length]
        require(len(data) == length and objects[end + 1 + length:end + 2 + length] == b"\n",
                "truncated Git source object")
        offset = end + 2 + length
        inventory[name] = hashlib.sha256(data).hexdigest()
        if name == "platform/nuttx/platforms/mps2-an521-mock.toml":
            profile = tomllib.loads(data.decode("utf-8"))
    require(offset == len(objects), "unexpected trailing Git source objects")
    require(profile is not None and "extends" not in profile
            and profile.get("target") == "thumbv8m.main-nuttx-eabi"
            and profile.get("board") == "mps2-an521:nsh"
            and profile.get("hal") == {"imu": "mock", "gnss": "mock"},
            "source inventory policy only supports the non-inheriting MPS2 mock profile")
    return inventory


def verify_sources(repository, receipt, input_root):
    """Bind the complete recorded inventory to regular blobs at its Git commit.

    input_root is the retained run directory containing this exact build.json.
    The current verifier's source identity is reported separately from firmware.
    """
    require(not (Path(input_root) / "build.json").is_symlink(), "linked retained build receipt")
    saved = evidence.checked_file(Path(input_root), "build.json")
    require(evidence.canonical(evidence.read_json(saved)) == evidence.canonical(receipt),
            "source verification requires the retained build receipt")
    require(receipt.get("platform") == "mps2-an521-mock"
            and receipt.get("target") == "thumbv8m.main-nuttx-eabi",
            "complete P1 replay only supports the ARM MPS2 artifact")
    revision = receipt["source_revision"]
    inventory = git_source_inventory(repository, revision)
    recorded = receipt.get("source_sha256")
    require(isinstance(recorded, dict) and set(recorded) == set(inventory),
            "recorded source inventory differs from the complete Git build input set")
    differing = [name for name in inventory if inventory[name] != recorded[name]]
    require(not differing, "source bytes differ from Git revision: " + ", ".join(differing))
    return {
        "source_revision": revision,
        "source_tree": git(repository, "rev-parse", revision + "^{tree}").decode().strip(),
        "source_input_count": len(inventory), "source_sha256": inventory,
        "inventory_policy": INVENTORY_POLICY, "inventory_sha256": evidence.canonical(inventory),
        "historical_std_report_optional": "tests/channel-progress/std_cohort.py" not in inventory,
    }


def regular_run(root):
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            require(not path.is_symlink(), "linked evidence is not a regular retained capture: " + str(path))
            require(path.is_dir() or path.is_file(), "non-regular retained evidence: " + str(path))


def stored_report(root, relative, expected, *, optional=False):
    path = root / relative
    if optional and not path.exists() and not path.is_symlink():
        return {"path": relative, "status": "not-recorded-by-historical-source"}
    require(not path.is_symlink(), "linked stored cohort report: " + relative)
    path = evidence.checked_file(root, relative)
    require(evidence.canonical(evidence.read_json(path)) == evidence.canonical(expected),
            "stored cohort report differs from replay: " + relative)
    return {"path": relative, "status": "matched", "sha256": evidence.sha256(path)}


def verifier_identity():
    files = {name: evidence.sha256(HERE / name) for name in VERIFIER_FILES}
    # A verifier can also be copied outside a checkout for offline use. Its exact
    # module hashes remain available even when no enclosing Git revision exists.
    try:
        revision = git(ROOT, "rev-parse", "HEAD").decode().strip()
        dirty = bool(git(ROOT, "status", "--porcelain", "--untracked-files=all",
                         "--", "tests/channel-progress"))
    except (ValueError, OSError):
        revision, dirty = None, None
    return {"source_revision": revision, "working_tree_dirty": dirty,
            "files_sha256": files, "files_identity_sha256": evidence.canonical(files)}


def analyze_replay(root, repository):
    root = Path(root)
    require(not root.is_symlink(), "linked complete artifact root")
    root = root.resolve(strict=True)
    require(root.is_dir(), "complete artifact root is not a directory")
    expected = {f"{relative}/{mode}/run/capture.json"
                for relative, module in FAMILIES.values() for mode in module.MODES}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("capture.json")}
    require(actual == expected, "complete P1 replay requires exactly 19 named captures; missing=" +
            str(sorted(expected - actual)) + ", unexpected=" + str(sorted(actual - expected)))
    reports, receipts, common, environments = {}, {}, None, []
    first_run, first_receipt = None, None
    for name, (relative, module) in FAMILIES.items():
        family_root = root
        for part in Path(relative).parts:
            family_root = cohort.directory(family_root, part)
        for mode in module.MODES:
            run = cohort.directory(cohort.directory(family_root, mode), "run")
            regular_run(run)
        report = module.analyze_cohort(family_root)
        require(report["runtime_kind"] == "qemu"
                and report["execution_evidence"] == "virtual",
                "complete P1 replay requires ARM/QEMU virtual execution evidence")
        identity = {field: report["matched_identity"][field] for field in cohort.IDENTITY_FIELDS}
        if common is None:
            common = identity
        require(evidence.canonical(identity) == evidence.canonical(common),
                "mixed complete P1 source/compiler/configuration identity at " + name)
        environment = report.get("execution_environment") or {
            field: report["matched_identity"][field]
            for field in ("runtime_kind", "execution_evidence", "board")
        }
        environments.append(environment)
        require(evidence.canonical(environment) == evidence.canonical(environments[0]),
                "mixed complete P1 execution environment at " + name)
        for mode in module.MODES:
            run = family_root / mode / "run"
            receipt = evidence.read_json(run / "build.json")
            summary = evidence.read_json(run / "analysis.json")
            for record in (summary, summary["metadata"], summary["result"], summary["session"],
                           evidence.read_json(run / "capture.json")):
                for field in QUALIFICATION_FLAGS + ("physical_measurement", "production_qualified"):
                    if field in record:
                        require(evidence.boolean(record, field) is False,
                                "unsupported complete P1 qualification: " + field)
            if first_receipt is None:
                first_run, first_receipt = run, receipt
            build_id = receipt["build_id"]
            if build_id in receipts:
                require(evidence.canonical(receipts[build_id]) == evidence.canonical(receipt),
                        "one build ID names different complete receipts")
            receipts[build_id] = receipt
            if name == "fifo":
                acceptance = evidence.checked_file(run, "acceptance.json")
                bound = evidence.bind_build(evidence.analyze((run / "console.log").read_text()), receipt)
                evidence.require_no_repair(bound)
                require(evidence.canonical(evidence.read_json(acceptance)) == evidence.canonical(bound),
                        "stored FIFO acceptance differs from independently replayed evidence")
        try:
            module.require_no_repair(report)
            no_repair = True
        except ValueError:
            no_repair = False
        reports[name] = {"directory": relative, "case_count": len(module.MODES),
                         "no_repair_satisfied": no_repair, "report": report}
    require(len(receipts) == 8, "complete P1 replay requires exactly eight distinct firmware builds")
    sources = verify_sources(repository, first_receipt, first_run)
    saved = [stored_report(root, relative, reports[family]["report"], optional=optional)
             for relative, family, optional in (
                 ("mps2/cohort.json", "crossbeam", False),
                 ("mps2/acceptance.json", "crossbeam", False),
                 ("std-transfer/mps2/cohort.json", "std", sources["historical_std_report_optional"]),
                 ("fifo/mps2/cohort.json", "fifo", False),
             )]
    return {
        "schema": SCHEMA, "scope": "complete-p1-arm-qemu-diagnostic",
        "diagnostic_valid": True, "cohort_complete": True, "case_count": 19, "build_count": 8,
        "families": reports, "matched_identity": common,
        "matched_identity_sha256": evidence.canonical(common),
        "build_receipts_sha256": {key: evidence.canonical(value) for key, value in sorted(receipts.items())},
        "source_verification": sources, "verifier": verifier_identity(), "stored_reports": saved,
        "execution_environment": environments[0], "runtime_kind": "qemu",
        "execution_evidence": "virtual", **dict.fromkeys(QUALIFICATION_FLAGS, False),
        "scope_limits": (
            "Complete replay of six unequal-priority Crossbeam diagnostics, seven prewarmed std "
            "primitive cases and six equal-FIFO reservation cases. Acceptance is per named family; "
            "a complete diagnostic does not erase the original progress failures or qualify a full "
            "owner protocol, production transport, physical execution, latency or a hard time bound."
        ),
    }


def require_no_repair(report, families):
    require(report.get("schema") == SCHEMA and report.get("diagnostic_valid") is True
            and report.get("cohort_complete") is True and report.get("case_count") == 19
            and report.get("build_count") == 8, "no-repair gate requires a complete valid P1 replay")
    require(bool(families), "no-repair gate requires named families")
    for family in families:
        require(family in FAMILIES, "unknown P1 no-repair family: " + str(family))
        result = report["families"][family]
        require(result["no_repair_satisfied"] is True, "P1 no-repair gate refused for " + family)
        FAMILIES[family][1].require_no_repair(result["report"])


def require_physical(report):
    require(report.get("physical_execution_verified") is True
            and report.get("physical_progress_qualified") is True,
            "physical gate refused: ARM/QEMU replay is not verified device/flash/ownership evidence")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-no-repair", nargs="+", choices=tuple(FAMILIES))
    parser.add_argument("--require-physical", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = analyze_replay(args.artifact_directory, args.repository)
    except (ValueError, OSError, KeyError) as error:
        print(f"complete P1 replay rejected: {error}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    try:
        if args.require_no_repair:
            require_no_repair(report, args.require_no_repair)
        if args.require_physical:
            require_physical(report)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
