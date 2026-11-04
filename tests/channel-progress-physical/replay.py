#!/usr/bin/env python3
"""Replay the fixed 19-case ESP campaign without authenticating physical labels.

Diagnostic validity, observed clean boots and named no-repair acceptance are
separate results. This offline command never acquires, flashes or resets a board.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import campaign

evidence = campaign.evidence
cohort = campaign.cohort
std_cohort = campaign.std_cohort
fifo_cohort = campaign.fifo_cohort
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCHEMA = "channel-progress-physical-replay-v1"
FAMILIES = {
    "crossbeam": ("runs/crossbeam", cohort),
    "std": ("runs/std", std_cohort),
    "fifo": ("runs/fifo", fifo_cohort),
}
QUALIFICATION_FLAGS = std_cohort.QUALIFICATION_FLAGS
BROAD_CLAIMS = QUALIFICATION_FLAGS + ("physical_measurement", "production_qualified")
ROM_BANNER = "ESP-ROM:esp32s3-20210327"
VERIFIER_FILES = (
    "analyze.py", "capture.py", "cohort.py", "fifo_analyze.py", "fifo_cohort.py",
    "fifo_sources.py", "mutex_analyze.py", "prepare-crossbeam.py", "std_cohort.py",
    "boot_image.py",
)
VERIFIER_DEPENDENCIES = (
    "tests/std-channel-perf/simpleboot_digest.py",
    "tests/std-channel-perf/inspect_esp_image.py", "tools/apply-nuttx-patches.py",
    "upstream/nuttx/patches/0107-xtensa-simpleboot-hash-offset.patch",
)


def require(condition, message):
    evidence.require(condition, message)


def regular_evidence(root):
    """Reject aliases and devices before parsing or hashing retained evidence."""
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            require(not path.is_symlink(), "linked campaign evidence: " + str(path))
            require(path.is_dir() or path.is_file(), "non-regular campaign evidence: " + str(path))


def saved_report(root, relative, expected):
    path = evidence.checked_file(root, relative)
    require(evidence.canonical(evidence.read_json(path)) == evidence.canonical(expected),
            "stored campaign report differs from replay: " + relative)
    return {"path": relative, "sha256": evidence.sha256(path), "status": "matched"}


def check_claims(record, location):
    require(isinstance(record, dict), "invalid campaign record: " + location)
    for name in BROAD_CLAIMS:
        if name in record:
            require(evidence.boolean(record, name) is False,
                    "unsupported campaign qualification in " + location + ": " + name)


def boot_observation(raw, mode):
    """Require the full ROM prefix before claiming an observed warning-free boot.

    A missing ROM prefix does not erase an otherwise valid NuttX diagnostic. It
    does prevent absence of a warning in the retained text from counting as a
    complete ROM-boot observation.
    """
    structure = cohort.capture_structure(raw, mode)
    text = cohort.ANSI.sub("", raw.decode("utf-8", errors="strict").replace("\r", ""))
    command = re.search(r"(?m)^nsh>[ \t]*channel_progress " + re.escape(mode) + r"[ \t]*$", text)
    require(command is not None, "missing campaign command after capture validation")
    prefix = text[:command.start()]
    rom_lines = list(re.finditer(r"(?m)^ESP-ROM:[^\n]*$", prefix))
    require(len(re.findall(r"(?m)^ESP-ROM:[^\n]*$", text)) <= 1,
            "campaign capture contains multiple ESP ROM boots")
    boot_positions = [prefix.index(banner) for banner in cohort.BOOT_BANNERS if banner in prefix]
    complete = (len(rom_lines) == 1 and rom_lines[0].group() == ROM_BANNER and bool(boot_positions)
                and rom_lines[0].start() < min(boot_positions))
    warning = structure["boot_rom_digest_warning_observed"]
    # Preserve the existing capture flag's exact meaning, while refusing a
    # clean-boot claim if a digest-failure marker is retained anywhere else.
    warning_anywhere = "SHA-256 comparison failed:" in text
    return {
        "rom_boot_prefix_observed": complete,
        "boot_rom_digest_warning_observed": warning,
        "digest_warning_anywhere_in_capture": warning_anywhere,
        "clean_boot_observed": complete and not warning_anywhere,
    }


def verifier_identity():
    paths = {"tests/channel-progress-physical/" + name: HERE / name
             for name in ("campaign.py", "replay.py")}
    paths.update({"tests/channel-progress/" + name: HERE.parent / "channel-progress" / name
                  for name in VERIFIER_FILES})
    paths.update({name: ROOT / name for name in VERIFIER_DEPENDENCIES})
    hashes = {name: evidence.sha256(path) for name, path in paths.items()}
    try:
        revision = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
        status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                                 "--untracked-files=all", "--", *paths], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        dirty = bool(status)
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    return {"source_revision": revision, "working_tree_dirty": dirty,
            "files_sha256": hashes, "files_identity_sha256": evidence.canonical(hashes)}


def analyze_replay(root, repository=ROOT, manifest_path=None):
    manifest_path = Path(manifest_path) if manifest_path is not None else campaign.DEFAULT_MANIFEST
    manifest = campaign.load_manifest(manifest_path)
    root = Path(root)
    require(not root.is_symlink(), "linked campaign root")
    root = root.resolve(strict=True)
    require(root.is_dir(), "campaign root is not a directory")
    regular_evidence(root)
    expected = {row["run_path"] + "/capture.json" for row in campaign.CASES}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("capture.json")}
    require(actual == expected,
            "complete physical campaign requires exactly 19 named captures; missing=" +
            str(sorted(expected - actual)) + ", unexpected=" + str(sorted(actual - expected)))
    expected_builds = {row["mode"]: row for row in manifest["builds"]}
    reports, receipts, observations, saved = {}, {}, [], []
    common, environment, sources = None, None, None
    sessions, declared_campaigns = set(), []
    for family, (relative, module) in FAMILIES.items():
        family_root = root
        for part in Path(relative).parts:
            family_root = cohort.directory(family_root, part)
        report = module.analyze_cohort(family_root)
        require(report["runtime_kind"] == "physical"
                and report["execution_evidence"] == "operator-asserted",
                "campaign requires operator-declared physical sessions")
        matched = report["matched_identity"]
        identity = {field: matched[field] for field in cohort.common_identity_fields(matched)}
        if common is None:
            common = identity
        require(evidence.canonical(identity) == evidence.canonical(common),
                "mixed campaign source/compiler/configuration/profile identity at " + family)
        execution = report.get("execution_environment") or {
            field: matched[field] for field in
            ("runtime_kind", "execution_evidence", "board", "board_serial", "active_cores")
        }
        if environment is None:
            environment = execution
        require(evidence.canonical(execution) == evidence.canonical(environment),
                "mixed campaign board/session environment at " + family)
        for row in (row for row in campaign.CASES if row["family"] == family):
            mode = row["mode"]
            run = cohort.directory(cohort.directory(family_root, mode), "run")
            receipt = evidence.read_json(evidence.checked_file(run, "build.json"))
            expected_build = expected_builds[row["build_mode"]]
            require(evidence.sha256(run / "build.json") == expected_build["build_receipt_sha256"],
                    "retained campaign receipt differs from frozen manifest: " + mode)
            require(receipt.get("mode") == row["build_mode"], "campaign uses a different build mode: " + mode)
            if row["build_mode"] not in receipts:
                receipt = campaign.verify_build(run, expected_build)
                receipts[row["build_mode"]] = receipt
                if sources is None:
                    sources = campaign.verify_sources(repository, receipt)
            else:
                require(evidence.canonical(receipt) == evidence.canonical(receipts[row["build_mode"]]),
                        "one campaign build names different retained receipts")
            raw = evidence.checked_file(run, "console.log").read_bytes()
            bound = evidence.bind_build(evidence.analyze(raw.decode("utf-8", errors="strict")), receipt)
            session = evidence.read_json(evidence.checked_file(run, "session.json"))
            attached = evidence.attach_execution(bound, session, fresh_boot_observed=True)
            # The legacy Crossbeam analyzer tolerates its historical overclaim;
            # this new campaign admits only the exact current replayed analysis.
            saved_analysis = evidence.read_json(evidence.checked_file(run, "analysis.json"))
            require(evidence.canonical(saved_analysis) == evidence.canonical(attached),
                    "stored campaign analysis differs from replay: " + mode)
            acceptance = evidence.checked_file(run, "acceptance.json")
            require(evidence.canonical(evidence.read_json(acceptance)) == evidence.canonical(bound),
                    "stored campaign acceptance differs from replay: " + mode)
            capture = evidence.read_json(evidence.checked_file(run, "capture.json"))
            for location, record in (("analysis", attached), ("metadata", attached["metadata"]),
                                     ("result", attached["result"]), ("session", session),
                                     ("capture", capture), ("acceptance", bound)):
                check_claims(record, mode + "/" + location)
            measurement = session["measurement_session"]
            require(measurement not in sessions, "duplicate campaign measurement session: " + measurement)
            sessions.add(measurement)
            declared_campaigns.append(session.get("campaign_id"))
            boot = boot_observation(raw, mode)
            try:
                evidence.require_no_repair(bound)
                no_repair_exit = 0
            except ValueError:
                no_repair_exit = 2
            observations.append({"family": family, "mode": mode, "directory": row["run_path"],
                                 "measurement_session": measurement,
                                 "build_id": receipt["build_id"], "image_sha256": receipt["image_sha256"],
                                 "acceptance_sha256": evidence.sha256(acceptance),
                                 "no_repair_exit": no_repair_exit,
                                 "expected_no_repair_exit": row["expected_no_repair_exit"], **boot})
        try:
            module.require_no_repair(report)
            no_repair = True
        except ValueError:
            no_repair = False
        reports[family] = {"directory": relative, "case_count": len(module.MODES),
                            "no_repair_satisfied": no_repair, "report": report}
        saved.append(saved_report(root, family + "-cohort.json", report))
    require(set(receipts) == set(campaign.BUILDS) and len(receipts) == 8
            and len({receipt["build_id"] for receipt in receipts.values()}) == 8,
            "complete campaign requires eight distinct frozen builds")
    require(len(observations) == len(sessions) == 19, "campaign capture/session count differs")
    require(all(isinstance(value, str) and bool(value.strip()) for value in declared_campaigns)
            and len(set(declared_campaigns)) == 1,
            "mixed or missing operator-declared campaign ID")
    campaign_id = declared_campaigns[0]
    clean = all(row["clean_boot_observed"] for row in observations)
    return {
        "schema": SCHEMA, "scope": "complete-p1-esp32s3-diagnostic",
        "diagnostic_valid": True, "cohort_complete": True, "case_count": 19, "build_count": 8,
        "clean_boot_observed": clean,
        "all_cases_have_complete_rom_prefix": all(row["rom_boot_prefix_observed"] for row in observations),
        "boot_rom_digest_warning_count": sum(row["digest_warning_anywhere_in_capture"] for row in observations),
        "clean_boot_blocking_cases": [row["mode"] for row in observations if not row["clean_boot_observed"]],
        "campaign_manifest_sha256": evidence.sha256(manifest_path),
        "campaign_manifest_identity_sha256": evidence.canonical(manifest),
        "families": reports, "boot_observations": observations,
        "matched_identity": common, "matched_identity_sha256": evidence.canonical(common),
        "build_receipts_sha256": {mode: expected_builds[mode]["build_receipt_sha256"] for mode in campaign.BUILDS},
        "source_verification": sources, "verifier": verifier_identity(), "stored_reports": saved,
        "execution_environment": environment, "runtime_kind": "physical",
        "operator_campaign_id": campaign_id, "capture_order_verified": False,
        "execution_evidence": "operator-asserted", **dict.fromkeys(QUALIFICATION_FLAGS, False),
        "physical_evidence_scope": (
            "Consistent operator declarations and retained serial text do not independently verify "
            "device identity, flash/readback, exclusive ownership or restoration."
        ),
        "scope_limits": (
            "Nineteen source-bound diagnostics using the eight fixed ESP candidate images. Clean boot "
            "means the retained ROM prefix has no digest-failure warning. No-repair acceptance applies "
            "only to named families; original progress failures remain visible. This replay does not "
            "qualify a full owner protocol, production transport, physical execution, latency or time bounds."
        ),
    }


def require_complete(report):
    require(report.get("schema") == SCHEMA and report.get("diagnostic_valid") is True
            and report.get("cohort_complete") is True and type(report.get("case_count")) is int
            and report["case_count"] == 19 and type(report.get("build_count")) is int
            and report["build_count"] == 8, "campaign gate requires the complete valid 19-case replay")


def require_no_repair(report, families):
    require_complete(report)
    require(bool(families), "no-repair gate requires named families")
    for family in families:
        require(family in FAMILIES, "unknown campaign no-repair family: " + str(family))
        entry = report["families"][family]
        require(entry["no_repair_satisfied"] is True, "campaign no-repair gate refused for " + family)
        FAMILIES[family][1].require_no_repair(entry["report"])


def require_clean_boot(report):
    require_complete(report)
    observations = report.get("boot_observations")
    require(isinstance(observations, list) and len(observations) == 19
            and {row.get("mode") for row in observations} == {row["mode"] for row in campaign.CASES}
            and report.get("clean_boot_observed") is True
            and report.get("all_cases_have_complete_rom_prefix") is True
            and type(report.get("boot_rom_digest_warning_count")) is int
            and report["boot_rom_digest_warning_count"] == 0
            and report.get("clean_boot_blocking_cases") == []
            and all(row.get("rom_boot_prefix_observed") is True
                    and row.get("boot_rom_digest_warning_observed") is False
                    and row.get("digest_warning_anywhere_in_capture") is False
                    and row.get("clean_boot_observed") is True for row in observations),
            "clean-boot gate refused: all 19 retained ROM boot prefixes must be present without digest warnings")


def require_physical(report):
    require_complete(report)
    # This verifier has no independent device/flash/ownership evidence path.
    # Flipping flags in an otherwise well-shaped report cannot create one.
    require(False,
            "physical gate refused: operator session labels and serial text are not independently verified device/flash/ownership evidence")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_directory", type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=campaign.DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-no-repair", nargs="+", choices=tuple(FAMILIES))
    parser.add_argument("--require-clean-boot", action="store_true")
    parser.add_argument("--require-physical", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output:
            require(not args.output.exists() and not args.output.is_symlink(),
                    "refusing to overwrite retained evidence or an existing report")
        report = analyze_replay(args.campaign_directory, args.repository, args.manifest)
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x") as stream:
                stream.write(encoded)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("physical campaign replay rejected: " + str(error), file=sys.stderr)
        return 1
    print(encoded, end="")
    try:
        if args.require_no_repair:
            require_no_repair(report, args.require_no_repair)
        if args.require_clean_boot:
            require_clean_boot(report)
        if args.require_physical:
            require_physical(report)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
