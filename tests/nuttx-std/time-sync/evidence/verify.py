#!/usr/bin/env python3
"""Retain and replay original time API evidence, including normal build inputs."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent
spec = importlib.util.spec_from_file_location("time_evidence_workflow", FIXTURE / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)
oracle = workflow.support.load_module("time_evidence_oracle", FIXTURE / "oracle.py")
digest, save = workflow.digest, workflow.save
VARIANTS = ("baseline", "controller")
BINARIES = ("firmware/nuttx/nuttx", "firmware/nuttx/nuttx.bin", "firmware/nxrs-nuttx-link")
RECEIPT = "firmware/.nxrs-build-cache.json"
PROJECTION = "firmware/native-receipt-projection.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def restore_receipt(common_inputs, projection):
    require(projection.get("schema") == 1 and projection.get("kind") == "original-native-receipt-input-projection",
            "unsupported receipt projection")
    inputs = {**common_inputs, "features": projection["features"]}
    require(canonical_hash(inputs) == projection["canonical_inputs_sha256"], "retained input projection differs")
    return {"schema": 1, "inputs": inputs, "files": projection["selected_file_hashes"]}


def receipt_pair(builds, receipts, frontends):
    """Check contemporaneous frontend/SDK/native inputs, not reconstructed inputs."""
    common = []
    for variant, build, receipt, frontend in zip(VARIANTS, builds, receipts, frontends):
        require(receipt.get("schema") == 1, "unsupported native receipt schema")
        inputs = receipt["inputs"]
        require(inputs["features"] == ([] if variant == "baseline" else ["controller"]),
                "unexpected native receipt features")
        require(inputs["controller"] == frontend, "compiled frontend binary differs from original receipt")
        require(inputs["compiler"] == build["compiler"]["rustc_sha256"], "Rust compiler identity differs")
        require(inputs["native_compiler"] == build["compiler"]["cc_sha256"], "native compiler identity differs")
        require(inputs["nuttx"] == build["identities"]["nuttx"]
                and inputs["apps"] == build["identities"]["nuttx-apps"], "native revision identity differs")
        for name in ("nuttx/nuttx", "nuttx/nuttx.bin", "nuttx/.config"):
            require(receipt["files"][name + ":bytes"] == build["artifacts"]["firmware/" + name],
                    "native receipt image/config differs from captured build")
        require(inputs["build_inputs"] and inputs["sdk_inputs"], "native input inventory is empty")
        common.append({key: value for key, value in inputs.items() if key != "features"})
    require(common[0] == common[1], "native inputs differ beyond the intentional software feature")
    return {"same_inputs_except_features": True, "frontend_sha256": frontends[0],
            "build_input_count": len(common[0]["build_inputs"]),
            "sdk_input_count": len(common[0]["sdk_inputs"]),
            "expected_features": [[], ["controller"]]}


def captured_build(path, omitted):
    build = json.loads((path / "build.json").read_text())
    variant = build["variant"]
    require(build.get("build_complete") is True, "incomplete original build")
    require(build["sources_before"] == build["sources_after"] == workflow.sources(),
            "captured fixture/production source manifest differs from current files")
    for name, checksum in build["artifacts"].items():
        current = digest(path / name) if (path / name).is_file() else omitted[name]["sha256"]
        require(current == checksum, "retained artifact differs from original build")
    analysis = json.loads((path / "analysis.json").read_text())
    require(analysis["qualified"] is True and analysis["variant"] == variant,
            "original capture was not qualified")
    require(analysis["build_sha256"] == digest(path / "build.json")
            and analysis["artifacts"] == build["artifacts"], "capture is bound to a different build")
    require(analysis["oracle_sha256"] == digest(FIXTURE / "oracle.py")
            and analysis["workflow_sha256"] == digest(FIXTURE / "workflow.py")
            and analysis["harness_sha256"] == digest(FIXTURE.parent / "run.py"),
            "captured oracle/workflow/harness differs")
    captures = json.loads((path / "captures/results.json").read_text())
    require(len(captures) == len(analysis["runs"]) == 3, "three fresh boots are required")
    for index, captured in enumerate(captures):
        require(captured["success"] is True and captured["mode"] == variant
                and captured["image_sha256"] == build["artifacts"]["firmware/nuttx/nuttx"],
                "capture image/status identity differs")
        command = captured["command"]
        require(command[command.index("-M") + 1] == "mps2-an521"
                and command[command.index("-icount") + 1] == "shift=3,sleep=off",
                "capture virtual clock/machine differs")
        require(build["identities"]["nuttx"][:12] in captured["uname"], "capture kernel revision differs")
        text = (path / f"captures/console-{index}-{variant}.log").read_text()
        require(re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", text.replace("\r", ""), re.M) == ["0"],
                "capture does not contain one successful NSH result")
        require(oracle.analyze(text, variant) == analysis["runs"][index], "capture arithmetic replay differs")
    return build


def section_sizes(path, binary_size):
    sections = {}
    for line in (path / "firmware/image-size.txt").read_text().splitlines():
        match = re.fullmatch(r"\s*(\.[\w.]+)\s+(\d+)\s+(\d+)\s*", line)
        if match:
            sections[match[1]] = int(match[2])
    return {"linked_flash_bytes": sum(sections.get(key, 0) for key in
                                     (".text", ".rodata", ".ARM.extab", ".ARM.exidx", ".data")),
            "unpadded_image_bytes": binary_size,
            "static_ram_bytes": sections[".data"] + sections[".bss"], "sections": sections}


def verify(path, check_local=False):
    manifest = json.loads((path / "evidence.json").read_text())
    require(manifest.get("schema") == 1 and manifest["verifier_sha256"] == digest(Path(__file__)),
            "supplemental verifier identity differs")
    for name, checksum in manifest["retained_files"].items():
        require(digest(path / name) == checksum, "retained evidence was modified")
    if check_local:
        for binary in manifest["omitted_binaries"].values():
            original = Path(binary["local_path"])
            require(digest(original) == binary["sha256"] and original.stat().st_size == binary["bytes"],
                    "locally retained binary differs")
        for receipt in manifest["original_receipts"].values():
            original = Path(receipt["local_path"])
            require(digest(original) == receipt["sha256"] and original.stat().st_size == receipt["bytes"],
                    "locally retained original receipt differs")
    builds, receipts, frontends = [], [], []
    comparison = json.loads((path / "footprint.json").read_text())
    common_inputs = json.loads((path / "native-inputs.json").read_text())
    for index, variant in enumerate(VARIANTS):
        folder = path / variant
        omitted = {name: manifest["omitted_binaries"][variant + "/" + name] for name in BINARIES}
        build = captured_build(folder, omitted)
        require(build["variant"] == variant, "retained variant inventory differs")
        require(digest(folder / "build.json") == comparison["build_sha256"][index],
                "comparison references a different build")
        measured = section_sizes(folder, omitted["firmware/nuttx/nuttx.bin"]["bytes"])
        require(measured == comparison[variant], "linked section/raw image comparison differs")
        builds.append(build)
        projection = json.loads((folder / PROJECTION).read_text())
        require(projection["original_receipt_sha256"] == manifest["original_receipts"][variant]["sha256"],
                "projection points to a different original receipt")
        receipt = restore_receipt(common_inputs, projection)
        if check_local:
            original = json.loads(Path(manifest["original_receipts"][variant]["local_path"]).read_text())
            require(original["inputs"] == receipt["inputs"]
                    and all(original["files"][key] == value for key, value in receipt["files"].items()),
                    "projection differs from locally retained original receipt")
        receipts.append(receipt)
        frontends.append(omitted["firmware/nxrs-nuttx-link"]["sha256"])
    for key in ("sources_after", "compiler", "native_environment_sha256"):
        require(builds[0][key] == builds[1][key], "comparison input differs: " + key)
    require(builds[0]["artifacts"]["firmware/nuttx/.config"]
            == builds[1]["artifacts"]["firmware/nuttx/.config"], "effective configurations differ")
    for key in ("linked_flash_bytes", "unpadded_image_bytes", "static_ram_bytes"):
        require(comparison["delta"][key] == comparison["controller"][key] - comparison["baseline"][key],
                "comparison arithmetic differs")
    proof = receipt_pair(builds, receipts, frontends)
    require(proof == manifest["original_receipt_check"], "supplemental native receipt proof differs")
    return {"qualified": True, "fresh_boots": 6, "original_receipt_check": proof,
            "delta": comparison["delta"], "local_binaries_rehashed": check_local,
            "original_full_receipts_rehashed": check_local}


def retain(args):
    outputs = [args.baseline.resolve(), args.controller.resolve()]
    builds = [workflow.checked_build(path) for path in outputs]
    receipts = [json.loads((path / RECEIPT).read_text()) for path in outputs]
    frontends = [digest(path / "firmware/nxrs-nuttx-link") for path in outputs]
    proof = receipt_pair(builds, receipts, frontends)
    destination = args.out.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    omitted, original_receipts = {}, {}
    common_inputs = {key: value for key, value in receipts[0]["inputs"].items() if key != "features"}
    save(destination / "native-inputs.json", common_inputs)
    for variant, original, build, receipt in zip(VARIANTS, outputs, builds, receipts):
        receipt_path = original / RECEIPT
        original_receipts[variant] = {"sha256": digest(receipt_path), "bytes": receipt_path.stat().st_size,
                                     "local_path": str(receipt_path)}
        for name in BINARIES:
            binary = original / name
            omitted[variant + "/" + name] = {"sha256": digest(binary), "bytes": binary.stat().st_size,
                                               "local_path": str(binary)}
        retained = {original / "build.json", original / "build.log", original / "analysis.json"}
        retained.update((original / "captures").glob("*"))
        retained.update(original / name for name in build["artifacts"] if name not in BINARIES)
        retained.update(file for file in (original / "firmware").iterdir()
                        if file.is_file() and file != receipt_path and file.suffix in
                        (".json", ".jsonl", ".txt", ".sha256", ".config", ".patch", ".c", ".rs"))
        for source in sorted(retained):
            target = destination / variant / source.relative_to(original)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        projection = {"schema": 1, "kind": "original-native-receipt-input-projection",
                      "original_receipt_sha256": original_receipts[variant]["sha256"],
                      "canonical_inputs_sha256": canonical_hash(receipt["inputs"]),
                      "features": receipt["inputs"]["features"],
                      "selected_file_hashes": {name + ":bytes": receipt["files"][name + ":bytes"]
                                               for name in ("nuttx/nuttx", "nuttx/nuttx.bin", "nuttx/.config")}}
        save(destination / variant / PROJECTION, projection)
    shutil.copyfile(args.comparison, destination / "footprint.json")
    manifest = {"schema": 1, "kind": "supplemental-original-receipt-validation",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "verifier_sha256": digest(Path(__file__)),
                "retained_files": {str(p.relative_to(destination)): digest(p)
                                   for p in sorted(destination.rglob("*")) if p.is_file()},
                "omitted_binaries": omitted, "original_receipts": original_receipts,
                "original_receipt_check": proof,
                "notes": ["Original build/capture records and selected native receipts are copied unchanged.",
                          "This later check verifies contemporaneous build-tool/SDK/native identities.",
                          "Full native receipts and binaries remain local; git retains their hashes and lengths.",
                          "All original native input fields are retained once with exact feature differences and canonical per-build hashes."]}
    save(destination / "evidence.json", manifest)
    print(json.dumps(verify(destination, check_local=True), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("retain")
    capture.add_argument("--baseline", type=Path, required=True)
    capture.add_argument("--controller", type=Path, required=True)
    capture.add_argument("--comparison", type=Path, required=True)
    capture.add_argument("--out", type=Path, required=True)
    replay = commands.add_parser("verify")
    replay.add_argument("evidence", type=Path)
    replay.add_argument("--check-local-binaries", action="store_true")
    args = parser.parse_args()
    if args.command == "retain":
        retain(args)
    else:
        print(json.dumps(verify(args.evidence.resolve(), args.check_local_binaries), indent=2))


if __name__ == "__main__":
    main()
