#!/usr/bin/env python3
"""Offline preflight for the fixed PR255 ESP32-S3 eight-image campaign.

This host-only tool never discovers ports, opens devices, flashes or resets a
board. The checked-in plan selects already built firmware; the current verifier
has a separate source identity and supplies no physical authentication claim.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
_FRONTEND_SPEC = importlib.util.spec_from_file_location(
    "progress_frontend_inputs", ROOT / "tests/std-channel-perf/build_inputs.py")
frontend_inputs = importlib.util.module_from_spec(_FRONTEND_SPEC)
_FRONTEND_SPEC.loader.exec_module(frontend_inputs)
FIXTURE = HERE.parent / "channel-progress"
# Append, rather than prepend, so sibling replay.py remains the host campaign
# replayer when unittest discovers both this directory and the original fixture.
if str(FIXTURE) not in sys.path:
    sys.path.append(str(FIXTURE))
import analyze as evidence
import cohort
import fifo_cohort
import std_cohort

DEFAULT_MANIFEST = HERE / "candidate-manifest.json"
MANIFEST_SCHEMA = "channel-progress-physical-campaign-plan-v1"
SCHEMA = "channel-progress-physical-preflight-v1"
PINNED_MANIFEST_SHA256 = "0ec2f4149bbacf0e69237de121dbc16234cb603dcaab316d07b31998f5ce1400"
PINNED_ARTIFACT = 13323366472
PINNED_REVISION = "9c08fb5ff5ec9237f25340b643cc19b4b2394d01"
PINNED_HEAD = "499a4a6a18397410c1d5b12d716bd6a1b44bca56"
PINNED_TREE = "ec57055f6ca9cdd85f233ee6bc2d8560c2232ff2"
PLATFORM = "esp32s3-service-footprint"
TARGET = "xtensa-esp32s3-nuttx"
PROFILE = "ram-prefix-digest-v1"
INVENTORY_POLICY = "esp32s3-channel-progress-inputs-v1"
FRONTEND_INVENTORY_POLICY = "esp32s3-channel-progress-inputs-v2"
IMAGE_FILE = "firmware/nuttx/nuttx.rom-digest.bin"
ORIGINAL_IMAGE_FILE = "firmware/nuttx/nuttx.merged.bin"
FLASH_BYTES = 0x1000000
BUILDS = (*cohort.MODES, "std-mutex-suite", "fifo-channel-suite")
CASES = tuple(
    {"index": index, "family": family, "mode": mode,
     "build_mode": build_mode or mode, "run_path": f"runs/{family}/{mode}/run",
     "expected_no_repair_exit": 2 if mode in (
         "direct-reserved", "select-reserved", "send-reserved") else 0}
    for index, (family, mode, build_mode) in enumerate([
        *(("crossbeam", mode, None) for mode in cohort.MODES),
        *(("std", mode, "std-mutex-suite") for mode in evidence.STD_TRANSFER_MODES),
        *(("fifo", mode, "fifo-channel-suite") for mode in evidence.FIFO_MODES),
    ], 1)
)
QUALIFICATION_FLAGS = (
    "physical_execution_verified", "physical_progress_qualified",
    "priority_progress_qualified", "physical_timing_qualified", "timing_qualified",
    "hard_realtime_bound", "production_transport_qualified",
)
# Select this non-inheriting, empty-HAL ESP profile independently from Git. No
# receipt-supplied list or executable build module determines the source set.
# The parity test detects drift in the existing build.inventory recipe.
FIXED_INPUTS = (
    "Cargo.toml", "Cargo.lock",
    "platform/nuttx/platforms/esp32s3-service-footprint.toml",
    "tests/std-channel-perf/native.c", "tests/std-channel-perf/native.h",
    "tests/std-channel-perf/rust/src/native.rs", "tests/std-channel-perf/build.py",
    "tests/std-channel-perf/prepare-std.py", "tests/std-channel-perf/simpleboot_digest.py",
    "tests/std-channel-perf/inspect_esp_image.py", "tools/build-nuttx-std-app.sh",
    "tools/platform_config.py", "tools/check-deployment.py", "tools/apply-nuttx-patches.py",
    "tools/nuttx-version.py", "platform/nuttx/init.py", "platform/nuttx/mcuboot.py",
    "tests/nuttx-std/prepare-std.py", "tests/nuttx-std/check-abi.py", "tests/nuttx-std/link.py",
    "tools/prepare-esp-hal-cache.sh",
)
PROFILE_SOURCE = "platform/nuttx/platforms/esp32s3-service-footprint.toml"
INPUT_DIRECTORIES = (
    "platform/nuttx/std-app", "upstream/nuttx", "upstream/nuttx-apps",
    "upstream/rust-std", "upstream/libc", "upstream/esp-hal-3rdparty",
)
VERIFIER_DEPENDENCIES = (
    "tests/channel-progress/analyze.py", "tests/channel-progress/boot_image.py",
    "tests/channel-progress/capture.py", "tests/channel-progress/cohort.py",
    "tests/channel-progress/fifo_analyze.py", "tests/channel-progress/fifo_cohort.py",
    "tests/channel-progress/fifo_sources.py", "tests/channel-progress/mutex_analyze.py",
    "tests/channel-progress/prepare-crossbeam.py", "tests/channel-progress/std_cohort.py",
    "tests/std-channel-perf/simpleboot_digest.py", "tests/std-channel-perf/inspect_esp_image.py",
    "tools/apply-nuttx-patches.py", "tests/std-channel-perf/build_inputs.py",
    "upstream/nuttx/patches/0107-xtensa-simpleboot-hash-offset.patch",
)


def require(condition, message):
    evidence.require(condition, message)


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA,
            "unknown physical campaign manifest")
    require(evidence.canonical(manifest) == PINNED_MANIFEST_SHA256,
            "campaign manifest differs from the frozen candidate artifact")
    require([item["mode"] for item in manifest["builds"]] == list(BUILDS)
            and manifest["cases"] == list(CASES), "campaign must contain the exact eight builds and 19 cases")
    require(all(manifest.get(flag) is False for flag in QUALIFICATION_FLAGS),
            "campaign manifest makes an unsupported qualification claim")
    return manifest


def load_manifest(path=DEFAULT_MANIFEST):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "missing or linked campaign manifest")
    require(path.stat().st_size <= 1024 * 1024, "campaign manifest exceeds size bound")
    return validate_manifest(evidence.read_json(path))


def git(repository, *arguments, data=None):
    result = subprocess.run(["git", "-C", str(repository), *arguments], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
    require(result.returncode == 0, "Git source verification failed: " +
            result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def expected_source_paths(paths):
    paths = set(paths)
    selected = set(FIXED_INPUTS)
    if frontend_inputs.MARKER in paths:
        selected.update(frontend_inputs.select(paths))
    selected.update(name for name in paths if any(name.startswith(prefix + "/")
                                                for prefix in INPUT_DIRECTORIES))
    selected.update(name for name in paths if name.startswith("tests/channel-progress/")
                    and Path(name).suffix in (".rs", ".toml", ".py")
                    and "__pycache__" not in Path(name).parts)
    require(selected <= paths, "Git revision lacks required ESP build inputs: " +
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
        if name == PROFILE_SOURCE:
            profile = tomllib.loads(data.decode("utf-8"))
    require(offset == len(objects), "unexpected trailing Git source objects")
    require(profile is not None and "extends" not in profile
            and profile.get("target") == TARGET and profile.get("board") == "esp32s3-devkit:nsh"
            and profile.get("hal") == {},
            "source policy only supports the non-inheriting ESP service-footprint profile with no HAL bindings")
    return inventory


def verify_sources(repository, receipt):
    manifest = load_manifest()
    expected = manifest["source"]
    require(receipt.get("source_revision") == expected["revision"]
            and receipt.get("platform") == PLATFORM and receipt.get("target") == TARGET,
            "firmware source is not the frozen ESP candidate")
    inventory = git_source_inventory(repository, expected["revision"])
    require(isinstance(receipt.get("source_sha256"), dict)
            and receipt["source_sha256"] == inventory,
            "recorded source inventory differs from the complete Git build input set")
    require(len(inventory) == expected["input_count"]
            and evidence.canonical(inventory) == expected["inventory_sha256"],
            "Git input inventory differs from the frozen candidate")
    tree = git(repository, "rev-parse", expected["revision"] + "^{tree}").decode().strip()
    require(tree == expected["tree"] and git(repository, "rev-parse",
            expected["head"] + "^{tree}").decode().strip() == tree,
            "firmware source tree differs from the frozen candidate head and CI merge")
    return {"source_revision": expected["revision"], "source_head": expected["head"],
            "source_tree": tree, "source_input_count": len(inventory),
            "inventory_policy": (FRONTEND_INVENTORY_POLICY if frontend_inputs.MARKER in inventory
                                 else INVENTORY_POLICY),
            "inventory_sha256": evidence.canonical(inventory)}


def regular_tree(root):
    root = Path(root)
    require(not root.is_symlink() and root.is_dir(), "missing or linked retained build directory")
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            require(not path.is_symlink() and (path.is_dir() or path.is_file()),
                    "linked or non-regular retained build evidence: " + str(path))


def verify_build(directory, manifest_build, repository=None):
    """Validate one retained image against a build entry from load_manifest()."""
    directory = Path(directory)
    regular_tree(directory)
    mode = manifest_build["mode"]
    require(mode in BUILDS, "build mode is outside the fixed campaign")
    receipt_path = evidence.checked_file(directory, "build.json")
    require(evidence.sha256(receipt_path) == manifest_build["build_receipt_sha256"],
            "build receipt bytes differ from the frozen candidate: " + mode)
    case = next(item for item in CASES if item["build_mode"] == mode)
    receipt = evidence.build_receipt(receipt_path, case["mode"])
    require(receipt.get("boot_image_profile") == PROFILE and receipt.get("platform") == PLATFORM
            and receipt.get("target") == TARGET and receipt.get("source_revision") == PINNED_REVISION,
            "build profile, platform or source revision differs from the frozen candidate")
    for field in ("build_id", "image_file", "image_sha256"):
        require(receipt.get(field) == manifest_build[field], "frozen candidate mismatch: " + mode + ": " + field)
    require(receipt["image_file"] == IMAGE_FILE
            and evidence.checked_file(directory, IMAGE_FILE).stat().st_size == FLASH_BYTES,
            "candidate must retain the complete selected 16 MiB image")
    original = evidence.checked_file(directory, ORIGINAL_IMAGE_FILE)
    require(original.stat().st_size == FLASH_BYTES
            and evidence.sha256(original) == manifest_build["original_image_sha256"],
            "candidate original image differs from its bound fresh build")
    require(receipt.get("runtime_kind") == "unmeasured", "build receipt claims execution")
    if repository is not None:
        verify_sources(repository, receipt)
    return receipt


def verifier_identity():
    own = sorted(p.relative_to(ROOT).as_posix() for p in HERE.glob("*.py") if p.is_file())
    names = [*own, "tests/channel-progress-physical/candidate-manifest.json", *VERIFIER_DEPENDENCIES]
    files = {name: evidence.sha256(ROOT / name) for name in names}
    try:
        revision = git(ROOT, "rev-parse", "HEAD").decode().strip()
        dirty = bool(git(ROOT, "status", "--porcelain", "--untracked-files=all", "--", *names))
    except (OSError, ValueError, subprocess.SubprocessError):
        revision, dirty = None, None
    return {"source_revision": revision, "working_tree_dirty": dirty,
            "files_sha256": files, "files_identity_sha256": evidence.canonical(files)}


def preflight(build_root, repository, manifest, *, archive=None):
    validate_manifest(manifest)
    root = Path(build_root)
    regular_tree(root)
    names = {path.name for path in root.iterdir()}
    require(names == set(BUILDS), "candidate root must contain exactly the eight frozen build directories")
    archive_report = {"status": "not-supplied", "expected_sha256": manifest["artifact"]["archive_sha256"]}
    if archive is not None:
        archive = Path(archive)
        require(not archive.is_symlink() and archive.is_file(), "missing or linked candidate archive")
        require(archive.stat().st_size == manifest["artifact"]["archive_bytes"]
                and evidence.sha256(archive) == manifest["artifact"]["archive_sha256"],
                "candidate archive differs from the frozen GitHub artifact")
        archive_report = {"status": "verified", "bytes": archive.stat().st_size,
                          "sha256": manifest["artifact"]["archive_sha256"]}
    records, identity, first_receipt = [], None, None
    for selected in manifest["builds"]:
        receipt = verify_build(root / selected["mode"], selected)
        current = {key: receipt[key] for key in cohort.common_identity_fields(receipt)}
        if identity is None:
            identity, first_receipt = current, receipt
        else:
            require(current == identity, "mixed firmware source/configuration identity in fixed campaign")
        records.append({**selected, "retained_artifacts": len(receipt["artifacts_sha256"]),
                        "build_evidence_verified": True})
    source = verify_sources(repository, first_receipt)
    return {"schema": SCHEMA, "preflight_complete": True,
            "manifest_sha256": evidence.canonical(manifest), "artifact": manifest["artifact"],
            "archive_verification": archive_report, "source_git_binding": source,
            "boot_image_profile": PROFILE, "builds": records, "build_count": len(records),
            "cases": list(CASES), "case_count": len(CASES), "runtime_kind": "unmeasured",
            "verifier": verifier_identity(), **{flag: False for flag in QUALIFICATION_FLAGS},
            "scope": "Exact retained firmware preflight only. Requires a separate exclusive board handoff, "
                     "fresh backup, flashing, nineteen fresh boots and verified restoration by the operator."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="verify existing artifacts and Git inputs without device access")
    check.add_argument("--build-root", type=Path, required=True)
    check.add_argument("--repository", type=Path, default=ROOT)
    check.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    check.add_argument("--archive", type=Path, help="also verify the retained download's byte hash and size")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists() and not args.output.is_symlink(), "refusing to overwrite preflight output")
        require(not args.output.resolve().is_relative_to(args.build_root.resolve()),
                "preflight output must be outside the retained build root")
        report = preflight(args.build_root, args.repository, load_manifest(args.manifest), archive=args.archive)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("CHANNEL_PROGRESS_PHYSICAL_PREFLIGHT_REFUSED: " + str(error), file=sys.stderr)
        return 1
    print("CHANNEL_PROGRESS_PHYSICAL_PREFLIGHT_COMPLETE builds=8 cases=19 physical_execution_verified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
